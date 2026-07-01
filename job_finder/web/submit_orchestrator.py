"""Submit orchestrator — mechanism-agnostic auto-submit spine (issue #604).

Single chokepoint for all real job-application submissions. Every submit goes
through submit_application_for(), which enforces config gating, idempotency,
target-URL safety, rate limiting, audit logging, and terminal-state transitions.

The actual submission mechanism is a swappable seam (submit_application), mirroring
the application_prepare.tailor_resume pattern. The default implementation is a
no-op returning 'not_wired' — the real MV3 extension sender lands in a separate
issue.

FRAUD SURFACE: This spine closes the approve-route fraud surface where jobs were
marked 'applied' with zero actual submission. The approve route now calls
submit_application_for BEFORE flipping pipeline_status, and only marks 'applied'
on a real 'submitted' outcome.
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from job_finder.json_utils import local_day_utc_window, utc_now_iso
from job_finder.web.direct_link import is_ats_or_careers_url

logger = logging.getLogger(__name__)

# SubmitResult outcome types
SubmitOutcome = Literal["disabled", "refused", "dispatched", "submitted", "failed", "not_wired"]


@dataclass
class SubmitResult:
    """Result of a submit attempt.

    Attributes:
        outcome: The final outcome (disabled, refused, dispatched, submitted, failed, not_wired)
        reason: Human-readable reason for refusal/failure (empty for success outcomes)
        application_id: The applications row id (for terminal-state write)
    """

    outcome: SubmitOutcome
    reason: str = ""
    application_id: int | None = None


def _submit_application(
    *,
    conn: sqlite3.Connection,
    config: dict,
    job: dict,
    application_row: dict,
) -> SubmitResult:
    """Default no-op submit implementation.

    The real MV3 extension sender will replace this in a separate issue.
    This default returns 'not_wired' to signal that no mechanism is connected.

    Args:
        conn: sqlite3 connection
        config: app config dict
        job: job dict
        application_row: applications row dict

    Returns:
        SubmitResult with outcome='not_wired'
    """
    return SubmitResult(outcome="not_wired", reason="Submit mechanism not wired")


# Module-level attribute for test monkeypatching (mirrors application_prepare.tailor_resume)
submit_application = _submit_application


def submit_application_for(
    conn: sqlite3.Connection, config: dict, application_row: dict
) -> SubmitResult:
    """Orchestrator chokepoint — submit a job application with full guard chain.

    Strict order of operations:
      1. Config gate (default OFF) → return DISABLED if not enabled
      2. Idempotency → refuse if job already 'submitted'
      3. Target-URL safety → refuse if apply_url is non-strict/aggregator
      4. Rate limit → refuse if daily cap exceeded
      5. Dispatch via submit_application seam
      6. Audit ledger write (append-only, includes refusals/failures)
      7. Terminal-state write (resolve_application to 'submitted' or 'submit_failed')

    Args:
        conn: sqlite3 connection
        config: app config dict
        application_row: applications row dict (must have id, job_id, form_mapping)

    Returns:
        SubmitResult with outcome and optional reason
    """
    application_id = application_row["id"]
    job_id = application_row["job_id"]

    # Fetch the job row
    job = conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (job_id,)).fetchone()
    if job is None:
        logger.error("Job not found for application %d: job_id=%s", application_id, job_id)
        return SubmitResult(
            outcome="refused", reason="Job not found", application_id=application_id
        )
    job = dict(job)

    # 1. Config gate (default OFF)
    auto_submit_config = config.get("application", {}).get("auto_submit", {})
    if not auto_submit_config.get("enabled", False):
        logger.debug("Auto-submit disabled (config gate)")
        return SubmitResult(
            outcome="disabled",
            reason="Auto-submit disabled in config",
            application_id=application_id,
        )

    # 2. Idempotency — refuse if this job already has a real send in the immutable ledger.
    # (Do NOT read applications.status: upsert_application resets it to 'pending' on re-prepare.)
    already_sent = conn.execute(
        "SELECT 1 FROM submit_attempts WHERE job_id = ? AND outcome IN ('submitted','dispatched') LIMIT 1",
        (job_id,),
    ).fetchone()
    if already_sent:
        logger.info("Job already submitted, refusing duplicate: job_id=%s", job_id)
        _write_submit_attempt(
            conn=conn,
            job_id=job_id,
            mechanism=auto_submit_config.get("mechanism"),
            apply_url=_extract_apply_url(application_row),
            target_confidence=_extract_target_confidence(job),
            outcome="refused",
            detail="Idempotency guard: job already submitted",
        )
        return SubmitResult(
            outcome="refused", reason="Job already submitted", application_id=application_id
        )

    # 3. Target-URL safety — refuse non-strict/aggregator URLs
    apply_url = _extract_apply_url(application_row)
    target_confidence = _extract_target_confidence(job)
    require_strict = auto_submit_config.get("require_strict_target", True)

    if require_strict:
        # Safety must gate on the apply_url we are actually about to dispatch — NOT on the
        # job's direct_url_confidence, which is an independent field (confused-deputy bug).
        # Trust 'strict' confidence only when the apply_url IS the strict direct_url.
        is_safe_target = bool(apply_url) and (
            is_ats_or_careers_url(apply_url)
            or (target_confidence == "strict" and apply_url == job.get("direct_url"))
        )
        if not is_safe_target:
            logger.warning(
                "Refusing non-strict target URL: job_id=%s, apply_url=%s, confidence=%s",
                job_id,
                apply_url,
                target_confidence,
            )
            _write_submit_attempt(
                conn=conn,
                job_id=job_id,
                mechanism=auto_submit_config.get("mechanism"),
                apply_url=apply_url,
                target_confidence=target_confidence,
                outcome="refused",
                detail=f"Target-URL safety: non-strict target (confidence={target_confidence})",
            )
            return SubmitResult(
                outcome="refused",
                reason=f"Non-strict target URL (confidence={target_confidence})",
                application_id=application_id,
            )

    # 4. Rate limit — reconstruct today's count from audit ledger
    daily_limit = auto_submit_config.get("daily_limit", 5)
    day_start, day_end = local_day_utc_window()
    today_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM submit_attempts "
        "WHERE occurred_at >= ? AND occurred_at < ? AND outcome IN ('dispatched','submitted')",
        (day_start, day_end),
    ).fetchone()["cnt"]

    if today_count >= daily_limit:
        logger.warning("Rate limit exceeded: %d/%d submissions today", today_count, daily_limit)
        _write_submit_attempt(
            conn=conn,
            job_id=job_id,
            mechanism=auto_submit_config.get("mechanism"),
            apply_url=apply_url,
            target_confidence=target_confidence,
            outcome="refused",
            detail=f"Rate limit: {today_count}/{daily_limit} submissions today",
        )
        return SubmitResult(
            outcome="refused",
            reason=f"Daily rate limit exceeded ({today_count}/{daily_limit})",
            application_id=application_id,
        )

    # 5. Dispatch via the seam
    mechanism = auto_submit_config.get("mechanism", "extension")
    logger.info("Dispatching submit: job_id=%s, mechanism=%s", job_id, mechanism)

    try:
        result = submit_application(
            conn=conn, config=config, job=job, application_row=application_row
        )
    except Exception as e:
        logger.exception("Submit mechanism raised exception: job_id=%s, error=%s", job_id, e)
        # 6. Audit ledger write for failure
        _write_submit_attempt(
            conn=conn,
            job_id=job_id,
            mechanism=mechanism,
            apply_url=apply_url,
            target_confidence=target_confidence,
            outcome="failed",
            detail=f"Submit mechanism exception: {e}",
        )
        # 7. Terminal-state write: submit_failed
        _resolve_to_terminal(conn, application_id, "submit_failed")
        return SubmitResult(
            outcome="failed",
            reason=f"Submit mechanism exception: {e}",
            application_id=application_id,
        )

    # 6. Audit ledger write for final outcome
    _write_submit_attempt(
        conn=conn,
        job_id=job_id,
        mechanism=mechanism,
        apply_url=apply_url,
        target_confidence=target_confidence,
        outcome=result.outcome,
        detail=result.reason or "",
    )

    # 7. Terminal-state write
    if result.outcome == "submitted":
        _resolve_to_terminal(conn, application_id, "submitted")
    elif result.outcome == "dispatched":
        _resolve_to_terminal(conn, application_id, "dispatched")
    elif result.outcome in ("failed", "not_wired"):
        _resolve_to_terminal(conn, application_id, "submit_failed")
    # For 'refused' or 'disabled', we leave the application pending (no terminal state)

    return SubmitResult(
        outcome=result.outcome, reason=result.reason, application_id=application_id
    )


def _extract_apply_url(application_row: dict) -> str | None:
    """Extract apply_url from application form_mapping."""
    form_mapping = application_row.get("form_mapping", {})
    if isinstance(form_mapping, dict):
        return form_mapping.get("apply_url")
    return None


def _extract_target_confidence(job: dict) -> str | None:
    """Extract direct_url_confidence from job row."""
    return job.get("direct_url_confidence")


def _write_submit_attempt(
    conn: sqlite3.Connection,
    job_id: str,
    mechanism: str | None,
    apply_url: str | None,
    target_confidence: str | None,
    outcome: str,
    detail: str,
) -> None:
    """Write an immutable audit ledger entry for a submit attempt.

    One INSERT + commit per attempt, including refusals and failures.
    This ledger is both the audit trail and the rate-limit denominator.
    """
    conn.execute(
        """INSERT INTO submit_attempts (job_id, mechanism, apply_url, target_confidence, outcome, detail, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, mechanism, apply_url, target_confidence, outcome, detail, utc_now_iso()),
    )
    conn.commit()


def _resolve_to_terminal(conn: sqlite3.Connection, application_id: int, status: str) -> None:
    """Resolve an application to a terminal state.

    This is a thin wrapper around db._applications.resolve_application,
    extended to support 'submitted' and 'submit_failed' statuses.
    """
    from job_finder.db._applications import resolve_application

    resolve_application(conn, application_id, status)
