"""Pre-scoring exclusion filter. Zero API cost -- pure string matching.

Provides should_exclude() to determine whether a job should be skipped
before any scoring call, based on title keywords, excluded companies,
and a configurable salary floor.
"""

import logging

from job_finder.config import COMPANY_DENYLIST, get_company_denylist
from job_finder.normalizers import normalize_company
from job_finder.web.job_scorer import scoring_precheck

logger = logging.getLogger(__name__)

# ── Single source of truth for "which unscored jobs are scorable" ────────────
#
# The dashboard "Score N unscored jobs" tile, the batch-session ``total``, and
# the batch worker's per-row decision MUST agree on the same set, or the button
# advertises jobs the worker silently no-ops and its count never decrements after
# a click (the recurring Score-Now desync — bitten on the jd_full gate, then the
# P3.2 location gate, then again every time the two implementations drift).
#
# Earlier fixes kept a SECOND, SQL re-implementation of the scoring gates inside
# count_scorable and pinned it to the Python source with a parity test. That is
# inherently fragile: SQL and Python disagree on JSON / NULL / whitespace edge
# cases the fixtures never exercise (e.g. ``locations_structured = 'null'`` or
# ``'[ ]'`` — not literally ``''``/``'[]'`` so SQL calls them "location-ready",
# but Python parses them to an empty list and gates them), and every new gate has
# to be mirrored in two languages in lockstep. So the SQL copy is gone. There is
# now ONE definition of "scorable":
#
#   * SCORABLE_CANDIDATE_WHERE — the cheap, index-friendly SQL pre-filter that
#     selects the UNSCORED candidate universe. Shared verbatim by count_scorable
#     AND the batch worker's SELECT, so they cannot disagree on the universe.
#   * is_scorable(job, config)  — the pure Python predicate (should_exclude +
#     scoring_precheck) the worker applies per row. count_scorable counts the
#     candidates that pass it; the worker scores them. Same functions, no drift.
#
# Any future scoring gate added to scoring_precheck is reflected in the count
# automatically — there is no SQL translation left to forget to update.

# Cheap pre-filter: the unscored candidate universe (classification IS NULL),
# minus dismissed/archived and quarantined (I-16/I-17) rows. This is the SQL the
# worker SELECTs; count_scorable SELECTs the same set. Per-row scorability is then
# decided in Python by is_scorable() — NOT in SQL — so the two never diverge.
SCORABLE_CANDIDATE_WHERE = (
    "classification IS NULL "
    "AND pipeline_status NOT IN ('dismissed', 'archived') "
    "AND COALESCE(unresolved_reasons, '[]') = '[]'"
)

# Freshest-first scoring queue; served by idx_jobs_last_seen. Used by the worker
# (count_scorable does not care about order).
SCORABLE_CANDIDATE_ORDER_BY = "ORDER BY last_seen DESC"


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


# Lightweight projection for counting: the only columns is_scorable's gates
# read. jd_full is projected to a presence sentinel ('x' when non-blank, ''
# otherwise) so a dashboard render never streams full JD bodies (up to
# JD_STORAGE_MAX_CHARS each, for every unscored row, on every quick-actions
# refresh) merely to answer a yes/no — scoring_precheck only needs jd presence.
_SCORABLE_COLS = (
    "title, company, salary_max, "
    "locations_structured, location, enrichment_tier, unresolved_reasons, "
    "CASE WHEN TRIM(COALESCE(jd_full, '')) <> '' THEN 'x' ELSE '' END AS jd_full"
)


def is_scorable(job: dict, config: dict) -> bool:
    """Pure predicate: would the batch worker actually SCORE this row?

    THE single source of truth for "scorable", shared by ``count_scorable`` (the
    dashboard tile + batch ``total``) and the batch worker. A candidate row is
    scorable iff it is not excluded (``should_exclude``) and passes every
    completeness gate (``scoring_precheck`` returns ``None``) — the exact two
    checks the worker applies per row, in the same order. Because the count and
    the worker call the SAME functions, the tile can never advertise a job the
    worker silently no-ops.

    Pure: no I/O, no mutation. (The worker layers its exclusion auto-dismiss
    side effect on top separately; counting must never mutate.) Callers pass
    rows from SCORABLE_CANDIDATE_WHERE, so classification / pipeline_status /
    quarantine are already filtered in SQL; this adds the per-row exclusion +
    completeness gates that are impractical to express faithfully in SQL.
    """
    exclusions = config.get("profile", {}).get("exclusions", {})
    min_salary = config.get("profile", {}).get("min_salary")
    if should_exclude(job, exclusions, min_salary, config=config)[0]:
        return False
    return scoring_precheck(job) is None


def count_scorable(conn, config: dict) -> int:
    """Count unscored jobs the batch worker would actually score.

    Single-source design: SELECT the coarse candidate universe via the shared
    ``SCORABLE_CANDIDATE_WHERE`` (identical to the worker's SELECT), then count
    the rows that pass ``is_scorable`` — the SAME pure Python predicate
    (``should_exclude`` + ``scoring_precheck``) the worker applies per row.

    This replaces a prior SQL re-implementation of the scoring gates. A parallel
    SQL translation of ``scoring_precheck`` drifted from the Python source every
    time a gate was added or the data hit an untested JSON/NULL edge case (e.g.
    ``locations_structured = 'null'`` or ``'[ ]'``), producing the recurring
    "Score N unscored" tile that counts rows the worker no-ops and never
    decrements. Deriving the count from the worker's own predicate makes that
    desync structurally impossible. The candidate universe is the UNSCORED set
    (``classification IS NULL``), which is inherently small, so the per-row
    Python pass is cheap.

    Returns 0 (and logs a WARNING with traceback) on any DB error — a dashboard
    render must never 500 because the count query failed.
    """
    try:
        cur = conn.execute(f"SELECT {_SCORABLE_COLS} FROM jobs WHERE {SCORABLE_CANDIDATE_WHERE}")
        cols = [d[0] for d in cur.description]
        return sum(1 for row in cur if is_scorable(dict(zip(cols, row, strict=True)), config))
    except Exception:
        logger.warning("count_scorable failed; returning 0", exc_info=True)
        return 0
