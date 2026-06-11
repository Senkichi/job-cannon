#!/usr/bin/env bash
# bootstrap.sh -- end-user one-liner installer for Job Cannon (WP7).
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.sh)"
#
# (Plain `curl ... | bash` works identically -- the script is prompt-free.)
#
# What it does: find Python 3.12+ (print your package manager's install
# command if missing) -> ensure pipx (user-space pip install, never sudo) ->
# pipx install/upgrade job-cannon -> launch. Idempotent; re-running upgrades.
#
# This is NOT the contributor installer -- that's install.sh, which syncs a
# git checkout with uv. Config via env vars:
#   JC_BOOTSTRAP_NO_LAUNCH=1  install only, don't start the app
set -euo pipefail

say()  { printf '%s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }

# No sudo, no prompts: every step is pure user-space (pip --user, pipx).
# Anything that would need elevation is printed for the user to run instead,
# so headless `curl | bash` and interactive runs behave identically.

# --- Step 1: find Python 3.12+ ----------------------------------------------
say '==> Looking for Python 3.12+...'
PY=""
for candidate in python3.13 python3.12 python3 python; do
    command -v "$candidate" > /dev/null 2>&1 || continue
    ver="$("$candidate" --version 2> /dev/null | awk '{print $2}')"
    major="${ver%%.*}"
    rest="${ver#*.}"
    minor="${rest%%.*}"
    if [ "${major:-0}" -gt 3 ] 2> /dev/null || { [ "${major:-0}" -eq 3 ] && [ "${minor:-0}" -ge 12 ]; } 2> /dev/null; then
        PY="$candidate"
        say "    found Python $ver ($candidate)"
        break
    fi
done

if [ -z "$PY" ]; then
    say '    Python 3.12+ not found. Install it with your package manager, then re-run this script:'
    if command -v apt-get > /dev/null 2>&1; then
        say '      sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv python3-pip'
        say '      (older Ubuntu/Debian may need the deadsnakes PPA for 3.12)'
    elif command -v dnf > /dev/null 2>&1; then
        say '      sudo dnf install -y python3.12'
    elif command -v pacman > /dev/null 2>&1; then
        say '      sudo pacman -S python'
    elif command -v brew > /dev/null 2>&1; then
        say '      brew install python@3.12'
    else
        say '      https://www.python.org/downloads/'
    fi
    fail 'This script never runs sudo itself -- run the line above, then re-run the one-liner.'
fi

# --- Step 2: ensure pipx -----------------------------------------------------
say '==> Checking for pipx...'
if "$PY" -m pipx --version > /dev/null 2>&1; then
    say '    pipx is already installed.'
else
    say '    pipx not found -- installing into your user account (no sudo)...'
    if ! "$PY" -m pip install --user pipx > /dev/null 2>&1; then
        # PEP 668 distros (Ubuntu 23.04+, Fedora 38+) refuse user pip installs
        # without an explicit override. A --user install of pipx itself is the
        # documented low-risk exception -- it touches nothing system-managed.
        say '    (externally-managed environment -- retrying with --break-system-packages)'
        "$PY" -m pip install --user --break-system-packages pipx \
            || fail 'Could not install pipx. Try your package manager (e.g. sudo apt install pipx), then re-run.'
    fi
    "$PY" -m pipx ensurepath > /dev/null 2>&1 || true
    # shellcheck disable=SC2016  # literal backticks, nothing to expand
    say '    pipx installed. (New terminals will have `pipx` on PATH.)'
fi

# --- Step 3: install or upgrade job-cannon -----------------------------------
# `ensurepath` only edits future sessions, so pipx is always invoked as
# `$PY -m pipx` here -- never as a bare `pipx`.
if "$PY" -m pipx list --short 2> /dev/null | grep -q '^job-cannon'; then
    say '==> job-cannon is already installed -- upgrading...'
    "$PY" -m pipx upgrade job-cannon
else
    say '==> Installing job-cannon...'
    "$PY" -m pipx install job-cannon
fi

# --- Step 4: launch -----------------------------------------------------------
if [ "${JC_BOOTSTRAP_NO_LAUNCH:-}" = "1" ]; then
    say '==> Done. Start it any time with: job-cannon  (new terminal), then visit http://localhost:5000'
    exit 0
fi
BIN_DIR="$("$PY" -m pipx environment --value PIPX_BIN_DIR 2> /dev/null || true)"
if [ -n "$BIN_DIR" ] && [ -x "$BIN_DIR/job-cannon" ]; then
    say '==> Launching Job Cannon (http://localhost:5000)...'
    "$BIN_DIR/job-cannon"
else
    say "==> Installed, but couldn't resolve the launcher. Open a NEW terminal and run: job-cannon"
fi
