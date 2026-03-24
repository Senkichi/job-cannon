"""SQLite persistence layer for job deduplication and run history."""

import json
import re
import sqlite3
from datetime import datetime, date
from typing import Optional

from job_finder.models import Job
from job_finder.json_utils import safe_json_load, utc_now_iso


# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() and get_filtered_jobs() which
# return complete row dicts to templates and callers.
_JOBS_ALL_COLUMNS = (
    "dedup_key, title, company, location, sources, source_urls, source_id, "
    "salary_min, salary_max, description, first_seen, last_seen, score, "
    "score_breakdown, user_interest, pipeline_status, posted_date, notes, "
    "haiku_score, haiku_summary, sonnet_score, fit_analysis, jd_full, is_stale, "
    "company_id, comp_data_json, enrichment_tier, rejection_reviewed, "
    "locations_raw, description_reformatted, expiry_checked_at"
)

# Columns read by upsert_job() for merge logic — only what the UPDATE branch needs.
_UPSERT_MERGE_COLUMNS = (
    "sources, source_urls, locations_raw, description, jd_full, pipeline_status"
)


def _merge_description(existing: Optional[str], new: Optional[str]) -> Optional[str]:
    """Merge two description strings — single source of truth for description merge logic.

    Also used iteratively by dedup_normalizer._merge_descriptions for N-way merges.

    Rules:
    - If either is None/empty, return the other.
    - If one is a substring of the other, return the longer one.
    - If they are substantially different, append new to existing with separator.

    Args:
        existing: Current description stored in DB (may be None).
        new: Incoming description from the new Job (may be None).

    Returns:
        Merged description string, or None if both are empty/None.
    """
    if not existing and not new:
        return None
    if not existing:
        return new
    if not new:
        return existing
    if existing == new:
        return existing
    # Substring check: keep the longer one if one contains the other
    if new in existing or existing in new:
        return existing if len(existing) >= len(new) else new
    # Substantially different — append with separator
    return f"{existing}\n\n---\n\n{new}"


def upsert_job(conn: sqlite3.Connection, job: Job) -> bool:
    """Insert or update a job. Returns True if it's new.

    Deduplication: if the same job (by dedup_key) already exists,
    merge source URLs, locations (Remote/Hybrid first), and descriptions
    (substring dedup -- keep longer; append different content with separator).
    Keep first_seen from the original row.

    INSERT branch initializes locations_raw as a JSON array with the
    initial location so the UPDATE merge logic always has a base array.

    Args:
        conn: Open sqlite3 connection.
        job: Job object to insert or update.

    Returns:
        True if the job is new (inserted), False if existing (updated).
    """
    existing = conn.execute(
        f"SELECT {_UPSERT_MERGE_COLUMNS} FROM jobs WHERE dedup_key = ?",
        (job.dedup_key,),
    ).fetchone()

    now = utc_now_iso()

    if existing:
        # Merge sources
        sources = safe_json_load(existing["sources"], default=[])
        urls = safe_json_load(existing["source_urls"], default=[])
        if job.source not in sources:
            sources.append(job.source)
        if job.source_url and job.source_url not in urls:
            urls.append(job.source_url)

        # Smart location merge: maintain locations_raw array (Remote/Hybrid first)
        existing_locs_raw = existing["locations_raw"]
        try:
            locs_list = json.loads(existing_locs_raw) if existing_locs_raw else []
        except (json.JSONDecodeError, TypeError):
            locs_list = []
        if not isinstance(locs_list, list):
            locs_list = [locs_list] if locs_list else []

        new_loc = job.location or ""
        if new_loc and new_loc not in locs_list:
            if re.search(r"\b(remote|hybrid)\b", new_loc, re.IGNORECASE):
                locs_list.insert(0, new_loc)
            else:
                locs_list.append(new_loc)

        # Build merged location string: ordered, deduplicated
        merged_location = ", ".join(dict.fromkeys(locs_list))

        # Smart description merge: keep longer; append different content
        merged_description = _merge_description(
            existing["description"], job.description
        )

        # Eager promotion: if jd_full is NULL and merged description is
        # substantial, promote it so enrichment has a baseline to beat.
        jd_full_clause = ""
        jd_full_value = ()
        if not existing["jd_full"] and merged_description and len(merged_description) > 200:
            jd_full_clause = ", jd_full = ?"
            jd_full_value = (merged_description[:8000],)

        conn.execute(
            f"""UPDATE jobs SET
                sources = ?, source_urls = ?, last_seen = ?,
                score = ?, score_breakdown = ?,
                salary_min = COALESCE(?, salary_min),
                salary_max = COALESCE(?, salary_max),
                description = ?,
                locations_raw = ?,
                location = ?{jd_full_clause}
            WHERE dedup_key = ?""",
            (
                json.dumps(sources),
                json.dumps(urls),
                now,
                job.score,
                json.dumps(job.score_breakdown),
                job.salary_min,
                job.salary_max,
                merged_description,
                json.dumps(locs_list),
                merged_location,
                *jd_full_value,
                job.dedup_key,
            ),
        )
        conn.commit()
        # Auto-reopen: if an archived job re-appears in ingestion, treat
        # re-appearance as proof the job is live again (per CONTEXT.md decision)
        if existing["pipeline_status"] == "archived":
            update_pipeline_status(
                conn, job.dedup_key, "discovered",
                source="ingestion", evidence="re_appeared",
            )
        return False
    else:
        # Use the email date as first_seen when available (Gmail-sourced jobs).
        # SerpAPI jobs have no email date, so they use the current ingestion time.
        first_seen = job.posted_date.isoformat() if job.posted_date else now
        # Initialize locations_raw as a JSON array with the initial location
        initial_locs = [job.location] if job.location else []
        # Eager promotion: if description is substantial, set jd_full immediately
        # so enrichment always has a quality baseline to beat.
        initial_jd_full = None
        if job.description and len(job.description) > 200:
            initial_jd_full = job.description[:8000]
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 source_id, salary_min, salary_max, description,
                 first_seen, last_seen, score, score_breakdown, locations_raw,
                 jd_full)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.dedup_key,
                job.title,
                job.company,
                job.location,
                json.dumps([job.source]),
                json.dumps([job.source_url]),
                job.source_id,
                job.salary_min,
                job.salary_max,
                job.description,
                first_seen,
                now,
                job.score,
                json.dumps(job.score_breakdown),
                json.dumps(initial_locs),
                initial_jd_full,
            ),
        )
        conn.commit()
        return True


def log_run(
    conn: sqlite3.Connection, source: str, fetched: int, new: int, scored: int
) -> None:
    """Log a pipeline run for auditing.

    Args:
        conn: Open sqlite3 connection.
        source: Source label (e.g., "gmail", "serpapi").
        fetched: Number of jobs fetched.
        new: Number of new jobs inserted.
        scored: Number of jobs scored.
    """
    conn.execute(
        "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
        (utc_now_iso(), source, fetched, new, scored),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Module-level DB functions for Flask views (use sqlite3.Connection directly)
# ---------------------------------------------------------------------------


def persist_haiku_score(
    conn: sqlite3.Connection,
    dedup_key: str,
    haiku_score: float,
    haiku_summary: str,
) -> None:
    """Persist Haiku scoring results for a job.

    Single point of truth for the haiku_score UPDATE statement.
    Called from scoring_orchestrator, backfill_enrichment, and scoring_evaluator.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        haiku_score: Numeric score from Haiku fast-filter.
        haiku_summary: Summary text from Haiku evaluation.
    """
    conn.execute(
        "UPDATE jobs SET haiku_score = ?, haiku_summary = ? WHERE dedup_key = ?",
        (haiku_score, haiku_summary, dedup_key),
    )
    conn.commit()


def persist_sonnet_score(
    conn: sqlite3.Connection,
    dedup_key: str,
    sonnet_score: float,
    fit_analysis: str,
) -> None:
    """Persist Sonnet evaluation results for a job.

    Single point of truth for the sonnet_score UPDATE statement.
    Called from scoring_orchestrator, backfill_enrichment, and scoring_evaluator.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        sonnet_score: Numeric score from Sonnet deep evaluation.
        fit_analysis: JSON string containing fit analysis details.
    """
    conn.execute(
        "UPDATE jobs SET sonnet_score = ?, fit_analysis = ? WHERE dedup_key = ?",
        (sonnet_score, fit_analysis, dedup_key),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, dedup_key: str) -> Optional[dict]:
    """Return a single job by dedup_key, or None if not found.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Job as dict with all columns, or None if not found.
    """
    row = conn.execute(
        f"SELECT {_JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def load_job_context(conn: sqlite3.Connection, dedup_key: str) -> Optional[dict]:
    """Load the standard job context bundle: job + resume_history + prep_row.

    Shared helper for expand, rescore, paste_jd, and quick_apply routes.
    Always fetches all three -- no flags/options per locked decision.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Dict with keys 'job', 'resume_history', 'prep_row', or None if job not found.
    """
    job = get_job(conn, dedup_key)
    if job is None:
        return None

    resume_history = conn.execute(
        "SELECT id, job_id, status, doc_url, error_msg, generated_at, model, generation_type, validation_report "
        "FROM resume_generations WHERE job_id = ? ORDER BY generated_at DESC",
        (dedup_key,),
    ).fetchall()

    prep_row = conn.execute(
        "SELECT status, company_brief, predicted_questions, gap_mitigation, "
        "questions_to_ask, error_msg "
        "FROM interview_preps WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (dedup_key,),
    ).fetchone()

    return {
        "job": job,
        "resume_history": resume_history,
        "prep_row": prep_row,
    }


def get_dashboard_stats(conn: sqlite3.Connection) -> dict:
    """Return stat card data for the Dashboard page.

    Returns:
        dict with keys:
            total_jobs (int): all jobs in DB
            new_today (int): jobs where first_seen date == today
            reviewing_count (int): jobs where pipeline_status == 'reviewing'
            by_status (dict[str, int]): count per pipeline_status, active only
    """
    today_prefix = date.today().isoformat()  # e.g. "2026-03-10"

    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE first_seen LIKE ?",
        (f"{today_prefix}%",),
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
        List of dicts with keys: id, source, jobs_fetched, jobs_new, timestamp.
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


def update_pipeline_status(
    conn: sqlite3.Connection,
    dedup_key: str,
    new_status: str,
    source: str = "manual",
    evidence: str = "",
) -> None:
    """Update a job's pipeline_status and log a pipeline_events record.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        new_status: The target pipeline status to move the job to.
        source: Who triggered the move ('manual', 'email', 'ai', etc.).
        evidence: Optional evidence string describing what triggered the change
            (e.g., "lever_api 404"). Defaults to empty string.

    Raises:
        ValueError: If new_status is not a recognized pipeline status.
    """
    from job_finder.constants import VALID_PIPELINE_STATUSES

    if new_status not in VALID_PIPELINE_STATUSES:
        raise ValueError(
            f"Invalid pipeline status: {new_status!r}. "
            f"Must be one of: {sorted(VALID_PIPELINE_STATUSES)}"
        )

    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return  # Job not found — no-op

    from_status = row["pipeline_status"]
    if from_status == new_status:
        return  # Already at this status — skip duplicate event insertion

    now = utc_now_iso()

    conn.execute(
        "UPDATE jobs SET pipeline_status = ? WHERE dedup_key = ?",
        (new_status, dedup_key),
    )
    conn.execute(
        """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (dedup_key, from_status, new_status, now, source, evidence),
    )
    conn.commit()


def get_filtered_jobs(
    conn: sqlite3.Connection,
    status: Optional[str | list[str]] = None,
    location: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    salary_min: Optional[int] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: str = "score",
    sort_dir: str = "DESC",
    limit: int = 100,
    hide_stale: bool = False,
) -> list[dict]:
    """Return jobs matching the given filters, sorted and limited.

    All filters are optional; passing None skips that filter.
    status accepts a single string or a list of strings for multi-select
    filtering (builds WHERE pipeline_status IN (?, ?, ...) clause).
    sort_by is validated against an allowlist to prevent SQL injection.
    When sort_by is "score", uses COALESCE(sonnet_score, haiku_score, score)
    to prefer the best available AI score over the heuristic score.
    """
    allowed_sort_cols = {
        "score",
        "title",
        "company",
        "location",
        "first_seen",
        "salary_min",
        "salary_max",
        "pipeline_status",
        "haiku_score",
        "sonnet_score",
    }
    if sort_by not in allowed_sort_cols:
        sort_by = "score"
    sort_dir = "DESC" if sort_dir.upper() != "ASC" else "ASC"

    # When sorting by score, use best available AI score (sonnet > haiku > heuristic)
    if sort_by == "score":
        score_expr = f"COALESCE(sonnet_score, haiku_score, score) {sort_dir}"
    else:
        score_expr = f"{sort_by} {sort_dir}"

    # Deprioritize archived/withdrawn jobs (push to bottom) when viewing all statuses.
    # When the user explicitly filters by a specific status, skip deprioritization
    # so that e.g. "show me all archived" sorts purely by score.
    if not status:
        order_expr = (
            "CASE WHEN pipeline_status IN ('archived', 'withdrawn') THEN 1 ELSE 0 END, "
            + score_expr
        )
    else:
        order_expr = score_expr

    conditions: list[str] = []
    params: list = []

    if status:
        if isinstance(status, list):
            placeholders = ", ".join("?" * len(status))
            conditions.append(f"pipeline_status IN ({placeholders})")
            params.extend(status)
        else:
            conditions.append("pipeline_status = ?")
            params.append(status)
    if location:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")
    if min_score is not None:
        conditions.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        conditions.append("score <= ?")
        params.append(max_score)
    if salary_min is not None:
        conditions.append("(salary_min >= ? OR salary_max >= ?)")
        params.extend([salary_min, salary_min])
    if source:
        conditions.append("sources LIKE ?")
        params.append(f'%"{source}"%')
    if date_from:
        conditions.append("first_seen >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("first_seen <= ?")
        params.append(date_to)
    if hide_stale:
        conditions.append("is_stale = 0")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT {_JOBS_ALL_COLUMNS} FROM jobs {where_clause} ORDER BY {order_expr} LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_pipeline_events(conn: sqlite3.Connection, dedup_key: str) -> list[dict]:
    """Return all pipeline events for a job, newest first."""
    rows = conn.execute(
        "SELECT id, job_id, from_status, to_status, timestamp, source, evidence "
        "FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC",
        (dedup_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_distinct_locations(conn: sqlite3.Connection) -> list[str]:
    """Return distinct non-empty location values for filter dropdown."""
    rows = conn.execute(
        "SELECT DISTINCT location FROM jobs WHERE location != '' ORDER BY location"
    ).fetchall()
    return [row[0] for row in rows]


def get_distinct_sources(conn: sqlite3.Connection) -> list[str]:
    """Return distinct source names parsed from the JSON sources column."""
    rows = conn.execute(
        "SELECT DISTINCT sources FROM jobs WHERE sources != '[]'"
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        try:
            for src in json.loads(row[0]):
                seen.add(src)
        except (json.JSONDecodeError, TypeError):
            pass
    return sorted(seen)


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
            "SELECT * FROM user_activity ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


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
    except sqlite3.OperationalError:
        return []


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
        """SELECT pd.*,
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
            f"Invalid resolution: {resolution!r}. "
            f"Must be one of: {', '.join(_VALID_RESOLUTIONS)}."
        )
    now = utc_now_iso()
    conn.execute(
        "UPDATE pipeline_detections SET status = ?, resolved_at = ? WHERE id = ?",
        (resolution, now, detection_id),
    )
    conn.commit()
