"""Autoheal heal pipeline — ASSEMBLE → GENERATE → VALIDATE → ADOPT.

``run_heal`` is the single entry point, fired from the post-ingestion
detection pass and the daily health-check retry sweep. Everything is gated on
``config['autoheal']['heal_enabled']`` (default true since D6 — the
"never come back and fix the parser" promise holds out of the box; set to
false to disable), a ``DEGRADED`` source_health status, the attempt cap, and
the backoff window. Keyless instances simply audit ``no_provider`` once per
backoff window and consume no attempt — default-on costs nothing without a
configured provider.

Phase D adds: re-break rollback (a degraded source with an adopted override
rolls it back before re-healing), episodic attempt semantics (one generate =
one attempt; reset only at episode boundaries — see health_monitor),
``no_provider`` backoff, and (D4) the careers surface — per-company
``careers:<hostname>`` sources heal through the same pipeline into
``heal_overrides/careers/``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import codegen, override_loader, surface_for_source, validator
from job_finder.web.autoheal.audit import record_audit
from job_finder.web.autoheal.recipe_schema import recipe_to_dict
from job_finder.web.model_provider import ProviderCascadeExhaustedError

logger = logging.getLogger(__name__)


def run_heal(conn: sqlite3.Connection, config: dict, source: str) -> str | None:
    """Attempt to heal one DEGRADED source. Returns the audit outcome or None.

    Gates (all must hold, else returns None without a model call):
    - ``autoheal.heal_enabled`` is true (defensive read; default true since D6)
    - source_health.status == 'degraded'
    - heal_attempts < heal_max_attempts
    - backoff window elapsed since last_heal_at
    """
    autoheal_cfg = config.get("autoheal", {}) or {}
    if not autoheal_cfg.get("heal_enabled", True):
        return None

    row = conn.execute(
        "SELECT status, heal_attempts, last_heal_at FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None or row[0] != "degraded":
        return None

    # Re-break guard: a degraded source that still has an adopted override means
    # the heal went bad on live traffic — roll it back before anything else
    # (BEFORE the attempt-cap check: a bad override must come off even when
    # attempts are exhausted).
    from job_finder.web.autoheal import rollback as _rollback

    if override_loader.recipe_for(source) is not None:
        _rollback.rollback_override(conn, source, "rebreak", new_status="degraded")

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

    surface = surface_for_source(source)

    # --- ASSEMBLE → GENERATE ---
    inputs = codegen.assemble_inputs(conn, source, surface)
    try:
        candidate = codegen.generate_recipe(conn, config, source, surface, inputs=inputs)
    except ProviderCascadeExhaustedError as exc:
        record_audit(conn, source, surface, "no_provider", str(exc))
        # Start the backoff window WITHOUT consuming an attempt: the daily
        # retry sweep then re-fires at most once per backoff period, and
        # keyless users keep their full budget for when a provider appears.
        conn.execute(
            "UPDATE source_health SET last_heal_at = ? WHERE source = ?",
            (utc_now_iso(), source),
        )
        conn.commit()
        return "no_provider"

    if candidate is None:
        _record_failure(conn, source, max_attempts)
        record_audit(conn, source, surface, "rejected:generation_failed")
        return "rejected:generation_failed"

    record_audit(conn, source, surface, "candidate_generated")

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
        _record_failure(conn, source, max_attempts)
        record_audit(conn, source, surface, f"rejected:{reason}")
        return f"rejected:{reason}"

    record_audit(conn, source, surface, "validated")

    # --- ADOPT (C5) — write the override, hot-swap, reset health ---
    return _adopt_stage(conn, source, surface, candidate, max_attempts, autoheal_cfg)


# ---------------------------------------------------------------------------
# ADOPT
# ---------------------------------------------------------------------------


def _adopt_stage(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    candidate,
    max_attempts: int,
    autoheal_cfg: dict | None = None,
) -> str:
    """Write the validated recipe as an override, hot-swap the cache, reset health.

    Override files are keyed by the loader's file layout: email uses the
    label verbatim; prefixed sources (``ats:<platform>``,
    ``careers:<hostname>``) strip the prefix — the loader re-adds it when
    scanning the surface directory. Careers file keys are NTFS-safe by
    construction (I5: hostname only, no port, no colon).

    Attempt semantics (plan invariant I1): one generate = one consumed attempt,
    success or failure — adopting does NOT reset ``heal_attempts`` (a
    bad-but-validated recipe must not grant itself a fresh budget every
    adoption). The counter resets only at episode boundaries (positive yield
    with no override active — see health_monitor) or the 30-day hygiene sweep.
    ``shadow_legacy_wins`` is zeroed for the newborn override (I2).
    """
    file_key = source.split(":", 1)[1] if ":" in source else source
    try:
        override_loader.write_override(surface, file_key, recipe_to_dict(candidate))
        override_loader.reload()
    except Exception as exc:
        logger.exception("autoheal: adopting override for %s failed", source)
        _record_failure(conn, source, max_attempts)
        record_audit(conn, source, surface, "rejected:write_failed", str(exc))
        return "rejected:write_failed"

    record_audit(conn, source, surface, "adopted")

    # --- Upstream contribution bundle (D5) — consent-gated, local-only.
    # Adoption stands regardless: a bundle failure is audited, never raised. ---
    bundle = None
    try:
        from job_finder.web.autoheal import upstream_reporter

        bundle = upstream_reporter.build_bundle(conn, source, surface, recipe_to_dict(candidate))
        upstream_reporter.write_bundle(bundle)
    except Exception as exc:
        logger.exception("autoheal: contribution bundle for %s failed", source)
        record_audit(conn, source, surface, "contrib_failed", str(exc))

    # Maintainer auto-PR (default-off; remote-only; never raises).
    if bundle is not None:
        from job_finder.web.autoheal import upstream_reporter

        pr_outcome = upstream_reporter.maintainer_pr(bundle, autoheal_cfg or {})
        if pr_outcome is not None:
            record_audit(conn, source, surface, pr_outcome)

    conn.execute(
        "UPDATE source_health SET status = 'healthy', consecutive_breaks = 0, "
        "heal_attempts = heal_attempts + 1, shadow_legacy_wins = 0, last_heal_at = ? "
        "WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()
    _audit_cap_if_exhausted(conn, source, max_attempts)
    logger.info("autoheal: adopted %s override for source '%s'", surface, source)
    return "adopted"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_failure(conn: sqlite3.Connection, source: str, max_attempts: int) -> None:
    """Count a consumed heal attempt and start the backoff window."""
    conn.execute(
        "UPDATE source_health SET heal_attempts = heal_attempts + 1, last_heal_at = ? "
        "WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()
    _audit_cap_if_exhausted(conn, source, max_attempts)


def _audit_cap_if_exhausted(conn: sqlite3.Connection, source: str, max_attempts: int) -> None:
    """Audit ``cap_exhausted`` when an attempt increment reached the cap.

    Called after EVERY increment (failure or adopt) — increments are monotonic
    within an episode, so this fires exactly once per episode (I1), including
    episodes whose budget drains through the adopt→re-break→re-heal cycle.
    """
    row = conn.execute(
        "SELECT heal_attempts FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    if row is not None and int(row[0] or 0) >= max_attempts:
        record_audit(conn, source, surface_for_source(source), "cap_exhausted")


def _backoff_elapsed(last_heal_at: str | None, backoff_hours: float) -> bool:
    if not last_heal_at:
        return True
    try:
        last = datetime.fromisoformat(last_heal_at)
    except ValueError:
        return True
    now = datetime.fromisoformat(utc_now_iso())
    return (now - last) >= timedelta(hours=backoff_hours)


__all__ = ["run_heal"]
