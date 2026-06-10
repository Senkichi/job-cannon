"""Autoheal heal pipeline — ASSEMBLE → GENERATE → [VALIDATE → ADOPT] (flag-gated).

Phase C / C3 scope: ASSEMBLE + GENERATE are implemented.  VALIDATE and ADOPT are
explicit no-op stubs; they will be filled in Phase C4 and C5 respectively.

**CARDINAL CONSTRAINT** — ``heal_enabled`` defaults to ``false`` in config.
``run_heal`` returns immediately when the flag is off.  Zero model calls, zero
DB writes, zero production effect.

Entry point::

    from job_finder.web.autoheal.heal_pipeline import run_heal
    run_heal(conn, config, source)

``source`` is the source-key string as stored in ``source_health.source``
(e.g. ``"linkedin"`` or ``"ats:lever"``).  Surface is inferred from the key:
any key starting with ``"ats:"`` is ``"ats"``; everything else is ``"email"``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal.codegen import generate_recipe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# C4 / C5 stub markers — replace these in Phase C4 (validator) and C5 (adopt)
# ---------------------------------------------------------------------------

#: Stub marker: VALIDATE stage not yet implemented (filled in C4).
_VALIDATE_STUB = "C4_NOT_YET_IMPLEMENTED"

#: Stub marker: ADOPT stage not yet implemented (filled in C5).
_ADOPT_STUB = "C5_NOT_YET_IMPLEMENTED"


def _validate_stub(candidate, surface: str, conn: sqlite3.Connection, config: dict) -> None:
    """C4 VALIDATE stub — no-op.  Replace in Phase C4 with real corpus replay."""
    # TODO(C4): call validator.validate(candidate, surface, corpus_samples, failing_samples,
    #           timeout_s=config.get('autoheal', {}).get('validate_timeout_s', 30))
    pass


def _adopt_stub(candidate, surface: str, source: str, conn: sqlite3.Connection) -> None:
    """C5 ADOPT stub — no-op.  Replace in Phase C5 with write_override + reload."""
    # TODO(C5): call override_loader.write_override(surface, source, candidate)
    #           then override_loader.reload()
    #           then reset consecutive_breaks + update status to healthy
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_surface(source: str) -> str:
    """Derive ``"ats"`` or ``"email"`` from the source key."""
    return "ats" if source.startswith("ats:") else "email"


def _get_health_row(conn: sqlite3.Connection, source: str) -> dict | None:
    """Return the source_health row for *source* as a dict, or None."""
    row = conn.execute(
        "SELECT source, surface, status, consecutive_breaks, baseline_yield, "
        "       last_signal, last_break_at, updated_at, heal_attempts, last_heal_at "
        "FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _is_backoff_elapsed(last_heal_at: str | None, backoff_hours: int) -> bool:
    """Return True when the backoff window has elapsed (or there was no previous heal)."""
    if last_heal_at is None:
        return True
    if backoff_hours <= 0:
        return True
    try:
        last_dt = datetime.fromisoformat(last_heal_at)
    except (ValueError, TypeError):
        return True
    threshold = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=backoff_hours)
    return last_dt <= threshold


def _write_audit(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Insert one row into ``heal_audit``. Commits."""
    conn.execute(
        "INSERT INTO heal_audit (source, surface, outcome, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, surface, outcome, detail, utc_now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# run_heal — public entry point
# ---------------------------------------------------------------------------


def run_heal(conn: sqlite3.Connection, config: dict, source: str) -> None:
    """Execute one heal attempt for *source* (flag-gated, never raises).

    Phase C3 scope:
    - ASSEMBLE: gather corpus inputs.
    - GENERATE: call model → candidate recipe.
    - VALIDATE: **stub** (C4).
    - ADOPT: **stub** (C5).

    On generate success: audit row ``candidate_generated``.
    On generate failure: audit row ``no_provider``.

    The function returns immediately (no model call, no DB write) when:
    - ``config.get('autoheal', {}).get('heal_enabled', False)`` is falsy, OR
    - ``source_health.status`` is not ``'degraded'``, OR
    - ``heal_attempts >= heal_max_attempts``, OR
    - the backoff window has not yet elapsed.

    Args:
        conn: Open SQLite connection (shared with ingestion caller).
        config: Application config dict.
        source: Source key as stored in ``source_health``.
    """
    autoheal_cfg = config.get("autoheal", {})

    # --- Cardinal gate: flag must be explicitly enabled ---
    if not autoheal_cfg.get("heal_enabled", False):
        return

    surface = _infer_surface(source)
    heal_max_attempts: int = autoheal_cfg.get("heal_max_attempts", 3)
    backoff_hours: int = autoheal_cfg.get("heal_backoff_hours", 24)

    # --- Load health row ---
    health = _get_health_row(conn, source)
    if health is None:
        logger.debug("autoheal run_heal: no health row for source=%s, skipping", source)
        return

    # --- Status gate ---
    if health["status"] != "degraded":
        logger.debug(
            "autoheal run_heal: source=%s status=%s (not degraded), skipping",
            source,
            health["status"],
        )
        return

    # --- Attempt exhaustion gate ---
    heal_attempts: int = health["heal_attempts"] or 0
    if heal_attempts >= heal_max_attempts:
        logger.info(
            "autoheal run_heal: source=%s heal_attempts=%d >= max=%d, exhausted",
            source,
            heal_attempts,
            heal_max_attempts,
        )
        return

    # --- Backoff gate ---
    if not _is_backoff_elapsed(health.get("last_heal_at"), backoff_hours):
        logger.info(
            "autoheal run_heal: source=%s within backoff window, skipping",
            source,
        )
        return

    # --- ASSEMBLE + GENERATE ---
    logger.info("autoheal run_heal: starting GENERATE for source=%s surface=%s", source, surface)
    candidate = generate_recipe(conn, config, source, surface)

    if candidate is None:
        logger.warning("autoheal run_heal: generate_recipe returned None for source=%s", source)
        _write_audit(conn, source, surface, "no_provider", detail="generate_recipe returned None")
        return

    # Audit: candidate generated successfully
    _write_audit(conn, source, surface, "candidate_generated")
    logger.info(
        "autoheal run_heal: candidate_generated for source=%s surface=%s type=%s",
        source,
        surface,
        type(candidate).__name__,
    )

    # --- VALIDATE stub (C4) ---
    _validate_stub(candidate, surface, conn, config)

    # --- ADOPT stub (C5) ---
    _adopt_stub(candidate, surface, source, conn)
