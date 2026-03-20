"""SQLite persistence layer for job deduplication and run history."""

import json
import re
import sqlite3
from datetime import datetime, date
from typing import Optional

from job_finder.models import Job
from job_finder.web.db_helpers import safe_json_load


def _merge_description(existing: Optional[str], new: Optional[str]) -> Optional[str]:
    """Merge two description strings for the smart upsert_job UPDATE branch.

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


class JobDB:
    """SQLite-backed job storage with deduplication."""

    def __init__(self, db_path: str = "jobs.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """Create tables if they don't exist."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',     -- JSON array of source names
                source_urls TEXT DEFAULT '[]',  -- JSON array of URLs
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed'  -- unreviewed, interested, skipped, applied
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                jobs_fetched INTEGER DEFAULT 0,
                jobs_new INTEGER DEFAULT 0,
                jobs_scored INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_interest ON jobs(user_interest);
            CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen DESC);
        """
        )
        self.conn.commit()

    def upsert_job(self, job: Job) -> bool:
        """Insert or update a job. Returns True if it's new.

        Deduplication: if the same job (by dedup_key) already exists,
        merge source URLs, locations (Remote/Hybrid first), and descriptions
        (substring dedup — keep longer; append different content with separator).
        Keep first_seen from the original row.

        INSERT branch initializes locations_raw as a JSON array with the
        initial location so the UPDATE merge logic always has a base array.
        """
        existing = self.conn.execute(
            "SELECT * FROM jobs WHERE dedup_key = ?", (job.dedup_key,)
        ).fetchone()

        now = datetime.now().isoformat()

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

            self.conn.execute(
                """UPDATE jobs SET
                    sources = ?, source_urls = ?, last_seen = ?,
                    score = ?, score_breakdown = ?,
                    salary_min = COALESCE(?, salary_min),
                    salary_max = COALESCE(?, salary_max),
                    description = ?,
                    locations_raw = ?,
                    location = ?
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
                    job.dedup_key,
                ),
            )
            self.conn.commit()
            # Auto-reopen: if an archived job re-appears in ingestion, treat
            # re-appearance as proof the job is live again (per CONTEXT.md decision)
            if existing["pipeline_status"] == "archived":
                update_pipeline_status(
                    self.conn, job.dedup_key, "discovered",
                    source="ingestion", evidence="re_appeared",
                )
            return False
        else:
            # Use the email date as first_seen when available (Gmail-sourced jobs).
            # SerpAPI jobs have no email date, so they use the current ingestion time.
            first_seen = job.posted_date.isoformat() if job.posted_date else now
            # Initialize locations_raw as a JSON array with the initial location
            initial_locs = [job.location] if job.location else []
            self.conn.execute(
                """INSERT INTO jobs
                    (dedup_key, title, company, location, sources, source_urls,
                     source_id, salary_min, salary_max, description,
                     first_seen, last_seen, score, score_breakdown, locations_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            self.conn.commit()
            return True

    def get_top_jobs(
        self,
        limit: int = 50,
        min_score: float = 0,
        interest_filter: Optional[str] = None,
    ) -> list[dict]:
        """Get top-scored jobs."""
        query = "SELECT * FROM jobs WHERE score >= ?"
        params: list = [min_score]

        if interest_filter:
            query += " AND user_interest = ?"
            params.append(interest_filter)

        query += " ORDER BY score DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_interest(self, dedup_key: str, interest: str):
        """Mark a job as interested, skipped, or applied."""
        self.conn.execute(
            "UPDATE jobs SET user_interest = ? WHERE dedup_key = ?",
            (interest, dedup_key),
        )
        self.conn.commit()

    def log_run(self, source: str, fetched: int, new: int, scored: int):
        """Log a pipeline run for auditing."""
        self.conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), source, fetched, new, scored),
        )
        self.conn.commit()

    def stats(self) -> dict:
        """Get database statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_interest = dict(
            self.conn.execute(
                "SELECT user_interest, COUNT(*) FROM jobs GROUP BY user_interest"
            ).fetchall()
        )
        recent_runs = self.conn.execute(
            "SELECT * FROM runs ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()

        return {
            "total_jobs": total,
            "by_interest": by_interest,
            "recent_runs": [dict(r) for r in recent_runs],
        }


# ---------------------------------------------------------------------------
# Module-level DB functions for Flask views (use sqlite3.Connection directly)
# ---------------------------------------------------------------------------


def get_job(conn: sqlite3.Connection, dedup_key: str) -> Optional[dict]:
    """Return a single job by dedup_key, or None if not found.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Job as dict with all columns, or None if not found.
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
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
    except Exception:
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
        "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?",
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
    """
    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return  # Job not found — no-op

    from_status = row["pipeline_status"]
    if from_status == new_status:
        return  # Already at this status — skip duplicate event insertion

    now = datetime.now().isoformat()

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
    status: Optional[str] = None,
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
    query = f"SELECT * FROM jobs {where_clause} ORDER BY {order_expr} LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_pipeline_events(conn: sqlite3.Connection, dedup_key: str) -> list[dict]:
    """Return all pipeline events for a job, newest first."""
    rows = conn.execute(
        "SELECT * FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC",
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
    except Exception:
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
    except Exception:
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
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE pipeline_detections SET status = ?, resolved_at = ? WHERE id = ?",
        (resolution, now, detection_id),
    )
    conn.commit()
