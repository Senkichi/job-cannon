"""SQLite persistence layer for job deduplication and run history.

Package layout (post-S7d):

- `_classification.py` — JobAssessment + derive_classification + _SUB_SCORE_KEYS
  (pure scoring-rule logic, zero DB deps).
- `_persistence.py` (planned) — write paths (persist_*, update_pipeline_status,
  log_run).
- `_jobs.py` (planned) — job CRUD (upsert_job, get_job, merge_description,
  load_job_context, JOBS_ALL_COLUMNS).
- `_queries.py` (planned) — read-only filters (get_filtered_jobs +
  sort_by allowlist co-located, get_distinct_sources).

This `__init__.py` re-exports the public surface so existing
`from job_finder.db import X` paths continue to work unchanged.
"""

from __future__ import annotations

import json
import logging
import sqlite3

# v3.0 scoring-rule cluster — pure logic, no DB deps. Re-exported for the
# public surface (downstream callers in eval/, tests/, web/ import these).
# PEP 484 explicit re-export form — `as X` makes the re-export contract
# machine-readable to mypy/pyright and silences reportUnusedImport.
from ._classification import _SUB_SCORE_KEYS as _SUB_SCORE_KEYS
from ._classification import JobAssessment as JobAssessment
from ._classification import derive_classification as derive_classification

# DB write paths — runs log + per-row persistence + pipeline state machine.
from ._persistence import log_run as log_run
from ._persistence import persist_job_archetype as persist_job_archetype
from ._persistence import persist_job_assessment as persist_job_assessment
from ._persistence import persist_job_expiry_state as persist_job_expiry_state
from ._persistence import update_pipeline_status as update_pipeline_status

# Job CRUD — full-row reads + upsert with merge logic + context bundle.
# `JOBS_ALL_COLUMNS` is owned by `_jobs.py` and re-imported below by
# `get_filtered_jobs` (still in this file at this commit; moves in C6).
from ._jobs import JOBS_ALL_COLUMNS as JOBS_ALL_COLUMNS
from ._jobs import get_job as get_job
from ._jobs import load_job_context as load_job_context
from ._jobs import merge_description as merge_description
from ._jobs import upsert_job as upsert_job

_log = logging.getLogger(__name__)


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
# Sibling modules `db_pipeline.py` and `db_queries.py` live at the
# `job_finder/` level, NOT inside this package. Their public functions are
# re-exported here so existing callers continue to use `from job_finder.db
# import X` for any DB-layer name. PEP 484 explicit re-export form (`as X`)
# documents the contract and silences pyright's reportUnusedImport.

from job_finder.db_pipeline import get_pending_detections as get_pending_detections
from job_finder.db_pipeline import get_pipeline_events as get_pipeline_events
from job_finder.db_pipeline import resolve_detection as resolve_detection
from job_finder.db_queries import get_dashboard_stats as get_dashboard_stats
from job_finder.db_queries import get_distinct_locations as get_distinct_locations
from job_finder.db_queries import get_jobs_by_status as get_jobs_by_status
from job_finder.db_queries import get_pipeline_summary as get_pipeline_summary
from job_finder.db_queries import get_recent_activity as get_recent_activity
from job_finder.db_queries import get_recent_pipeline_events as get_recent_pipeline_events
from job_finder.db_queries import get_recent_runs as get_recent_runs
