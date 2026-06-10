"""Autoheal heal pipeline — ASSEMBLE → GENERATE → VALIDATE → ADOPT.

``run_heal`` is the single entry point, fired from the post-ingestion
detection pass (no scheduler job). Everything is gated on
``config['autoheal']['heal_enabled']`` (default false → never runs in
production), a ``DEGRADED`` source_health status, the attempt cap, and the
backoff window.

C3 ships ASSEMBLE → GENERATE only (audit ``candidate_generated``); the
VALIDATE (C4) and ADOPT (C5) stages are explicit no-op stubs below.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import codegen, override_loader, validator
from job_finder.web.autoheal.recipe_schema import recipe_to_dict
from job_finder.web.model_provider import ProviderCascadeExhaustedError

logger = logging.getLogger(__name__)


def run_heal(conn: sqlite3.Connection, config: dict, source: str) -> str | None:
    """Attempt to heal one DEGRADED source. Returns the audit outcome or None.

    Gates (all must hold, else returns None without a model call):
    - ``autoheal.heal_enabled`` is true (defensive read; default false)
    - source_health.status == 'degraded'
    - heal_attempts < heal_max_attempts
    - backoff window elapsed since last_heal_at
    """
    autoheal_cfg = config.get("autoheal", {}) or {}
    if not autoheal_cfg.get("heal_enabled", False):
        return None

    row = conn.execute(
        "SELECT status, heal_attempts, last_heal_at FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None or row[0] != "degraded":
        return None

    attempts = int(row[1] or 0)
    max_attempts = int(autoheal_cfg.get("heal_max_attempts", 3))
    if attempts >= max_attempts:
        logger.info(
            "autoheal: %s exhausted heal attempts (%d); staying degraded", source, attempts
        )
        return None

    backoff_hours = float(autoheal_cfg.get("heal_backoff_hours", 24))
    if not _backoff_elapsed(row[2], backoff_hours):
        return None

    surface = "ats" if source.startswith("ats:") else "email"

    # --- ASSEMBLE → GENERATE ---
    inputs = codegen.assemble_inputs(conn, source, surface)
    try:
        candidate = codegen.generate_recipe(conn, config, source, surface, inputs=inputs)
    except ProviderCascadeExhaustedError as exc:
        _audit(conn, source, surface, "no_provider", str(exc))
        return "no_provider"

    if candidate is None:
        _record_failure(conn, source)
        _audit(conn, source, surface, "rejected:generation_failed")
        return "rejected:generation_failed"

    _audit(conn, source, surface, "candidate_generated")

    # --- VALIDATE (C4) — subprocess corpus replay + regression proof ---
    timeout_s = float(autoheal_cfg.get("validate_timeout_s", 30))
    verdict = validator.validate(
        candidate,
        surface,
        corpus_samples=inputs["baseline_samples"],
        failing_samples=inputs["failing_samples"],
        timeout_s=timeout_s,
    )
    if not verdict.ok:
        reason = verdict.reason or "rejected"
        _record_failure(conn, source)
        _audit(conn, source, surface, f"rejected:{reason}")
        return f"rejected:{reason}"

    _audit(conn, source, surface, "validated")

    # --- ADOPT (C5) — write the override, hot-swap, reset health ---
    return _adopt_stage(conn, source, surface, candidate)


# ---------------------------------------------------------------------------
# ADOPT
# ---------------------------------------------------------------------------


def _adopt_stage(conn: sqlite3.Connection, source: str, surface: str, candidate) -> str:
    """Write the validated recipe as an override, hot-swap the cache, reset health.

    Override files are keyed by the loader's file layout: email uses the
    label verbatim; ATS strips the ``ats:`` prefix (the loader re-adds it
    when scanning the ats/ directory).
    """
    file_key = source.split(":", 1)[1] if surface == "ats" else source
    try:
        override_loader.write_override(surface, file_key, recipe_to_dict(candidate))
        override_loader.reload()
    except Exception as exc:
        logger.exception("autoheal: adopting override for %s failed", source)
        _record_failure(conn, source)
        _audit(conn, source, surface, "rejected:write_failed", str(exc))
        return "rejected:write_failed"

    _audit(conn, source, surface, "adopted")
    conn.execute(
        "UPDATE source_health SET status = 'healthy', consecutive_breaks = 0, "
        "heal_attempts = 0, last_heal_at = ? WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()
    logger.info("autoheal: adopted %s override for source '%s'", surface, source)
    return "adopted"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_failure(conn: sqlite3.Connection, source: str) -> None:
    """Count a consumed heal attempt and start the backoff window."""
    conn.execute(
        "UPDATE source_health SET heal_attempts = heal_attempts + 1, last_heal_at = ? "
        "WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()


def _backoff_elapsed(last_heal_at: str | None, backoff_hours: float) -> bool:
    if not last_heal_at:
        return True
    try:
        last = datetime.fromisoformat(last_heal_at)
    except ValueError:
        return True
    now = datetime.fromisoformat(utc_now_iso())
    return (now - last) >= timedelta(hours=backoff_hours)


def _audit(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO heal_audit (source, surface, outcome, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, surface, outcome, detail, utc_now_iso()),
    )
    conn.commit()


__all__ = ["run_heal"]
