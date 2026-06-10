#!/usr/bin/env bash
# Job Cannon bootstrap (macOS).
#
# Walks a fresh-machine user from a clean clone to a running app in their
# browser. Detects required tools (Python 3.13+, Git), installs uv via the
# official Astral installer if missing, runs `uv sync`, optionally
# installs Ollama + the `qwen2.5:14b` model and Node.js + the Claude Code
# CLI, then launches `uv run job-cannon` (which prints a URL banner and
# auto-opens the browser via the F2 entry-point work).
#
# Flags:
#   --yes        Accept every prompt non-interactively.
#   --minimal    Skip Ollama, Node, and the Claude Code CLI. Only does
#                Python+Git detection, uv install, uv sync, and launch.
#                For users on a paid Anthropic key who don't want the
#                9 GB Ollama download.
#   --no-launch  Stop after install steps; do not run `uv run job-cannon`.
#   -h / --help  Print this header.
#
# Tested on macOS (Homebrew path). Linux is not covered by this script;
# Linux users follow docs/SETUP.md for manual setup steps.

set -u  # error on undefined vars; intentionally NOT -e so we can surface
        # next-manual-command guidance on failures rather than exit silently.

# --- Constants ---
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=13
OLLAMA_MODEL="qwen2.5:14b"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# --- Flag parsing ---
ASSUME_YES=0
MINIMAL=0
LAUNCH=1
for arg in "$@"; do
    case "$arg" in
        -y|--yes)        ASSUME_YES=1 ;;
        --minimal)       MINIMAL=1 ;;
        --no-launch)     LAUNCH=0 ;;
        -h|--help)
            sed -n '1,/^# UAT/p' "$0" | sed 's/^# \{0,1\}//;1d'
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg (try --help)" >&2
            exit 2
            ;;
    esac
done

# --- Output helpers (colour if stdout is a tty) ---
if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m'); RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m'); BLUE=$(printf '\033[34m')
    RESET=$(printf '\033[0m')
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

info()    { printf "%s==>%s %s\n" "$BLUE" "$RESET" "$*"; }
ok()      { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()    { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
fail()    { printf "%s✗%s %s\n" "$RED" "$RESET" "$*" >&2; }
step()    { printf "\n%s%s%s\n" "$BOLD" "$*" "$RESET"; }

# Prompt with a default-Y answer. Returns 0 on yes, 1 on no.
# Honors --yes (always returns 0). Single argument: the prompt text.
prompt_yes() {
    if [ "$ASSUME_YES" -eq 1 ]; then
        return 0
    fi
    local reply=""
    printf "%s [Y/n] " "$1"
    # Prefer reading from the controlling terminal so stdin pipes don't
    # accidentally feed answers. Fall back to plain stdin when /dev/tty
    # is unavailable (e.g., Git Bash on Windows, some CI runners).
    # Probe /dev/tty in a subshell with stderr suppressed — the [ -r ]
    # test passes on some Git Bash subprocesses where the actual open()
    # still EIOs, and a failed redirection at the shell level can't be
    # caught with `||` because it triggers before the command runs.
    if (exec </dev/tty) 2>/dev/null; then
        read -r reply </dev/tty || reply=""
    else
        read -r reply || reply=""
    fi
    case "$reply" in
        ""|y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Banner ---
cat <<EOF
${BOLD}Job Cannon bootstrap (macOS)${RESET}

This script will:
  1. Check for Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ and Git (exits with install link if missing).
  2. Install ${BOLD}uv${RESET} (Astral's Python package manager) if missing.
  3. Run ${BOLD}uv sync --extra dev --extra eval${RESET} to install the project.
EOF
if [ "$MINIMAL" -eq 0 ]; then
cat <<EOF
  4. (Optional) Install ${BOLD}Ollama${RESET} + pull ${BOLD}${OLLAMA_MODEL}${RESET} (~9 GB).
  5. (Optional) Install ${BOLD}Node.js${RESET} + ${BOLD}@anthropic-ai/claude-code${RESET} CLI.
EOF
fi
if [ "$LAUNCH" -eq 1 ]; then
    echo "  $(if [ "$MINIMAL" -eq 0 ]; then echo 6; else echo 4; fi). Launch the app (${BOLD}uv run job-cannon${RESET}) and open your browser."
fi
echo
if ! prompt_yes "Continue?"; then
    info "Aborted."
    exit 0
fi

# --- Step 1: Python ---
step "Step 1 — Checking Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+"

PY_BIN=""
for candidate in python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version_str=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
        major=$(echo "$version_str" | cut -d. -f1)
        minor=$(echo "$version_str" | cut -d. -f2)
        if [ -n "$major" ] && [ -n "$minor" ]; then
            if [ "$major" -gt "$PYTHON_MIN_MAJOR" ] || \
               { [ "$major" -eq "$PYTHON_MIN_MAJOR" ] && [ "$minor" -ge "$PYTHON_MIN_MINOR" ]; }; then
                PY_BIN="$candidate"
                ok "Found $candidate (Python $version_str)"
                break
            fi
        fi
    fi
done

if [ -z "$PY_BIN" ]; then
    fail "Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ not found on PATH."
    cat >&2 <<EOF

Install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} from one of:
  - https://www.python.org/downloads/    (official installer)
  - brew install python@${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}             (Homebrew)

Then re-run this script.
EOF
    exit 2
fi

# --- Step 2: Git ---
step "Step 2 — Checking Git"

if ! command -v git >/dev/null 2>&1; then
    fail "git not found on PATH."
    cat >&2 <<EOF

Install Git from one of:
  - https://git-scm.com/download/mac     (official installer)
  - brew install git                     (Homebrew)
  - xcode-select --install               (Apple Command Line Tools)

Then re-run this script.
EOF
    exit 2
fi
ok "Found git ($(git --version))"

# --- Step 3: uv ---
step "Step 3 — Installing uv (Astral)"

ensure_uv_on_path() {
    # uv's installer drops the binary at ~/.local/bin (or $XDG_BIN_HOME).
    # Add it to PATH for the current process so the subsequent `uv` calls
    # find it without requiring a new shell.
    if [ -d "$HOME/.local/bin" ] && [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if [ -d "$HOME/.cargo/bin" ] && [[ ":$PATH:" != *":$HOME/.cargo/bin:"* ]]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
}

ensure_uv_on_path
if command -v uv >/dev/null 2>&1; then
    ok "Found uv ($(uv --version))"
else
    info "uv is not installed."
    info "Will run: curl -LsSf ${UV_INSTALL_URL} | sh"
    if prompt_yes "Install uv now?"; then
        if ! curl -LsSf "$UV_INSTALL_URL" | sh; then
            fail "uv install failed."
            cat >&2 <<EOF

Try the manual command:
    curl -LsSf ${UV_INSTALL_URL} | sh

Or see https://docs.astral.sh/uv/getting-started/installation/ for alternatives.
EOF
            exit 1
        fi
        ensure_uv_on_path
        if ! command -v uv >/dev/null 2>&1; then
            fail "uv install completed but \`uv\` not found on PATH."
            warn "Open a new terminal (so PATH picks up ~/.local/bin) and re-run this script."
            exit 1
        fi
        ok "Installed uv ($(uv --version))"
    else
        warn "Skipped uv install. Cannot continue without uv."
        exit 1
    fi
fi

# --- Step 4: uv sync ---
step "Step 4 — Syncing project dependencies (uv sync --extra dev --extra eval)"

cd "$REPO_ROOT" || { fail "Could not cd into $REPO_ROOT"; exit 1; }
if ! uv sync --extra dev --extra eval; then
    fail "uv sync failed."
    cat >&2 <<EOF

Try the manual command from the repo root:
    uv sync --extra dev --extra eval

EOF
    exit 1
fi
ok "Project dependencies installed."

# --- Step 5: Ollama (optional, skipped with --minimal) ---
if [ "$MINIMAL" -eq 0 ]; then
    step "Step 5 — Ollama + ${OLLAMA_MODEL}"

    if command -v ollama >/dev/null 2>&1; then
        ok "Found ollama ($(ollama --version 2>/dev/null | head -1))"
    else
        info "Ollama is not installed. It runs your local LLM (Job Cannon's free \$0 scoring tier)."
        if prompt_yes "Install Ollama now?"; then
            installed=0
            if command -v brew >/dev/null 2>&1; then
                info "Using Homebrew: brew install --cask ollama"
                if brew install --cask ollama; then
                    installed=1
                fi
            fi
            if [ "$installed" -eq 0 ]; then
                fail "Could not install Ollama automatically."
                cat >&2 <<EOF

Install manually from: https://ollama.com/download/mac
Then re-run this script.

EOF
                warn "Continuing without Ollama. Cascade fallbacks (Groq/Cerebras/Gemini/Anthropic) will be used instead."
            else
                ok "Installed Ollama."
            fi
        else
            warn "Skipped Ollama. Cascade fallbacks will handle scoring."
        fi
    fi

    if command -v ollama >/dev/null 2>&1; then
        # Check if the model is already pulled.
        if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$OLLAMA_MODEL"; then
            ok "Model ${OLLAMA_MODEL} already pulled."
        else
            warn "About to pull ${OLLAMA_MODEL} (~9 GB download)."
            if prompt_yes "Pull ${OLLAMA_MODEL} now?"; then
                if ! ollama pull "$OLLAMA_MODEL"; then
                    fail "ollama pull ${OLLAMA_MODEL} failed."
                    warn "You can retry manually: ollama pull ${OLLAMA_MODEL}"
                else
                    ok "Pulled ${OLLAMA_MODEL}."
                fi
            else
                warn "Skipped model pull. Job Cannon will fall through the provider cascade."
            fi
        fi
    fi

    # --- Step 6: Node + Claude Code CLI (optional) ---
    step "Step 6 — Node.js + Claude Code CLI"

    if command -v node >/dev/null 2>&1; then
        ok "Found node ($(node --version))"
    else
        info "Node.js is not installed. Job Cannon uses the Claude Code CLI as a \$0 fallback."
        if prompt_yes "Install Node.js now?"; then
            installed=0
            if command -v brew >/dev/null 2>&1; then
                info "Using Homebrew: brew install node"
                if brew install node; then
                    installed=1
                fi
            fi
            if [ "$installed" -eq 0 ]; then
                fail "Could not install Node.js automatically."
                cat >&2 <<EOF

Install manually from: https://nodejs.org/en/download/
Then re-run this script.

EOF
                warn "Continuing without Node + Claude Code CLI."
            else
                ok "Installed Node.js."
            fi
        else
            warn "Skipped Node install. Claude Code CLI fallback will not be available."
        fi
    fi

    if command -v node >/dev/null 2>&1; then
        if command -v claude >/dev/null 2>&1; then
            ok "Found Claude Code CLI."
        else
            info "Will run: npm install -g @anthropic-ai/claude-code"
            if prompt_yes "Install the Claude Code CLI now?"; then
                if ! npm install -g @anthropic-ai/claude-code; then
                    fail "npm install -g @anthropic-ai/claude-code failed."
                    warn "You can retry manually: npm install -g @anthropic-ai/claude-code"
                else
                    ok "Installed Claude Code CLI."
                    cat <<EOF

${BOLD}Claude Code CLI installed.${RESET} To log in (one-time, opens your browser):
    ${BLUE}claude /login${RESET}

Run that in your own terminal whenever you're ready. The app works without
it; the CLI is one of several fallbacks in the scoring cascade.

EOF
                fi
            else
                warn "Skipped Claude Code CLI."
            fi
        fi
    fi
fi

# --- Final step: launch ---
if [ "$LAUNCH" -eq 1 ]; then
    step "Launching Job Cannon"
    cat <<EOF

About to run: ${BOLD}uv run job-cannon${RESET}

This prints a URL banner and opens your default browser ~1.5 s later.
Press Ctrl+C in this terminal to stop the server.

EOF
    if ! prompt_yes "Launch now?"; then
        info "Skipped launch. Run \`uv run job-cannon\` whenever you're ready."
        exit 0
    fi
    exec uv run job-cannon
fi

ok "Bootstrap complete. Start the app with: uv run job-cannon"
