"""SQLite persistence layer for job deduplication and run history."""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from job_finder.models import Job
from job_finder.json_utils import safe_json_load, utc_now_iso

_log = logging.getLogger(__name__)


# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() and get_filtered_jobs() which
# return complete row dicts to templates and callers.
JOBS_ALL_COLUMNS = (
    "dedup_key, title, company, location, sources, source_urls, source_id, "
    "salary_min, salary_max, description, first_seen, last_seen, score, "
    "score_breakdown, user_interest, pipeline_status, posted_date, notes, "
    "haiku_score, haiku_summary, sonnet_score, fit_analysis, jd_full, is_stale, "
    "company_id, comp_data_json, enrichment_tier, rejection_reviewed, "
    "locations_raw, description_reformatted, expiry_checked_at, scoring_provider, "
    "opus_score, expiry_status, eval_blocks, job_archetype"
)

# Columns read by upsert_job() for merge logic — only what the UPDATE branch needs.
_UPSERT_MERGE_COLUMNS = (
    "sources, source_urls, locations_raw, description, jd_full, pipeline_status"
)


def merge_description(existing: str | None, new: str | None) -> str | None:
    """Merge two description strings — single source of truth for description merge logic.

    Also used iteratively by dedup_normalizer.merge_descriptions for N-way merges.

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
    """Insert or update a job. Returns True if new, False if existing.

    Merges sources, locations (Remote/Hybrid first), and descriptions
    (keep longer; append divergent content with separator). Keeps first_seen
    from the original row. Initializes locations_raw as JSON array.
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
        merged_description = merge_description(
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
    provider: str | None = None,
    eval_blocks: str | None = None,
) -> None:
    """Persist Sonnet evaluation results for a job.

    Single point of truth for the sonnet_score UPDATE statement.
    Called from scoring_orchestrator, backfill_enrichment, and scoring_evaluator.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        sonnet_score: Numeric score from Sonnet deep evaluation.
        fit_analysis: JSON string containing fit analysis details.
        provider: Provider name that produced the score (e.g. "ollama"). None preserves existing value.
        eval_blocks: JSON string of structured evaluation criteria. None leaves column unchanged.
    """
    conn.execute(
        "UPDATE jobs SET sonnet_score = ?, fit_analysis = ?, "
        "scoring_provider = COALESCE(?, scoring_provider), "
        "eval_blocks = COALESCE(?, eval_blocks) WHERE dedup_key = ?",
        (sonnet_score, fit_analysis, provider, eval_blocks, dedup_key),
    )
    conn.commit()


def persist_job_expiry_state(
    conn: sqlite3.Connection,
    dedup_key: str,
    expiry_status: str,
    checked_at: str,
) -> None:
    """Persist job expiry verdict and timestamp atomically.

    Single write path for expiry_status and expiry_checked_at. Called by
    the scoring preflight (per-job liveness check) and the nightly batch
    expiry runner.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        expiry_status: One of 'expired', 'live', or 'inconclusive'.
        checked_at: ISO 8601 timestamp string of when the check ran.
    """
    conn.execute(
        "UPDATE jobs SET expiry_status = ?, expiry_checked_at = ? WHERE dedup_key = ?",
        (expiry_status, checked_at, dedup_key),
    )
    conn.commit()


def persist_job_archetype(
    conn: sqlite3.Connection,
    dedup_key: str,
    job_archetype: str,
) -> None:
    """Persist the deterministic job archetype classification result.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        job_archetype: Archetype label (e.g. 'platform_engineering').
    """
    conn.execute(
        "UPDATE jobs SET job_archetype = ? WHERE dedup_key = ?",
        (job_archetype, dedup_key),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, dedup_key: str) -> dict | None:
    """Return a single job by dedup_key, or None if not found.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Job as dict with all columns, or None if not found.
    """
    row = conn.execute(
        f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def load_job_context(conn: sqlite3.Connection, dedup_key: str) -> dict | None:
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


_HIDDEN_STATUSES = ("archived", "withdrawn", "dismissed", "rejected")


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
            _log.warning("get_distinct_sources: corrupt sources JSON skipped: %r", row[0])
    return sorted(seen)


def get_filtered_jobs(
    conn: sqlite3.Connection,
    status: str | list[str] | None = None,
    location: str | None = None,
    posted_within: str | None = None,
    freshness: str | None = None,
    sort_by: str = "score",
    sort_dir: str = "DESC",
    limit: int = 100,
    hide_stale: bool = False,
    show_hidden: bool = False,
    min_score: float | None = None,
    max_score: float | None = None,
    salary_min: int | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Return jobs matching the given filters, sorted and limited.

    status: single string or list for IN-filter. sort_by validated against
    allowlist (SQL injection guard). score sort uses COALESCE(sonnet, haiku,
    heuristic). Hidden statuses excluded by default unless status set or
    show_hidden=True.
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
        sort_expr = f"COALESCE(sonnet_score, haiku_score, score) {sort_dir}"
    else:
        sort_expr = f"{sort_by} {sort_dir}"

    order_expr = sort_expr

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
    elif not show_hidden:
        hidden_placeholders = ", ".join("?" * len(_HIDDEN_STATUSES))
        conditions.append(f"pipeline_status NOT IN ({hidden_placeholders})")
        params.extend(_HIDDEN_STATUSES)

    if location:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")

    if posted_within:
        _within_map = {
            "today": "date('now')",
            "3d": "date('now', '-3 days')",
            "1w": "date('now', '-7 days')",
            "1m": "date('now', '-1 month')",
        }
        if posted_within in _within_map:
            conditions.append(f"first_seen >= {_within_map[posted_within]}")

    if freshness:
        from job_finder.utils.business_days import business_days_ago
        cutoff = None
        if freshness == "biz1":
            cutoff = business_days_ago(1).isoformat()
        elif freshness == "biz3":
            cutoff = business_days_ago(3).isoformat()
        if cutoff:
            conditions.append("first_seen >= ?")
            params.append(cutoff)

    if hide_stale:
        conditions.append("is_stale = 0")

    if min_score is not None:
        conditions.append("COALESCE(sonnet_score, haiku_score, score) >= ?")
        params.append(min_score)
    if max_score is not None:
        conditions.append("COALESCE(sonnet_score, haiku_score, score) <= ?")
        params.append(max_score)
    if salary_min is not None:
        conditions.append("salary_min >= ?")
        params.append(salary_min)
    if source:
        conditions.append("sources LIKE ?")
        params.append(f'%"{source}"%')
    if date_from:
        conditions.append("first_seen >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("first_seen <= ? || ' 23:59:59'")
        params.append(date_to)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT {JOBS_ALL_COLUMNS} FROM jobs {where_clause} ORDER BY {order_expr} LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Re-exports for backward compatibility
# ---------------------------------------------------------------------------

from job_finder.db_queries import (  # noqa: E402
    get_dashboard_stats,
    get_distinct_locations,
    get_jobs_by_status,
    get_pipeline_summary,
    get_recent_activity,
    get_recent_pipeline_events,
    get_recent_runs,
)
from job_finder.db_pipeline import (  # noqa: E402
    get_pending_detections,
    get_pipeline_events,
    resolve_detection,
)
