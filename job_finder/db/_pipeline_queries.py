"""Detection and pipeline-event queries for the pipeline_detections table."""

from __future__ import annotations

import sqlite3

from job_finder.json_utils import utc_now_iso


def get_pipeline_events(conn: sqlite3.Connection, dedup_key: str) -> list[dict]:
    """Return all pipeline events for a job, newest first."""
    rows = conn.execute(
        "SELECT id, job_id, from_status, to_status, timestamp, source, evidence "
        "FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC",
        (dedup_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_pending_detections(conn: sqlite3.Connection) -> list[dict]:
    """Return pending pipeline detections joined with job details.

    Queries pipeline_detections WHERE status = 'pending' ordered by
    created_at DESC. Joins with jobs table to include job title and company.

    Args:
        conn: Open sqlite3 connection.

    Returns:
        List of dicts with all detection fields plus job_title, job_company.
        job_title and job_company are None if job_id is NULL or job not found.
    """
    rows = conn.execute(
        """SELECT pd.id, pd.gmail_message_id, pd.detection_type, pd.job_id,
                  pd.confidence_score, pd.matched_signals, pd.snippet,
                  pd.email_subject, pd.email_from, pd.email_date,
                  pd.status, pd.created_at, pd.resolved_at,
                  j.title AS job_title,
                  j.company AS job_company,
                  j.pipeline_status AS job_pipeline_status
           FROM pipeline_detections pd
           LEFT JOIN jobs j ON pd.job_id = j.dedup_key
           WHERE pd.status = 'pending'
           ORDER BY pd.created_at DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_detection(
    conn: sqlite3.Connection,
    detection_id: int,
    resolution: str,
) -> None:
    """Update a pipeline detection's status to 'confirmed' or 'dismissed'.

    Sets resolved_at to the current timestamp.

    Args:
        conn: Open sqlite3 connection.
        detection_id: The detection's primary key.
        resolution: Either 'confirmed' or 'dismissed'.
    """
    _VALID_RESOLUTIONS = ("confirmed", "dismissed")
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(
            f"Invalid resolution: {resolution!r}. Must be one of: {', '.join(_VALID_RESOLUTIONS)}."
        )
    now = utc_now_iso()
    conn.execute(
        "UPDATE pipeline_detections SET status = ?, resolved_at = ? WHERE id = ?",
        (resolution, now, detection_id),
    )
    conn.commit()
