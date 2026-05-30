"""Pipeline detection engine for job-finder.

Scans Gmail for rejection, interview, and application confirmation emails.
Matches emails to existing jobs using multi-signal confidence scoring.
Auto-updates pipeline status for high-confidence matches (3+ signals) and
queues low-confidence matches (1-2 signals) for manual review.

Follows the stale_detector.py pattern: creates its own SQLite connection
and is thread-safe for APScheduler background jobs.
"""

import logging

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
from job_finder.web.pipeline_detector._db import (
    _already_processed,
    _insert_detection,
    _load_active_jobs,
    _mark_processed,
)
from job_finder.web.pipeline_detector._gmail import (
    _fetch_pipeline_emails,
    _get_gmail_service,
)
from job_finder.web.pipeline_detector._processing import _process_email
from job_finder.web.pipeline_detector._signals import (
    _classify_email,
    _company_in_email,
    _extract_snippet,
    _sender_is_ats,
    _timing_ok,
    _title_in_email,
    score_match,
)

__all__ = [
    "ATS_DOMAINS",
    "CONFIRMATION_KEYWORDS",
    "CONFIRMATION_QUERY",
    "DETECTION_TYPE_TO_STATUS",
    "INACTIVE_STATUSES",
    "INTERVIEW_KEYWORDS",
    "INTERVIEW_QUERY",
    "QUERY_DETECTION_TYPES",
    "REJECTION_KEYWORDS",
    "REJECTION_QUERY",
    "SIGNAL_KEYWORDS",
    "TITLE_STOP_WORDS",
    "_already_processed",
    "_classify_email",
    "_company_in_email",
    "_extract_snippet",
    "_fetch_pipeline_emails",
    "_get_gmail_service",
    "_insert_detection",
    "_load_active_jobs",
    "_mark_processed",
    "_process_email",
    "_sender_is_ats",
    "_timing_ok",
    "_title_in_email",
    "run_pipeline_detection",
    "score_match",
]

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
