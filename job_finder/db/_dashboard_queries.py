"""Read-only aggregate queries for the dashboard and pipeline views."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime


def get_dashboard_stats(conn: sqlite3.Connection) -> dict:
    """Return stat card data for the Dashboard page.

    Returns:
        dict with keys:
            total_jobs (int): all jobs in DB
            new_today (int): jobs where first_seen date == today
            reviewing_count (int): jobs where pipeline_status == 'reviewing'
            by_status (dict[str, int]): count per pipeline_status, active only
            stale_count (int): jobs where is_stale == 1
            pending_detections (int): pipeline_detections where status == 'pending' (0 if table missing)
    """
    today_prefix = date.today().isoformat()  # e.g. "2026-03-10"

    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE first_seen >= ? AND first_seen < ?",
        (f"{today_prefix}T00:00:00", f"{today_prefix}T23:59:60"),
    ).fetchone()[0]
    reviewing_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'reviewing'",
    ).fetchone()[0]

    # Active statuses: exclude archived, withdrawn
    by_status_rows = conn.execute(
        """SELECT pipeline_status, COUNT(*) as cnt FROM jobs
           WHERE pipeline_status NOT IN ('archived', 'withdrawn')
           GROUP BY pipeline_status
           ORDER BY cnt DESC"""
    ).fetchall()
    by_status = {row["pipeline_status"]: row["cnt"] for row in by_status_rows}

    stale_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_stale = 1",
    ).fetchone()[0]

    # Pending pipeline detections (0 if table not yet created)
    try:
        pending_detections = conn.execute(
            "SELECT COUNT(*) FROM pipeline_detections WHERE status = 'pending'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pending_detections = 0

    return {
        "total_jobs": total_jobs,
        "new_today": new_today,
        "reviewing_count": reviewing_count,
        "by_status": by_status,
        "stale_count": stale_count,
        "pending_detections": pending_detections,
    }


def get_recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list:
    """Return recent ingestion run records ordered newest-first.

    Args:
        conn: Open sqlite3 connection.
        limit: Max number of runs to return.

    Returns:
        List of dicts with keys: id, source, jobs_fetched, jobs_new, jobs_scored, timestamp.
    """
    rows = conn.execute(
        "SELECT id, timestamp, source, jobs_fetched, jobs_new, jobs_scored "
        "FROM runs ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_pipeline_summary(conn: sqlite3.Connection) -> dict:
    """Return job count per pipeline_status, excluding statuses with 0 jobs.

    Returns:
        dict mapping pipeline_status -> count (only non-zero counts).
    """
    rows = conn.execute(
        """SELECT pipeline_status, COUNT(*) as cnt FROM jobs
           GROUP BY pipeline_status
           HAVING cnt > 0"""
    ).fetchall()
    return {row["pipeline_status"]: row["cnt"] for row in rows}


def get_jobs_by_status(conn: sqlite3.Connection) -> dict:
    """Return all jobs grouped by pipeline_status.

    Each job dict includes dedup_key, title, company, score, salary_min,
    salary_max, location, pipeline_status, first_seen, and days_in_stage
    (days since the job entered its current pipeline stage, based on the most
    recent pipeline_events record with matching to_status, falling back to
    first_seen).

    Returns:
        dict mapping pipeline_status (str) -> list of job dicts
    """
    rows = conn.execute(
        """SELECT j.dedup_key, j.title, j.company, j.score,
                  j.salary_min, j.salary_max, j.location,
                  j.pipeline_status, j.first_seen,
                  (
                      SELECT pe.timestamp
                      FROM pipeline_events pe
                      WHERE pe.job_id = j.dedup_key
                        AND pe.to_status = j.pipeline_status
                      ORDER BY pe.timestamp DESC
                      LIMIT 1
                  ) AS stage_entered_at
           FROM jobs j
           ORDER BY j.score DESC"""
    ).fetchall()

    now = datetime.now()
    result: dict = {}
    for row in rows:
        job = dict(row)
        # Compute days_in_stage from stage_entered_at or first_seen
        entered_str = job.pop("stage_entered_at") or job["first_seen"]
        try:
            entered_dt = datetime.fromisoformat(entered_str).replace(tzinfo=None)
        except (ValueError, TypeError):
            entered_dt = now
        days_in_stage = max(0, (now - entered_dt).days)
        job["days_in_stage"] = days_in_stage

        status = job["pipeline_status"] or "discovered"
        result.setdefault(status, []).append(job)

    return result


def get_distinct_locations(conn: sqlite3.Connection) -> list[str]:
    """Return normalized, lower-case-deduped location values for the filter
    dropdown.

    Sources from per-entry ``locations_raw`` (JSON array per job), NOT from
    the merged ``location`` column. This avoids the pollution where every
    unique multi-location *combination* (e.g. "Remote, NYC, SF" vs.
    "NYC, SF, Remote") becomes its own dropdown entry.

    Each ``locations_raw`` entry is run through ``normalize_location``
    (trim / collapse whitespace / drop placeholders) and the result is
    deduplicated case-insensitively. Display uses the first-seen casing.
    """
    import json

    from job_finder.web.location_normalizer import normalize_location

    rows = conn.execute(
        "SELECT locations_raw FROM jobs "
        "WHERE locations_raw IS NOT NULL AND locations_raw != ''"
    ).fetchall()

    by_lower_key: dict[str, str] = {}
    for (raw,) in rows:
        try:
            locs = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(locs, list):
            continue
        for loc in locs:
            if not isinstance(loc, str):
                continue
            normalized = normalize_location(loc)
            if normalized is None:
                continue
            key = normalized.lower()
            by_lower_key.setdefault(key, normalized)

    return sorted(by_lower_key.values(), key=str.lower)


def get_recent_activity(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    """Return recent user_activity rows ordered newest-first.

    Args:
        conn: Open sqlite3 connection.
        limit: Max number of rows to return.

    Returns:
        List of dicts with keys: id, action, entity_id, metadata, occurred_at.
        Returns empty list if user_activity table does not exist (graceful
        pre-migration handling, same pattern as get_recent_pipeline_events).
    """
    try:
        rows = conn.execute(
            "SELECT id, action, entity_id, metadata, occurred_at "
            "FROM user_activity ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        # Gracefully handle missing table (pre-migration); re-raise other errors.
        if "no such table" in str(exc).lower():
            return []
        raise


def get_recent_pipeline_events(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Return recent pipeline status change events, newest first.

    Joins with jobs to include job title and company for display.

    Args:
        conn: Open sqlite3 connection.
        limit: Max number of events to return.

    Returns:
        List of dicts with: id, job_id, from_status, to_status, timestamp,
        source, job_title, job_company.
        Returns empty list if pipeline_events table does not exist.
    """
    try:
        rows = conn.execute(
            """SELECT pe.id, pe.job_id, pe.from_status, pe.to_status,
                      pe.timestamp, pe.source,
                      j.title AS job_title,
                      j.company AS job_company
               FROM pipeline_events pe
               LEFT JOIN jobs j ON pe.job_id = j.dedup_key
               ORDER BY pe.timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        # Gracefully handle missing table (pre-migration); re-raise other errors.
        if "no such table" in str(exc).lower():
            return []
        raise
