"""Job CRUD — full-row read, upsert with merge logic, context bundle.

Owns `JOBS_ALL_COLUMNS` (the canonical jobs-row projection contract; also
imported by `_queries.py` for full-row reads). `upsert_job` calls
`update_pipeline_status` from `._persistence` for the auto-reopen branch
on archived re-appearances.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import upsert_job` (etc.) paths keep working.
"""

from __future__ import annotations

import json
import re
import sqlite3

from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.models import Job

from ._persistence import update_pipeline_status

# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() (this module) and
# get_filtered_jobs() (`_queries.py`) which return complete row dicts to
# templates and callers. Single source of truth for the projection contract.
JOBS_ALL_COLUMNS = (
    "dedup_key, title, company, location, sources, source_urls, source_id, "
    "salary_min, salary_max, description, first_seen, last_seen, score, "
    "score_breakdown, user_interest, pipeline_status, posted_date, notes, "
    "fit_analysis, classification, sub_scores_json, scoring_model, "
    "jd_full, is_stale, "
    "company_id, comp_data_json, enrichment_tier, "
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
        merged_description = merge_description(existing["description"], job.description)

        # Eager promotion: if jd_full is NULL and merged description is
        # substantial, promote it so enrichment has a baseline to beat.
        jd_full_clause = ""
        jd_full_value: tuple = ()
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
                conn,
                job.dedup_key,
                "discovered",
                source="ingestion",
                evidence="re_appeared",
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
        # Explicit scoring_provider=NULL on INSERT to override the migration 20
        # column DEFAULT 'anthropic'. The DEFAULT pre-dates the multi-provider
        # cascade; without this override, every new row enters tagged as scored
        # by anthropic before any scorer has run. The legitimate write path
        # (persist_job_assessment) sets scoring_provider + scoring_model
        # atomically via COALESCE, so the discriminator for "real attribution"
        # is scoring_model IS NOT NULL.
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 source_id, salary_min, salary_max, description,
                 first_seen, last_seen, score, score_breakdown, locations_raw,
                 jd_full, scoring_provider)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                None,
            ),
        )
        conn.commit()
        return True


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
    """Load the standard job context bundle.

    Shared helper for expand, rescore, paste_jd, and save_jd routes.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Dict with key 'job', or None if job not found.
    """
    job = get_job(conn, dedup_key)
    if job is None:
        return None

    return {"job": job}
