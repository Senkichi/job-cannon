"""Pipeline detection engine for job-finder.

Scans Gmail for rejection, interview, and application confirmation emails.
Matches emails to existing jobs using multi-signal confidence scoring.
Auto-updates pipeline status for high-confidence matches (3+ signals) and
queues low-confidence matches (1-2 signals) for manual review.

Follows the stale_detector.py pattern: creates its own SQLite connection
and is thread-safe for APScheduler background jobs.
"""

import logging
import sqlite3

from job_finder.db import update_pipeline_status
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.pipeline_detector._constants import (
    ATS_DOMAINS,
    CONFIRMATION_KEYWORDS,
    CONFIRMATION_QUERY,
    DETECTION_TYPE_TO_STATUS,
    INACTIVE_STATUSES,
    INTERVIEW_KEYWORDS,
    INTERVIEW_QUERY,
    QUERY_DETECTION_TYPES,
    REJECTION_KEYWORDS,
    REJECTION_QUERY,
    SIGNAL_KEYWORDS,
    TITLE_STOP_WORDS,
)
from job_finder.web.pipeline_detector._gmail import (
    _fetch_pipeline_emails,
    _get_gmail_service,
)
from job_finder.web.pipeline_detector._db import (
    _already_processed,
    _insert_detection,
    _load_active_jobs,
    _mark_processed,
)
from job_finder.web.pipeline_detector._signals import (
    _classify_email,
    _company_in_email,
    _extract_snippet,
    _sender_is_ats,
    _timing_ok,
    _title_in_email,
    score_match,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_pipeline_detection(db_path: str, config: dict) -> dict:
    """Scan Gmail for pipeline emails and process matches.

    Creates its own SQLite connection (thread-safe for APScheduler).

    Args:
        db_path: Path to the SQLite database file.
        config: Full JF_CONFIG dict.

    Returns:
        Summary dict with keys:
            emails_scanned (int): Total emails fetched and examined.
            auto_updated (int): Jobs auto-updated from high-confidence matches.
            queued (int): Emails queued for manual review (low confidence).
            skipped (int): Emails skipped (no match or already processed).
            errors (list[str]): Error messages encountered.
    """
    summary = {
        "emails_scanned": 0,
        "auto_updated": 0,
        "queued": 0,
        "skipped": 0,
        "errors": [],
    }

    with standalone_connection(db_path) as conn:
        try:
            service = _get_gmail_service(config)
            if service is None:
                logger.warning("Pipeline detection: Gmail service unavailable, skipping")
                summary["errors"].append("Gmail authentication failed")
                return summary

            emails = _fetch_pipeline_emails(service, lookback_days=3)
            summary["emails_scanned"] = len(emails)

            # Load all active jobs once to avoid repeated DB queries
            jobs = _load_active_jobs(conn)

            for email in emails:
                try:
                    result = _process_email(email, conn, jobs, config=config)
                    if result == "auto_updated":
                        summary["auto_updated"] += 1
                    elif result == "queued":
                        summary["queued"] += 1
                    else:
                        summary["skipped"] += 1
                except Exception as e:
                    msg = f"Error processing email {email.get('message_id', '?')}: {e}"
                    logger.warning(msg)
                    summary["errors"].append(msg)

            logger.info(
                "Pipeline detection: %d scanned, %d auto-updated, %d queued, %d skipped",
                summary["emails_scanned"],
                summary["auto_updated"],
                summary["queued"],
                summary["skipped"],
            )

        except Exception as e:
            logger.exception("Pipeline detection failed: %s", e)
            summary["errors"].append(str(e))
            try:
                conn.rollback()
            except Exception:
                logger.debug("conn.rollback() failed in pipeline detection", exc_info=True)

    return summary


# ---------------------------------------------------------------------------
# Core email processing
# ---------------------------------------------------------------------------


def _process_email(
    email: dict,
    conn: sqlite3.Connection,
    jobs: list[dict],
    config: dict | None = None,
) -> str:
    """Process a single email: classify, match, score, auto-update or queue.

    Processing steps:
    1. Check email_parse_log — skip if already processed.
    2. Verify detection_type is set — skip if None (unclassified).
    3. For each active job, compute score_match.
    4. Take the best match. If tied, prefer 'applied' status.
    5. score >= 3: auto-update pipeline_status, insert 'auto-applied' detection.
    6. score 1-2: insert 'pending' detection.
    7. score 0: skip (no record).
    8. Mark message_id in email_parse_log at first detection time.

    Args:
        email: Email dict with message_id, subject, body, from_address, date, detection_type.
        conn: Open sqlite3 connection.
        jobs: List of active job dicts (pre-loaded).
        config: Optional full JF_CONFIG dict for notification toggle gating.

    Returns:
        'auto_updated', 'queued', or 'skipped' describing the outcome.
    """
    message_id = email.get("message_id", "")
    detection_type = email.get("detection_type")

    # Step 1: Dedup check
    if _already_processed(conn, message_id):
        return "skipped"

    # Step 2: Must have a classification
    if detection_type is None:
        return "skipped"

    # Step 3: Score against all active jobs
    best_score = 0
    best_signals: list[str] = []
    best_job: dict | None = None

    for job in jobs:
        score, signals = score_match(email, job)
        if score > best_score:
            best_score = score
            best_signals = signals
            best_job = job
        elif score == best_score and score > 0 and best_job is not None:
            # Tiebreak: prefer 'applied' status
            if (
                job.get("pipeline_status") == "applied"
                and best_job.get("pipeline_status") != "applied"
            ):
                best_job = job
                best_signals = signals

    # Company signal is mandatory — without it, we can't confidently
    # attribute an email to a specific job
    if "company" not in best_signals:
        return "skipped"

    # Extract snippet for the detection record
    snippet = _extract_snippet(email.get("body", ""), detection_type)
    new_status = DETECTION_TYPE_TO_STATUS.get(detection_type, "applied")
    job_id = best_job["dedup_key"] if best_job else None

    if best_score >= 3:
        # High confidence: auto-update pipeline status
        if best_job is not None:
            update_pipeline_status(
                conn,
                best_job["dedup_key"],
                new_status,
                source="auto-detected",
            )

        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="auto-applied",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "auto_updated"

    elif best_score >= 1:
        # Low confidence: queue for review
        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="pending",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "queued"

    else:
        # score == 0: silently drop — no record
        return "skipped"
