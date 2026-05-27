"""Onboarding state persistence + before_request gate (STRANGE-WIZ-01, Phase 42).

Per D-13/D-13a: all inter-step wizard data lives in onboarding_state.wizard_data (TEXT,
JSON-encoded). Single-row store keyed off id=1. Final config write happens only at the
Done step (D-15); this module provides the helpers but the call site is
blueprint.py:done.

Per D-15: _deep_merge and _write_config are DUPLICATED verbatim from
job_finder/web/blueprints/settings.py — CLAUDE.md flags config.yaml as wipe-vulnerable
and CONTEXT.md guidance says duplication is safer than lateral imports.

Per D-18: gate_onboarding is the @app.before_request callable that redirects
unwhitelisted paths to /onboarding/welcome when onboarding_complete=0.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import yaml
from flask import g, redirect, request, url_for
from flask import Response  # re-export of werkzeug.wrappers.Response; more idiomatic in blueprint code

from job_finder.web import user_data_dirs
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

# --- Whitelist for the gate (D-18) ---
_WHITELIST_PREFIXES: tuple[str, ...] = ("/onboarding/", "/static/")
_WHITELIST_EXACT: tuple[str, ...] = ("/favicon.ico",)


# --- Helpers ---

def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge overrides into base, returning a new dict.

    Verbatim duplicate of blueprints/settings.py:_deep_merge — CLAUDE.md flags
    config.yaml writes as wipe-vulnerable; duplication isolates the wizard's atomic
    write from settings.py's. See CONTEXT.md D-15.
    """
    merged = dict(base)
    for key, value in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _write_config(config: dict, config_path: str | Path) -> None:
    """Atomic temp+rename write. Mirrors blueprints/settings.py:_write_config
    (CLAUDE.md mandates atomic writes for any wizard config-touching code) with one
    generalization: the temp suffix is derived from the target's actual extension
    (e.g. `.yaml` -> `.yaml.tmp`, `.json` -> `.json.tmp`) so future non-YAML callers
    don't leave `<name>.yaml.tmp` debris on the filesystem if they ever land. The
    serializer is still yaml.dump (this function's contract is "atomic YAML write"),
    but the suffix logic does not assume YAML.

    On POSIX, the destination file is chmod-ed to 0600 after the replace so an
    IMAP app password / provider API key sitting in plaintext at rest is at least
    not world-readable. Windows uses ACLs not POSIX modes; the default
    home-directory ACL is already user-only there (M-4, 2026-05-20).
    """
    config_path_obj = Path(config_path)
    # Derive `.tmp` from the actual target extension rather than hardcoding `.yaml.tmp`.
    # `with_suffix(config_path_obj.suffix + ".tmp")` yields `.yaml.tmp`, `.yml.tmp`,
    # `.json.tmp`, etc. - matches whatever the target file uses.
    tmp_path = config_path_obj.with_suffix(config_path_obj.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, config_path)
        if os.name != "nt":
            try:
                os.chmod(config_path, 0o600)
            except OSError as exc:
                logger.warning(
                    "could not chmod 0600 on %s; secrets may be world-readable: %s",
                    config_path, exc,
                )
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# --- wizard_data CRUD (D-13a) ---

def _ensure_row(db: sqlite3.Connection) -> None:
    """Single-row store: INSERT (id=1, onboarding_complete=0, wizard_data='{}') if missing."""
    db.execute(
        "INSERT OR IGNORE INTO onboarding_state (id, onboarding_complete, wizard_data) VALUES (1, 0, '{}')"
    )
    db.commit()


def read_wizard_data(db: sqlite3.Connection) -> dict:
    """Return the wizard_data JSON column parsed as a dict. Empty dict if row missing or column NULL."""
    _ensure_row(db)
    row = db.execute("SELECT wizard_data FROM onboarding_state WHERE id = 1").fetchone()
    raw = row["wizard_data"] if row and row["wizard_data"] is not None else "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("onboarding_state.wizard_data was malformed JSON; resetting to {}")
        return {}


def write_wizard_data(db: sqlite3.Connection, slice_: dict) -> None:
    """Deep-merge slice_ into existing wizard_data; write back as JSON. D-13/D-14 semantics."""
    _ensure_row(db)
    existing = read_wizard_data(db)
    merged = _deep_merge(existing, slice_)
    db.execute(
        "UPDATE onboarding_state SET wizard_data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (json.dumps(merged),),
    )
    db.commit()


# --- Completion state ---

def is_onboarding_complete(db: sqlite3.Connection) -> bool:
    """Return True if onboarding_state.onboarding_complete = 1."""
    row = db.execute("SELECT onboarding_complete FROM onboarding_state WHERE id = 1").fetchone()
    return bool(row and row["onboarding_complete"])


def mark_onboarding_complete(db: sqlite3.Connection) -> None:
    """Set onboarding_complete=1, clear wizard_data='{}' per D-16."""
    _ensure_row(db)
    db.execute(
        "UPDATE onboarding_state SET onboarding_complete = 1, wizard_data = '{}', updated_at = CURRENT_TIMESTAMP WHERE id = 1"
    )
    db.commit()


# --- D-19 legacy-install heuristic ---

def _legacy_install_detected() -> bool:
    """Per D-19: if onboarding_state row is missing AND config.yaml exists AND
    experience_profile.json exists in user_data_root, treat the install as already-onboarded.
    All three conditions required (conservative).
    """
    try:
        cfg = Path(user_data_dirs.config_path())
        prof = Path(user_data_dirs.user_data_root()) / "experience_profile.json"
    except Exception:
        return False
    return cfg.exists() and prof.exists()


# --- @app.before_request gate (D-18) ---

def gate_onboarding() -> Response | None:
    """Redirect unwhitelisted paths to /onboarding/welcome when onboarding_complete=0.

    Whitelist: /onboarding/*, /static/*, /favicon.ico.
    Legacy heuristic (D-19): if no row exists but config.yaml + experience_profile.json
    both exist, auto-insert onboarding_complete=1 and pass through.
    SECURITY: NEVER consult request.args.get('next') — open-redirect risk (T-42-04).
    """
    path = request.path
    if path in _WHITELIST_EXACT or any(path.startswith(p) for p in _WHITELIST_PREFIXES):
        return None

    db = get_db()
    row = db.execute("SELECT onboarding_complete FROM onboarding_state WHERE id = 1").fetchone()

    if row is None:
        # No row at all — apply D-19 heuristic
        if _legacy_install_detected():
            db.execute(
                "INSERT OR IGNORE INTO onboarding_state (id, onboarding_complete, wizard_data) VALUES (1, 1, '{}')"
            )
            db.commit()
            return None
        # No row, no legacy install — needs onboarding
        return redirect(url_for("onboarding.welcome"))

    if not row["onboarding_complete"]:
        return redirect(url_for("onboarding.welcome"))

    return None
