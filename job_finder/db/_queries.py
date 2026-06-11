"""Read-only filter queries — get_filtered_jobs (D=26) + get_distinct_sources.

Owns the **sort_by allowlist invariant** (CLAUDE.md security-critical rule):
the `allowed_sort_cols` set literal AND the f-string composer that
interpolates `sort_by` into the ORDER BY clause MUST stay co-located in
this single file. Promoting the allowlist to a module-level constant in a
separate file would split the security contract across the lexical /
import boundary.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import get_filtered_jobs` (etc.) paths keep working.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from ._jobs import JOBS_ALL_COLUMNS

_log = logging.getLogger(__name__)


def _local_day_start_as_utc_iso(days_offset: int = 0) -> str:
    """Return the start-of-day in user-local time, N days back, as naive UTC ISO.

    days_offset=0  → start of today local
    days_offset=2  → start of the day 2 days ago local (covers "last 3 days" with 0,1,2)

    Uses datetime.now().astimezone() to treat the system clock as local-aware,
    then shifts to UTC. This keeps the filter anchored to the user's local midnight
    rather than UTC midnight.
    """
    local_now = datetime.now().astimezone()
    start_local = (local_now - timedelta(days=days_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start_local.astimezone(UTC).replace(tzinfo=None).isoformat()


def _local_date_str_to_utc_iso(date_str: str, end_of_day: bool = False) -> str:
    """Convert a local calendar-day string (YYYY-MM-DD) to a naive UTC ISO string.

    date_str is treated as local midnight (start_of_day) or local 23:59:59 (end_of_day).
    The result is stored-UTC-comparable with first_seen values.
    """
    parts = date_str.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    if end_of_day:
        local_dt = datetime(year, month, day, 23, 59, 59).astimezone()
    else:
        local_dt = datetime(year, month, day, 0, 0, 0).astimezone()
    return local_dt.astimezone(UTC).replace(tzinfo=None).isoformat()


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


def get_distinct_country_codes(conn: sqlite3.Connection) -> list[str]:
    """Return distinct ``primary_country_code`` values populated on ``jobs``.

    Sub-second on an indexed nullable column — only rows with a resolved
    country contribute. Empty until Layer-1 scanners (Commit C) or m067
    backfill (Commit E) populate the column.
    """
    rows = conn.execute(
        "SELECT DISTINCT primary_country_code FROM jobs "
        "WHERE primary_country_code IS NOT NULL "
        "ORDER BY primary_country_code"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def get_distinct_workplace_types(conn: sqlite3.Connection) -> list[str]:
    """Return distinct ``workplace_type`` values populated on ``jobs``.

    Drawn from the m066 denormalized column. Values are the four-element
    enum REMOTE / HYBRID / ONSITE / UNSPECIFIED. Returns whichever subset
    actually appears in the live data.
    """
    rows = conn.execute(
        "SELECT DISTINCT workplace_type FROM jobs "
        "WHERE workplace_type IS NOT NULL "
        "ORDER BY workplace_type"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


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

# ---------------------------------------------------------------------------
# Phase 47.06 — "unresolved" review filter
# ---------------------------------------------------------------------------
# A row is "unresolved" if it carries non-empty unresolved_reasons (the m078
# JSON column, default '[]') OR any structured location is flagged
# unresolved=true. json_valid() guards the json_each() call: locations_structured
# defaults to NULL (json_each(NULL) yields no rows — safe) but a malformed/empty
# '' value would raise "malformed JSON", so non-JSON is treated as "no unresolved
# location" rather than crashing the listing query. These are static SQL strings
# (no user input interpolated) — safe to compose into the WHERE clause.
_LOCATION_UNRESOLVED_SQL = (
    "(json_valid(locations_structured) = 1 AND EXISTS ("
    "SELECT 1 FROM json_each(locations_structured) "
    "WHERE json_extract(value, '$.unresolved') = 1))"
)
_UNRESOLVED_HIDE_SQL = f"(unresolved_reasons = '[]' AND NOT {_LOCATION_UNRESOLVED_SQL})"
_UNRESOLVED_ONLY_SQL = f"(unresolved_reasons != '[]' OR {_LOCATION_UNRESOLVED_SQL})"

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
    country: str | None = None,
    workplace_type: str | None = None,
    unresolved: str = "hide",
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
    # Recency = best-known posting date with an explicit detection-time
    # fallback (#365). Display marks the fallback cohort; filters and the
    # 'recency' sort share this single expression.
    _RECENCY_SQL = "COALESCE(posted_date, first_seen)"

    # SECURITY-CRITICAL: the `allowed_sort_cols` set literal and the f-string
    # composer below MUST stay co-located in the same file (per S7d split
    # invariant + CLAUDE.md "sort_by validated against Python allowlist before
    # SQL interpolation"). Splitting them across files re-introduces
    # SQL-injection surface even with passing tests. Move the entire function
    # together if this ever needs to relocate again.
    allowed_sort_cols = {
        "score",
        "title",
        "company",
        "location",
        "first_seen",
        "recency",
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
    elif sort_by == "recency":
        # Best-known posting date (#365): true posted_date when a source
        # provided one, detection time otherwise. Fixed expression — the
        # allowlist key never reaches the SQL string.
        sort_expr = f"{_RECENCY_SQL} {sort_dir}"
    else:
        sort_expr = f"{sort_by} {sort_dir}"

    order_expr = sort_expr

    conditions: list[str] = []
    params: list = []

    if status:
        # Phase 49.05 (I-15 / F-10): the explicit status filter resolves against
        # the unified computed_status VIRTUAL column so 'stale'/'expired' and the
        # pipeline states all filter from one canonical value (no more three-way
        # pipeline_status/is_stale/expiry_status drift at the UI). The default
        # hidden-status exclusion below stays on pipeline_status — it reflects a
        # user action (archived/dismissed/...), orthogonal to computed status.
        if isinstance(status, list):
            placeholders = ", ".join("?" * len(status))
            conditions.append(f"computed_status IN ({placeholders})")
            params.extend(status)
        else:
            conditions.append("computed_status = ?")
            params.append(status)
    elif not show_hidden:
        hidden_placeholders = ", ".join("?" * len(_HIDDEN_STATUSES))
        conditions.append(f"pipeline_status NOT IN ({hidden_placeholders})")
        params.extend(_HIDDEN_STATUSES)

    if location:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")

    if posted_within:
        # Cutoffs are computed in Python as local-midnight-to-UTC so that
        # "Today" means today in the user's local time, not UTC midnight.
        _within_cutoff: str | None = None
        if posted_within == "today":
            _within_cutoff = _local_day_start_as_utc_iso(0)
        elif posted_within == "3d":
            # "Last 3 days" = today + yesterday + day-before-yesterday → offset 2.
            _within_cutoff = _local_day_start_as_utc_iso(2)
        elif posted_within == "1w":
            _within_cutoff = _local_day_start_as_utc_iso(6)
        elif posted_within == "1m":
            # Approximate: 30 days back (SQLite's '-1 month' is calendar arithmetic;
            # 30 days is close enough for a display filter without adding dateutil).
            _within_cutoff = _local_day_start_as_utc_iso(30)
        if _within_cutoff is not None:
            conditions.append(f"{_RECENCY_SQL} >= ?")
            params.append(_within_cutoff)

    if freshness:
        from job_finder.utils.business_days import business_days_ago

        cutoff = None
        if freshness == "biz1":
            # business_days_ago returns a local date; treat as local midnight → UTC.
            from datetime import time as _time

            biz_date = business_days_ago(1)
            cutoff = (
                datetime.combine(biz_date, _time.min)
                .astimezone()
                .astimezone(UTC)
                .replace(tzinfo=None)
                .isoformat()
            )
        elif freshness == "biz3":
            from datetime import time as _time

            biz_date = business_days_ago(3)
            cutoff = (
                datetime.combine(biz_date, _time.min)
                .astimezone()
                .astimezone(UTC)
                .replace(tzinfo=None)
                .isoformat()
            )
        if cutoff:
            conditions.append(f"{_RECENCY_SQL} >= ?")
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
    # The legacy `jobs.score` column is vestigial under v3.0 (the LLM scoring
    # path never writes it; ~40% of classified rows carry score=0), so the
    # translation now matches on classification alone. Rows with NULL
    # classification (pre-v3 or unscored) are filtered out by this shim — they
    # were already noise in the old score-disjunct path since v3.0 never
    # populates `score`.
    if min_score is not None:
        mapped = _classifications_for_min_score(min_score)
        placeholders = ", ".join("?" * len(mapped))
        conditions.append(f"classification IN ({placeholders})")
        params.extend(mapped)
    if max_score is not None:
        mapped = _classifications_for_max_score(max_score)
        placeholders = ", ".join("?" * len(mapped))
        conditions.append(f"classification IN ({placeholders})")
        params.extend(mapped)
    if salary_min is not None:
        conditions.append("salary_min >= ?")
        params.append(salary_min)
    if source:
        conditions.append("sources LIKE ?")
        params.append(f'%"{source}"%')
    if date_from:
        # date_from is a local YYYY-MM-DD string from <input type="date">;
        # treat as local midnight and convert to UTC for comparison with stored naive UTC.
        conditions.append("first_seen >= ?")
        params.append(_local_date_str_to_utc_iso(date_from, end_of_day=False))
    if date_to:
        # date_to is a local YYYY-MM-DD string; treat as local 23:59:59 → UTC.
        conditions.append("first_seen <= ?")
        params.append(_local_date_str_to_utc_iso(date_to, end_of_day=True))

    # m066 denormalized columns. Values are SQL-bound (no f-string
    # interpolation), but sanity-check the shape so a malformed query
    # string returns no results instead of a SQL execute on garbage:
    #   - country: ISO 3166-1 alpha-2 (uppercase letters, exactly 2 chars)
    #   - workplace_type: one of the four-member enum.
    if country:
        normalized_cc = country.strip().upper()
        if len(normalized_cc) == 2 and normalized_cc.isalpha():
            conditions.append("primary_country_code = ?")
            params.append(normalized_cc)
    if workplace_type:
        normalized_wt = workplace_type.strip().upper()
        if normalized_wt in {"REMOTE", "HYBRID", "ONSITE", "UNSPECIFIED"}:
            conditions.append("workplace_type = ?")
            params.append(normalized_wt)

    # Phase 47.06 — unresolved review filter. Default "hide" keeps unresolved
    # rows out of the standard listing; "only" surfaces them for /admin/review;
    # "all" applies no filter. Unknown values fall through to "hide" (safe
    # default). Static SQL — no params appended.
    if unresolved == "only":
        conditions.append(_UNRESOLVED_ONLY_SQL)
    elif unresolved != "all":
        conditions.append(_UNRESOLVED_HIDE_SQL)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT {JOBS_ALL_COLUMNS} FROM jobs {where_clause} ORDER BY {order_expr} LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]
