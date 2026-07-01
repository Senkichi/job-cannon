"""Read-only aggregate queries for the dashboard and pipeline views."""

from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import UTC, date, datetime

from job_finder.db._queries import (
    _HIDDEN_STATUSES,
    _SUB_SCORE_SUM_SQL,
    surfaced_classification_sql,
    target_membership_sql,
)

_DEFAULT_COLD_START_EXCLUDE_DAYS = 30


def _normalized_hhi(counts: list[int]) -> float | None:
    """Compute normalized Herfindahl-Hirschman Index from group counts.

    HHI* = (Σ pᵢ² − 1/n) / (1 − 1/n) where pᵢ = countᵢ / total.
    Ranges 0 (perfectly even) → 1 (one group holds everything).
    Returns None if total == 0. Returns 1.0 if n == 1.

    Args:
        counts: List of group counts (non-negative integers).

    Returns:
        Normalized HHI (0-1) or None if total is zero.
    """
    total = sum(counts)
    if total == 0:
        return None
    n = len(counts)
    if n == 1:
        return 1.0

    # Compute sum of squared proportions
    sum_p_squared = sum((c / total) ** 2 for c in counts)

    # Normalize to [0, 1]
    numerator = sum_p_squared - (1 / n)
    denominator = 1 - (1 / n)
    return numerator / denominator


def _shannon_entropy(counts: list[int]) -> tuple[float, float] | None:
    """Compute Shannon entropy and normalized entropy from group counts.

    H = −Σ pᵢ log₂ pᵢ
    Normalized entropy = H / log₂(n)
    Returns None if total == 0 or n == 1.

    Args:
        counts: List of group counts (non-negative integers).

    Returns:
        Tuple of (entropy, normalized_entropy) or None if total is zero or n == 1.
    """
    total = sum(counts)
    if total == 0:
        return None
    n = len(counts)
    if n == 1:
        return None

    # Compute Shannon entropy
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)

    # Normalize by log₂(n)
    max_entropy = math.log2(n)
    normalized = entropy / max_entropy if max_entropy > 0 else 0.0

    return (entropy, normalized)


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


def get_liveness_stats(conn: sqlite3.Connection, config: dict) -> dict:
    """Return liveness metrics: live share, dead age, and ghost likelihood.

    Computes three read-only aggregates:
    1. Liveness-%: share of non-terminal jobs that are not stale.
    2. Dead-age: mean/median days since archived jobs were closed (from pipeline_events).
    3. Ghost-likelihood: count of non-terminal jobs that are likely ghosts.

    All aggregates are safe on empty DB (return None/0, never raise).

    Args:
        conn: Open sqlite3 connection.
        config: App config dict (reads metrics.ghost_open_days / ghost_repost_days).

    Returns:
        dict with keys:
            live_share (float | None): non-stale / non-terminal ratio (None if denom=0)
            live_n (int): count of non-stale, non-terminal jobs
            live_denom (int): count of non-terminal jobs
            mean_dead_age_days (float | None): mean days since archived jobs closed
            median_dead_age_days (float | None): median days since archived jobs closed
            dead_age_n (int): count of archived jobs with archive event rows
            ghost_count (int): count of non-terminal jobs flagged as likely ghosts
            ghost_n (int): count of non-terminal jobs (ghost denominator)
    """
    # Terminal statuses from _queries._HIDDEN_STATUSES
    terminal_placeholders = ",".join("?" * len(_HIDDEN_STATUSES))

    # 1. Liveness-%: non-stale / non-terminal
    liveness_row = conn.execute(
        f"""SELECT
             SUM(CASE WHEN is_stale = 0 THEN 1 ELSE 0 END) AS live_n,
             COUNT(*) AS live_denom
           FROM jobs
           WHERE pipeline_status NOT IN ({terminal_placeholders})""",
        _HIDDEN_STATUSES,
    ).fetchone()

    live_n = liveness_row["live_n"] or 0
    live_denom = liveness_row["live_denom"] or 0
    live_share = live_n / live_denom if live_denom > 0 else None

    # 2. Dead-age: derive from pipeline_events archive rows
    # Close time = MAX(pe.timestamp WHERE pe.to_status='archived')
    # Only include jobs that HAVE an archive event row (excludes pre-event paths)
    dead_age_rows = conn.execute(
        """SELECT (julianday('now') - julianday(
                 (SELECT MAX(pe.timestamp) FROM pipeline_events pe
                  WHERE pe.job_id = j.dedup_key AND pe.to_status = 'archived')
               )) AS dead_age_days
           FROM jobs j
           WHERE j.pipeline_status = 'archived'
             AND EXISTS (SELECT 1 FROM pipeline_events pe
                         WHERE pe.job_id = j.dedup_key AND pe.to_status = 'archived')"""
    ).fetchall()

    dead_age_values = [
        row["dead_age_days"] for row in dead_age_rows if row["dead_age_days"] is not None
    ]
    dead_age_n = len(dead_age_values)

    mean_dead_age_days = None
    median_dead_age_days = None
    if dead_age_values:
        mean_dead_age_days = sum(dead_age_values) / dead_age_n
        median_dead_age_days = statistics.median(dead_age_values)

    # 3. Ghost-likelihood: composite of three sub-signals
    # Active/protected stages from stale_detector (applied onward)
    _PROTECTED_STATUSES = ("applied", "phone_screen", "technical", "onsite", "offer", "accepted")

    ghost_open_days = config.get("metrics", {}).get("ghost_open_days", 30)
    ghost_repost_days = config.get("metrics", {}).get("ghost_repost_days", 14)

    # Feature-detect ats_refreshed_at column (pre-#575 compatibility)
    columns_info = conn.execute("PRAGMA table_info(jobs)").fetchall()
    column_names = {col["name"] for col in columns_info}
    has_ats_refreshed_at = "ats_refreshed_at" in column_names

    # Build ghost query with conditional repost clause
    protected_placeholders = ",".join("?" * len(_PROTECTED_STATUSES))
    if has_ats_refreshed_at:
        # Full composite with repost detection
        ghost_query = f"""
            SELECT COUNT(*) AS ghost_count
            FROM jobs j
            WHERE pipeline_status NOT IN ({terminal_placeholders})
              AND (
                -- open_too_long: anchor on posted_date (exact/approx) or first_seen fallback
                (CASE
                   WHEN j.posted_date_precision IN ('exact', 'approximate')
                   THEN (julianday('now') - julianday(j.posted_date))
                   ELSE (julianday('now') - julianday(j.first_seen))
                 END) > ?
                -- zero_pursuit: no pipeline_events row with protected status
                AND NOT EXISTS (
                  SELECT 1 FROM pipeline_events pe
                  WHERE pe.job_id = j.dedup_key
                    AND pe.to_status IN ({protected_placeholders})
                )
                -- repost_detected OR cadence_unknown
                AND (
                  -- repost_detected: ats_refreshed_at diverges from posted_date
                  (j.ats_refreshed_at IS NOT NULL
                   AND (julianday(j.ats_refreshed_at) - julianday(j.posted_date)) > ?)
                  OR
                  -- cadence_unknown: ats_refreshed_at is NULL (non-Greenhouse or pre-#575)
                  j.ats_refreshed_at IS NULL
                )
              )
        """
        ghost_params = (
            _HIDDEN_STATUSES + (ghost_open_days,) + _PROTECTED_STATUSES + (ghost_repost_days,)
        )
    else:
        # Degraded composite: repost signal always cadence_unknown (column absent)
        ghost_query = f"""
            SELECT COUNT(*) AS ghost_count
            FROM jobs j
            WHERE pipeline_status NOT IN ({terminal_placeholders})
              AND (
                -- open_too_long: anchor on posted_date (exact/approx) or first_seen fallback
                (CASE
                   WHEN j.posted_date_precision IN ('exact', 'approximate')
                   THEN (julianday('now') - julianday(j.posted_date))
                   ELSE (julianday('now') - julianday(j.first_seen))
                 END) > ?
                -- zero_pursuit: no pipeline_events row with protected status
                AND NOT EXISTS (
                  SELECT 1 FROM pipeline_events pe
                  WHERE pe.job_id = j.dedup_key
                    AND pe.to_status IN ({protected_placeholders})
                )
                -- cadence_unknown: ats_refreshed_at column absent → always true
                AND 1=1
              )
        """
        ghost_params = _HIDDEN_STATUSES + (ghost_open_days,) + _PROTECTED_STATUSES

    ghost_count = conn.execute(ghost_query, ghost_params).fetchone()["ghost_count"] or 0

    # Ghost denominator: non-terminal jobs
    ghost_n_row = conn.execute(
        f"SELECT COUNT(*) AS ghost_n FROM jobs WHERE pipeline_status NOT IN ({terminal_placeholders})",
        _HIDDEN_STATUSES,
    ).fetchone()
    ghost_n = ghost_n_row["ghost_n"] or 0

    return {
        "live_share": live_share,
        "live_n": live_n,
        "live_denom": live_denom,
        "mean_dead_age_days": mean_dead_age_days,
        "median_dead_age_days": median_dead_age_days,
        "dead_age_n": dead_age_n,
        "ghost_count": ghost_count,
        "ghost_n": ghost_n,
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

    Each job dict includes dedup_key, title, company, classification (v3.0
    bucket), sub_scores_json (the v3.0 fit signal — the kanban card derives the
    6-30 composite from it), salary_min, salary_max, location, pipeline_status,
    first_seen, and days_in_stage (days since the job entered its current
    pipeline stage, based on the most recent pipeline_events record with
    matching to_status, falling back to first_seen).

    Ordered by the live 6-30 composite, NOT the legacy `jobs.score` column (which
    v3.0 scoring no longer writes — every current row would tie at 0).

    Returns:
        dict mapping pipeline_status (str) -> list of job dicts
    """
    rows = conn.execute(
        f"""SELECT j.dedup_key, j.title, j.company,
                  j.classification, j.sub_scores_json,
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
           ORDER BY {_SUB_SCORE_SUM_SQL} DESC"""
    ).fetchall()

    now = datetime.now(UTC).replace(tzinfo=None)
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

    from job_finder.web.location_normalizer import normalize_for_display, normalize_location

    rows = conn.execute(
        "SELECT locations_raw FROM jobs WHERE locations_raw IS NOT NULL AND locations_raw != ''"
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
            # Two-stage normalization: write-side normalize_location
            # (whitespace + placeholder drop) then display-side polish
            # (annotation strip, ZIP strip, trailing-country strip,
            # ALLCAPS fold, state-name -> 2-letter code). The display
            # stage is what collapses the user-reported "many San Jose
            # variants" into a single dropdown entry.
            normalized = normalize_location(loc)
            if normalized is None:
                continue
            display = normalize_for_display(normalized)
            if display is None:
                continue
            key = display.lower()
            by_lower_key.setdefault(key, display)

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


def get_crawl_latency_sli(conn: sqlite3.Connection, config: dict) -> dict:
    """Return crawl latency SLI metrics (p50/p95/p99 days between posted_date and first_seen).

    Computes steady-state crawl latency for ATS-direct sources (exact posted_date_precision)
    with three guards:
    1. posted_date_precision = 'exact' (machine first-posted sources only)
    2. posted_date != first_seen (excludes m095 copy artifacts)
    3. latency <= cold_start_exclude_days (excludes cold-start backlog)

    Args:
        conn: Open sqlite3 connection.
        config: App config dict (reads metrics.crawl_latency.cold_start_exclude_days).

    Returns:
        dict with keys:
            p50_days (float | None): 50th percentile latency in days
            p95_days (float | None): 95th percentile latency in days
            p99_days (float | None): 99th percentile latency in days
            sample_n (int): number of qualifying jobs in the percentile set
            total_dated (int): total jobs with posted_date (coverage denominator)
            exact_coverage_pct (float): percentage of dated jobs that are exact-precision
            cold_start_exclude_days (int): the cold-start window used
        Returns zeroed dict (no crash) on pre-m095 DBs (missing posted_date_precision column).
    """
    cold_start_days = (
        config.get("metrics", {})
        .get("crawl_latency", {})
        .get("cold_start_exclude_days", _DEFAULT_COLD_START_EXCLUDE_DAYS)
    )

    try:
        # Total dated jobs (coverage denominator)
        total_dated = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE posted_date IS NOT NULL"
        ).fetchone()[0]

        # Guarded, steady-state, ATS-direct latency values
        rows = conn.execute(
            """SELECT (julianday(first_seen) - julianday(posted_date)) AS lat
               FROM jobs
               WHERE posted_date_precision = 'exact'
                 AND posted_date IS NOT NULL
                 AND posted_date <> first_seen
                 AND (julianday(first_seen) - julianday(posted_date)) >= 0
                 AND (julianday(first_seen) - julianday(posted_date)) <= ?
               ORDER BY lat""",
            (cold_start_days,),
        ).fetchall()

        latencies = [row["lat"] for row in rows]
        qualifying = len(latencies)

        # Compute percentiles via nearest-rank on sorted list
        p50_days = p95_days = p99_days = None
        if qualifying > 0:
            latencies.sort()
            # Nearest-rank: index = ceil(percentile * n) - 1
            import math

            p50_idx = math.ceil(0.50 * qualifying) - 1
            p95_idx = math.ceil(0.95 * qualifying) - 1
            p99_idx = math.ceil(0.99 * qualifying) - 1
            p50_days = latencies[p50_idx]
            p95_days = latencies[p95_idx]
            p99_days = latencies[p99_idx]

        exact_coverage_pct = round(100 * qualifying / total_dated, 1) if total_dated else 0.0

        return {
            "p50_days": p50_days,
            "p95_days": p95_days,
            "p99_days": p99_days,
            "sample_n": qualifying,
            "total_dated": total_dated,
            "exact_coverage_pct": exact_coverage_pct,
            "cold_start_exclude_days": cold_start_days,
        }

    except sqlite3.OperationalError as exc:
        # Gracefully handle missing column (pre-m095 DB); re-raise other errors.
        if "no such column" in str(exc).lower():
            return {
                "p50_days": None,
                "p95_days": None,
                "p99_days": None,
                "sample_n": 0,
                "total_dated": 0,
                "exact_coverage_pct": 0.0,
                "cold_start_exclude_days": cold_start_days,
            }
        raise


def get_target_set_size(conn: sqlite3.Connection, fit_floor: float) -> int:
    """Return the count of jobs that are target-set members.

    A job is a target-set member when:
    - sub_scores_json IS NOT NULL (scored)
    - mean of the six sub-scores >= fit_floor
    - classification is NOT a hard negative (reject, low_signal)

    This is the single sanctioned count for downstream metrics (M6/M7, discovery dashboard).

    Args:
        conn: Open sqlite3 connection.
        fit_floor: Mean sub-score threshold (1.0-5.0 scale).

    Returns:
        Count of target-set member jobs.
    """
    where_clause = target_membership_sql(fit_floor)
    row = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
    return row[0] if row else 0


def get_surfaced_concentration(conn: sqlite3.Connection) -> dict:
    """Return concentration metrics for surfaced jobs (apply/consider classifications).

    Computes normalized HHI and Shannon entropy for two groupings:
    - by_employer: jobs.company_id (NULL → "_unlinked" sentinel)
    - by_platform: companies.ats_platform via LEFT JOIN (NULL/empty → "_unknown" sentinel)

    The surfaced cohort is defined by surfaced_classification_sql() (classification IN ('apply','consider')).

    Returns:
        dict with keys:
            by_employer: {hhi, entropy, entropy_norm, n_groups, total} or None fields
            by_platform: {hhi, entropy, entropy_norm, n_groups, total} or None fields
    """
    surfaced_clause = surfaced_classification_sql()

    # Employer grouping: NULL company_id → "_unlinked" sentinel
    employer_rows = conn.execute(
        f"""SELECT COALESCE(company_id, '_unlinked') as group_key, COUNT(*) as cnt
           FROM jobs
           WHERE {surfaced_clause}
           GROUP BY group_key"""
    ).fetchall()

    employer_counts = [row["cnt"] for row in employer_rows]
    employer_total = sum(employer_counts)

    employer_metrics = {
        "hhi": _normalized_hhi(employer_counts),
        "entropy": None,
        "entropy_norm": None,
        "n_groups": len(employer_counts),
        "total": employer_total,
    }
    if employer_total > 0:
        entropy_result = _shannon_entropy(employer_counts)
        if entropy_result:
            employer_metrics["entropy"], employer_metrics["entropy_norm"] = entropy_result

    # Platform grouping: LEFT JOIN companies, NULL/empty platform → "_unknown" sentinel
    platform_rows = conn.execute(
        f"""SELECT COALESCE(NULLIF(c.ats_platform, ''), '_unknown') as group_key, COUNT(*) as cnt
           FROM jobs j
           LEFT JOIN companies c ON j.company_id = c.id
           WHERE {surfaced_clause}
           GROUP BY group_key"""
    ).fetchall()

    platform_counts = [row["cnt"] for row in platform_rows]
    platform_total = sum(platform_counts)

    platform_metrics = {
        "hhi": _normalized_hhi(platform_counts),
        "entropy": None,
        "entropy_norm": None,
        "n_groups": len(platform_counts),
        "total": platform_total,
    }
    if platform_total > 0:
        entropy_result = _shannon_entropy(platform_counts)
        if entropy_result:
            platform_metrics["entropy"], platform_metrics["entropy_norm"] = entropy_result

    return {
        "by_employer": employer_metrics,
        "by_platform": platform_metrics,
    }
