"""SQLite persistence layer for job deduplication and run history."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.models import Job

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v3.0 ordinal scoring (Phase 34) — JobAssessment + classification rule
# ---------------------------------------------------------------------------

# Canonical sub-score key order (matches CONTEXT D-05 and the v3 scoring prompt's
# JSON schema). Used for JSON serialization stability and for derive_classification.
_SUB_SCORE_KEYS: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


@dataclass(frozen=True)
class JobAssessment:
    """Unified v3.0 scoring result. Replaces HaikuScore + SonnetScore pair.

    Per CONTEXT D-05 (Phase 34):

      sub_scores: dict[str, int] with 6 keys (title_fit, location_fit, comp_fit,
          domain_match, seniority_match, skills_match) — each 1-5 integer.
      classification: one of apply|consider|skip|reject. Typically a sentinel
          empty string at construction time; derive_classification() at persist
          time computes the authoritative value (see D-06 rule and D-07 note
          that legitimacy_note is read from the jobs row, not from the LLM).
      rationale: dict with keys strengths, gaps, talking_points,
          resume_priority_skills (each a list[str]); serialized to the reused
          fit_analysis column per D-08.
      provider: cascade-attribution string (e.g., "ollama", "anthropic") or None.
    """

    sub_scores: dict
    classification: str
    rationale: dict
    provider: str | None = None


def derive_classification(
    sub_scores: dict,
    legitimacy_note: str | None,
    enrichment_tier: str | None = None,
    jd_full_length: int = 0,
    low_signal_threshold: int = 1500,
) -> str:
    """Python-derived 5-way classification — NOT LLM-emitted (CONTEXT D-06, anti-pattern 3).

    Rule precedence (per spec D-2.5, Phase 2d sub-fix 2/4):
      1. legitimacy_note truthy            -> "reject"
      2. enrichment exhausted + short jd   -> "low_signal"
      3. any sub-score == 1                -> "reject"
      4. all sub-scores >= 3               -> "apply"
      5. all sub-scores >= 2               -> "consider"
      6. otherwise                         -> "skip"

    The low_signal branch surfaces genuinely-no-signal jobs (enrichment cascade
    exhausted AND jd_full below threshold) honestly instead of rolling them
    into apply/consider/skip via unreliable rubric outputs. The branch sits
    BEFORE the any-axis-1 reject check on purpose: a job with insufficient JD
    text cannot be confidently rejected on rubric outputs (the 1 itself may be
    a hallucination from the model scoring against an empty prompt).

    For integer 1-5 sub-scores, branch 6 ("skip") is effectively unreachable —
    any value below 2 is 1, which already triggered reject at branch 3. The
    branch remains for defense-in-depth against future sub-score domain changes
    (e.g., 0 added as a sentinel).

    Args:
        sub_scores: dict of the 6 ordinal sub-scores (1-5 integers).
        legitimacy_note: value of the jobs.legitimacy_note column; truthy means
            ingestion-time scam/exclusion detection flagged this row.
        enrichment_tier: value of jobs.enrichment_tier ('free' | 'ddg' | 'haiku'
            | 'serpapi' | 'sonnet' | 'exhausted' | None). Only 'exhausted'
            participates in the low_signal rule; other tiers are still
            re-enrichment candidates.
        jd_full_length: character length of jobs.jd_full (0 when NULL).
        low_signal_threshold: jd_full_length below this triggers low_signal
            when enrichment is exhausted. Configurable via
            scoring.low_signal_jd_chars.

    Returns:
        One of "reject", "low_signal", "apply", "consider", "skip".
    """
    if legitimacy_note:
        return "reject"
    if enrichment_tier == "exhausted" and jd_full_length < low_signal_threshold:
        return "low_signal"
    if any(v == 1 for v in sub_scores.values()):
        return "reject"
    if all(v >= 3 for v in sub_scores.values()):
        return "apply"
    if all(v >= 2 for v in sub_scores.values()):
        return "consider"
    return "skip"


# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() and get_filtered_jobs() which
# return complete row dicts to templates and callers.
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


def log_run(conn: sqlite3.Connection, source: str, fetched: int, new: int, scored: int) -> None:
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


def persist_job_assessment(
    conn: sqlite3.Connection,
    dedup_key: str,
    assessment: JobAssessment,
    provider: str | None = None,
    model: str | None = None,
    *,
    config: dict | None = None,
) -> None:
    """Persist a v3.0 JobAssessment. Replaces persist_haiku_score + persist_sonnet_score.

    Writes classification (derived at persist time), sub_scores_json (JSON),
    fit_analysis (rationale payload — D-08 reuse), scoring_provider, scoring_model.
    Plan 5 (Migration 41) dropped the legacy haiku_score/haiku_summary/sonnet_score
    columns; this function now writes only the v3.0 surface.

    legitimacy_note sourcing (CONTEXT D-07): read from the existing jobs row,
    NOT from the assessment. derive_classification uses this value to compute
    the authoritative classification — any classification field on the passed
    assessment is ignored (anti-pattern 3 defense).

    Phase 2d sub-fix 2-3/4: also reads enrichment_tier and LENGTH(jd_full) from
    the row so derive_classification can compute the low_signal verdict. The
    threshold (default 1500 chars) is sourced from config.scoring.low_signal_jd_chars
    when config is provided; callers that pass config=None get the default,
    preserving back-compat with direct test/script invocations.

    No-op on missing dedup_key (SQLite UPDATE with no matching row is a silent
    no-op; we also short-circuit before the UPDATE to avoid COALESCE no-ops).

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        assessment: JobAssessment with sub_scores + rationale.
        provider: Cascade-attribution string; None preserves the existing value.
        model: Model identifier (e.g., "qwen2.5:14b"); None preserves existing.
        config: Optional application config dict. When provided, reads
            scoring.low_signal_jd_chars to set the low_signal threshold;
            otherwise the default (1500 chars) is used.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT legitimacy_note, enrichment_tier, COALESCE(LENGTH(jd_full), 0) AS jd_len "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    )
    row = cur.fetchone()
    if row is None:
        # Silent no-op matches SQLite UPDATE-no-match semantics.
        return
    legitimacy_note, enrichment_tier, jd_full_length = row[0], row[1], row[2] or 0

    # Resolve low_signal threshold from config (Phase 2d sub-fix 3/4). Default
    # 1500 chars matches scoring.low_signal_jd_chars.example. None config keeps
    # backwards compatibility for tests/scripts that call directly.
    threshold = 1500
    if config is not None:
        scoring_cfg = config.get("scoring") or {}
        threshold = int(scoring_cfg.get("low_signal_jd_chars", 1500))

    final_classification = derive_classification(
        assessment.sub_scores,
        legitimacy_note,
        enrichment_tier=enrichment_tier,
        jd_full_length=jd_full_length,
        low_signal_threshold=threshold,
    )

    # Serialize sub_scores with stable key order for diff-friendliness.
    ordered_sub_scores = {
        k: assessment.sub_scores[k] for k in _SUB_SCORE_KEYS if k in assessment.sub_scores
    }

    cur.execute(
        """
        UPDATE jobs
           SET classification   = ?,
               sub_scores_json  = ?,
               fit_analysis     = ?,
               scoring_provider = COALESCE(?, scoring_provider),
               scoring_model    = COALESCE(?, scoring_model)
         WHERE dedup_key = ?
        """,
        (
            final_classification,
            json.dumps(ordered_sub_scores),
            json.dumps(assessment.rationale),
            provider or assessment.provider,
            model,
            dedup_key,
        ),
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

    Retries on 'database is locked' (3 attempts, exponential backoff).
    On 2026-05-01 the day-1 monthly hygiene jobs collided with the daily
    agentic_backfill at 03:30, exhausting the standalone_connection's 30s
    busy_timeout 113 times in this function and aborting the reconciler
    mid-batch. The cron decoupling fix (scheduler.py: agentic moved to
    04:15) is the primary defense; this retry is belt-and-suspenders for
    any future writer contention spike.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        expiry_status: One of 'expired', 'live', or 'inconclusive'.
        checked_at: ISO 8601 timestamp string of when the check ran.
    """
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(3):
        try:
            conn.execute(
                "UPDATE jobs SET expiry_status = ?, expiry_checked_at = ? WHERE dedup_key = ?",
                (expiry_status, checked_at, dedup_key),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            last_err = e
            # Backoff: 0.5s, 1.0s. busy_timeout (30s) already kicked in inside
            # sqlite before we got here, so any sleep here is on top of that.
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
                _log.warning(
                    "persist_job_expiry_state: database locked, retry %d/2 (dedup_key=%s)",
                    attempt + 1,
                    dedup_key,
                )
    # Exhausted retries — re-raise so the caller's outer try/except records the error.
    assert last_err is not None
    raise last_err


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
    rows = conn.execute("SELECT DISTINCT sources FROM jobs WHERE sources != '[]'").fetchall()
    seen: set[str] = set()
    for row in rows:
        try:
            for src in json.loads(row[0]):
                seen.add(src)
        except (json.JSONDecodeError, TypeError):
            _log.warning("get_distinct_sources: corrupt sources JSON skipped: %r", row[0])
    return sorted(seen)


# ---------------------------------------------------------------------------
# v3.0 classification-rank ordering (Phase 34 Plan 3 Commit A)
# ---------------------------------------------------------------------------
# SQL CASE expression mapping classification enum -> numeric priority for ORDER BY.
# Plan 4 deletes legacy score columns; this expression becomes the ONLY score-like
# sort signal available.
_CLASSIFICATION_RANK_CASE = (
    "CASE classification "
    "WHEN 'apply' THEN 4 "
    "WHEN 'consider' THEN 3 "
    "WHEN 'skip' THEN 2 "
    "WHEN 'reject' THEN 1 "
    "WHEN 'low_signal' THEN 0 "
    "ELSE 0 END"
)

# Sum of the 6 sub-scores pulled from sub_scores_json — used as tiebreak within
# a classification bucket. Each sub-score is 1-5, so the sum is 6-30 (or 0 if JSON
# is NULL). COALESCE-wrapped so NULL sub_scores_json doesn't crash sort.
_SUB_SCORE_SUM_SQL = (
    "(COALESCE(json_extract(sub_scores_json, '$.title_fit'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.location_fit'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.comp_fit'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.domain_match'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.seniority_match'), 0) + "
    "COALESCE(json_extract(sub_scores_json, '$.skills_match'), 0))"
)


def _classification_score_order(sort_dir: str) -> str:
    """Compose the (classification_rank, sub_score_sum) composite ORDER BY clause.

    Used by get_filtered_jobs() when the caller sorts by the generic 'score'
    key OR a v3 alias ('classification', 'classification_rank', 'sub_score_sum').
    Both keys share the same direction (ASC/DESC).
    """
    direction = "DESC" if sort_dir.upper() != "ASC" else "ASC"
    return f"{_CLASSIFICATION_RANK_CASE} {direction}, {_SUB_SCORE_SUM_SQL} {direction}"


# v3 classification-aware sort keys (preferred).
_CLASSIFICATION_SORT_KEYS: set[str] = {
    "classification",
    "classification_rank",
    "sub_score_sum",
}


# Map of >=-threshold min_score/max_score (legacy numeric filter API) -> list of
# classifications that satisfy it. The numeric-score→classification mapping below
# preserves the *monotonic shim math* from Plan 2 (mean(sub_scores) * 20, range
# 20-100): apply rows have mean>=3 (>=60), consider rows may be 40-60, skip rows
# may be 20-40, reject rows may be NULL-20. Plan 4 removes min_score/max_score
# entirely; this shim only exists to keep existing callers (tests, URL params)
# working throughout Plan 3.
def _classifications_for_min_score(min_score: float) -> list[str]:
    """Translate a legacy min_score threshold into a classification IN-list."""
    if min_score >= 80:
        return ["apply"]
    if min_score >= 60:
        return ["apply", "consider"]
    if min_score >= 40:
        return ["apply", "consider", "skip"]
    return ["apply", "consider", "skip", "reject"]


def _classifications_for_max_score(max_score: float) -> list[str]:
    """Translate a legacy max_score threshold into a classification IN-list."""
    if max_score < 40:
        return ["skip", "reject"]
    if max_score < 60:
        return ["consider", "skip", "reject"]
    if max_score < 80:
        return ["apply", "consider", "skip", "reject"]
    return ["apply", "consider", "skip", "reject"]


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
    classification: str | list[str] | None = None,
) -> list[dict]:
    """Return jobs matching the given filters, sorted and limited.

    status: single string or list for IN-filter. sort_by validated against
    allowlist (SQL injection guard). The default 'score' sort (and the v3
    'classification'/'classification_rank'/'sub_score_sum' keys) map to the
    classification-rank CASE + sub_score_sum composite order defined above.
    Hidden statuses excluded by default unless status set or show_hidden=True.

    Plan 34-03 Commit A: migrated from COALESCE(sonnet_score, haiku_score,
    score) to classification-based ordering; min_score/max_score translate
    to classification IN-list shim via the mapping above.
    The explicit `classification=` kwarg is the preferred filter.
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
    } | _CLASSIFICATION_SORT_KEYS
    if sort_by not in allowed_sort_cols:
        sort_by = "score"
    sort_dir = "DESC" if sort_dir.upper() != "ASC" else "ASC"

    # 'score' sorts by raw composite (sum of 6 sub-scores) — no classification
    # rank prefix. Classification keys preserve the legacy rank+composite order
    # so downstream callers that explicitly opt in still get the bucketed sort.
    if sort_by == "score":
        sort_expr = f"{_SUB_SCORE_SUM_SQL} {sort_dir}"
    elif sort_by in _CLASSIFICATION_SORT_KEYS:
        sort_expr = _classification_score_order(sort_dir)
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

    # Apply classification filter (preferred v3 path).
    if classification is not None:
        classification_candidates = (
            {classification} if isinstance(classification, str) else set(classification)
        )
        placeholders = ", ".join("?" * len(classification_candidates))
        conditions.append(f"classification IN ({placeholders})")
        params.extend(sorted(classification_candidates))

    # Legacy min_score/max_score back-compat — Plan 4 removes this shim entirely.
    # The translation matches OR on either:
    #   (a) the row has a classification that maps to the legacy threshold, OR
    #   (b) the row has NULL classification but its heuristic `score` column
    #       still satisfies the threshold (covers pre-v3 rows that never went
    #       through the unified scorer).
    if min_score is not None:
        mapped = _classifications_for_min_score(min_score)
        placeholders = ", ".join("?" * len(mapped))
        conditions.append(
            f"(classification IN ({placeholders}) OR (classification IS NULL AND score >= ?))"
        )
        params.extend(mapped)
        params.append(min_score)
    if max_score is not None:
        mapped = _classifications_for_max_score(max_score)
        placeholders = ", ".join("?" * len(mapped))
        conditions.append(
            f"(classification IN ({placeholders}) OR (classification IS NULL AND score <= ?))"
        )
        params.extend(mapped)
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

from job_finder.db_pipeline import (  # noqa: F401
    get_pending_detections,
    get_pipeline_events,
    resolve_detection,
)
from job_finder.db_queries import (  # noqa: F401
    get_dashboard_stats,
    get_distinct_locations,
    get_jobs_by_status,
    get_pipeline_summary,
    get_recent_activity,
    get_recent_pipeline_events,
    get_recent_runs,
)
