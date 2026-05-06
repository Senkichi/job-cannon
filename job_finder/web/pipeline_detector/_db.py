"""SQLite helpers for the pipeline detector.

Four functions read/write the three tables the detector touches:

  - ``_load_active_jobs(conn)``        : SELECT from jobs (excluding
                                          INACTIVE_STATUSES rows)
  - ``_already_processed(conn, mid)``  : SELECT 1 FROM email_parse_log
  - ``_mark_processed(conn, mid, ...)``: INSERT OR IGNORE INTO
                                          email_parse_log
  - ``_insert_detection(conn, mid, ...)``: INSERT OR IGNORE INTO
                                          pipeline_detections

All functions are called from inside the orchestrator (``_process_email``
in ``__init__.py``) which holds the live ``standalone_connection``. None
of these helpers manage their own connection; they just take the one
they're given. This is the same shape the legacy ``pipeline_detector.py``
monolith used.
"""

import json
import logging
import sqlite3
from datetime import datetime

from job_finder.web.pipeline_detector._constants import INACTIVE_STATUSES

logger = logging.getLogger(__name__)


def _load_active_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Load all jobs that are NOT in inactive pipeline statuses.

    Used to avoid repeated DB queries during email processing.

    Args:
        conn: Open sqlite3 connection.

    Returns:
        List of job dicts for active jobs.
    """
    placeholders = ",".join("?" * len(INACTIVE_STATUSES))
    try:
        rows = conn.execute(
            f"SELECT dedup_key, title, company, location, first_seen, pipeline_status"
            f" FROM jobs WHERE pipeline_status NOT IN ({placeholders})",
            tuple(INACTIVE_STATUSES),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("_load_active_jobs failed (DB not ready?): %s", e)
        return []
    return [dict(row) for row in rows]


def _already_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    """Check if a Gmail message ID has already been processed.

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID to check.

    Returns:
        True if already in email_parse_log, False otherwise.
    """
    row = conn.execute(
        "SELECT 1 FROM email_parse_log WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def _mark_processed(
    conn: sqlite3.Connection,
    message_id: str,
    sender: str,
    detection_type: str | None,
) -> None:
    """Mark a Gmail message ID as processed in email_parse_log.

    Uses INSERT OR IGNORE so re-processing the same ID does not fail.
    Called at FIRST DETECTION TIME (not just at confirm/dismiss).

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID.
        sender: From address of the email.
        detection_type: Classification result or None.
    """
    now = datetime.now().isoformat()
    jobs_found = 1 if detection_type is not None else 0
    try:
        conn.execute(
            """INSERT OR IGNORE INTO email_parse_log
               (message_id, sender, processed_at, jobs_found, error)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, sender, now, jobs_found, None),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to mark message as processed: %s", e)


def _insert_detection(
    conn: sqlite3.Connection,
    message_id: str,
    detection_type: str,
    job_id: str | None,
    *,
    score: int,
    signals: list[str],
    snippet: str,
    email_subject: str,
    email_from: str,
    email_date: str,
    status: str,
) -> None:
    """Insert a record into pipeline_detections.

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID.
        detection_type: 'rejection', 'interview', or 'confirmation'.
        job_id: Matched job dedup_key, or None.
        score: Confidence score 0-4.
        signals: List of matched signal names.
        snippet: Email body snippet (max 200 chars).
        email_subject: Email subject.
        email_from: Email from address.
        email_date: Email date as ISO string.
        status: 'pending', 'auto-applied', etc.
    """
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO pipeline_detections
           (gmail_message_id, detection_type, job_id, confidence_score,
            matched_signals, snippet, email_subject, email_from,
            email_date, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            detection_type,
            job_id,
            score,
            json.dumps(signals),
            snippet,
            email_subject,
            email_from,
            email_date,
            status,
            now,
        ),
    )
    conn.commit()
