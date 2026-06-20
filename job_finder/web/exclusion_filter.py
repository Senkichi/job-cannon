"""Pre-scoring exclusion filter. Zero API cost -- pure string matching.

Provides should_exclude() to determine whether a job should be skipped
before any scoring call, based on title keywords, excluded companies,
and a configurable salary floor.
"""

import logging

from job_finder.config import COMPANY_DENYLIST, get_company_denylist
from job_finder.normalizers import normalize_company

logger = logging.getLogger(__name__)


def _normalize_company_sql(company: str | None) -> str:
    """NULL-safe normalize_company wrapper for use as a SQLite UDF.

    SQLite passes None for NULL company columns; normalize_company expects a str.
    """
    if not company:
        return ""
    return normalize_company(company)


def should_exclude(
    job_row: dict,
    exclusions: dict,
    min_salary: int | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    """Check if a job should be excluded before scoring.

    Args:
        job_row: Job record dict with at minimum: title (str), company (str),
                 salary_max (int|None).
        exclusions: Dict with optional keys:
                    - title_keywords (list[str]): Substrings to match against job title.
                    - companies (list[str]): Company names to exclude.
        min_salary: Candidate's minimum acceptable salary. If provided and salary_max
                    is disclosed and < min_salary * 0.85, the job is excluded.
                    Pass None to skip salary floor check.
        config: Optional full config dict. If provided, merges config.yaml
                filters.company_denylist entries with hardcoded defaults.
                If None, only the hardcoded COMPANY_DENYLIST is used.

    Returns:
        (True, reason_string) if the job should be excluded, (False, "") otherwise.
        Returns the first matching exclusion reason (title keywords checked first,
        then companies, then salary floor).
    """
    title = job_row.get("title", "") or ""
    company = job_row.get("company", "") or ""
    salary_max = job_row.get("salary_max")

    title_lower = title.lower()
    # Normalize the stored brand the same way the denylist is normalized, so
    # legal-entity-suffix variants match (#213): "Virtual Vocations Inc" and a
    # denylist entry of "Virtual Vocations" both reduce to "virtual vocations".
    company_normalized = normalize_company(company)

    # 1. Title keyword exclusions (case-insensitive substring match)
    for keyword in exclusions.get("title_keywords", []):
        if not keyword:
            continue
        if keyword.lower() in title_lower:
            return True, f"Title contains excluded keyword: '{keyword}'"

    # 2. Company exclusions (config + denylist), compared on normalize_company so
    #    suffix variants ("Acme, Inc." == "Acme") and aggregator re-posters fire.
    #    User-supplied exclusions.companies are normalized to the same form.
    excluded_companies = {normalize_company(c) for c in exclusions.get("companies", []) if c}
    # Merge in the denylist (hardcoded defaults + optional config entries, already normalized)
    denylist = get_company_denylist(config) if config else COMPANY_DENYLIST
    excluded_companies_set = excluded_companies | denylist
    if company_normalized and company_normalized in excluded_companies_set:
        return True, f"Excluded company: '{company.strip()}'"

    # 3. Salary floor check (only when min_salary provided and salary_max disclosed)
    if (
        min_salary is not None
        and salary_max is not None
        and isinstance(salary_max, (int, float))
        and salary_max > 0
    ):
        floor = min_salary * 0.85
        if salary_max < floor:
            return True, f"Max salary ${salary_max:,} below floor ${min_salary:,}"

    return False, ""


def count_scorable(conn, config: dict) -> int:
    """Count unscored jobs that would pass the exclusion filter.

    v3.0 (Phase 34 Plan 3 Commit A): predicate changed from
    `haiku_score IS NULL` to `classification IS NULL` — the unified scorer
    populates `classification` on every row it processes, so unclassified rows
    are the correct "unscored" set.

    Also requires non-empty ``jd_full`` because the v3 unified scorer skips
    rows without a job description (job_scorer.score_job returns
    status="skipped" on empty jd_full and never persists classification).
    Without this filter, the dashboard "Score N unscored jobs" button would
    advertise rows that the worker silently no-ops, producing the symptom
    where the count never decreases after clicking.

    Replicates the three exclusion checks from should_exclude() in SQL so the
    count matches what the batch scorer will actually attempt to score:
    1. Title keyword exclusions (case-insensitive substring)
    2. Company denylist + config exclusions (matched on normalize_company)
    3. Salary floor (salary_max < min_salary * 0.85)

    The company check registers normalize_company as a SQLite UDF so the SQL
    predicate uses the exact same canonical form as should_exclude (#213).
    Without this, a denylist of normalized bare names ("virtual vocations")
    would miss the suffixed brands the SERP sources actually store
    ("Virtual Vocations Inc"), and the dashboard "N unscored" tile would drift
    from what the scorer dismisses.
    """
    try:
        # Register normalize_company as a UDF for normalized-brand comparison.
        # deterministic=True lets SQLite cache/optimize; harmless if the binding
        # already exists (re-registration is idempotent).
        try:
            conn.create_function(
                "normalize_company", 1, _normalize_company_sql, deterministic=True
            )
        except TypeError:
            # Older SQLite builds without the deterministic kwarg.
            conn.create_function("normalize_company", 1, _normalize_company_sql)

        conditions = [
            "classification IS NULL",
            "jd_full IS NOT NULL",
            "TRIM(jd_full) != ''",
            "pipeline_status NOT IN ('dismissed', 'archived')",
            # Quarantine gate (I-16/I-17): mirror the batch scorer's candidate
            # SELECT so the dashboard "N unscored" tile matches what the worker
            # actually attempts (a quarantined row is withheld from scoring).
            "COALESCE(unresolved_reasons, '[]') = '[]'",
        ]
        params: list = []

        exclusions = config.get("profile", {}).get("exclusions", {})

        for keyword in exclusions.get("title_keywords", []):
            if keyword:
                conditions.append("LOWER(title) NOT LIKE ?")
                params.append(f"%{keyword.lower()}%")

        excluded_companies = {normalize_company(c) for c in exclusions.get("companies", []) if c}
        excluded_companies |= get_company_denylist(config)
        excluded_companies.discard("")
        if excluded_companies:
            placeholders = ",".join("?" * len(excluded_companies))
            conditions.append(f"normalize_company(company) NOT IN ({placeholders})")
            params.extend(sorted(excluded_companies))

        min_salary = config.get("profile", {}).get("min_salary")
        if min_salary is not None:
            floor = min_salary * 0.85
            conditions.append("NOT (salary_max IS NOT NULL AND salary_max > 0 AND salary_max < ?)")
            params.append(floor)

        where = " AND ".join(conditions)
        return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0]
    except Exception:
        logger.warning("count_scorable failed; returning 0", exc_info=True)
        return 0


# Back-compat alias — Plan 4 removes along with the rest of the legacy nomenclature.
# PLAN-4-REMOVE
count_haiku_scorable = count_scorable
