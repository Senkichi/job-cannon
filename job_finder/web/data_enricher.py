"""Data enrichment module for sparse job records.

Cost-ordered enrichment pipeline with fetch-only tiers. Each tier is only
attempted after all cheaper tiers have been exhausted. Per-job
enrichment_tier column tracks the highest tier attempted so future calls
resume from the next tier.

Enrichment tiers (in order):
  1. free      — Direct URL fetch, ATS API query, HTML careers scrape
  2. ddg       — DuckDuckGo web search + URL fetch (free, no key)
  3. serpapi   — SerpAPI Google Jobs search (paid, optional key)
  4. agentic   — Ollama-driven query gen + Playwright fetch (deepest fallback)
  5. exhausted — All tiers attempted; never re-enrich

Per-field cost ceilings:
  jd_full:    escalates all the way to agentic (critical for AI scoring)
  salary_min: capped at ddg (extracted post-fetch from jd_full when present)
  salary_max: capped at ddg

The previous LLM-synthesis tiers (haiku, sonnet) were removed in Phase 2b
sub-fix RC4: they fabricated short pseudo-JDs from search-result fragments
and blocked escalation to fetch tiers that actually retrieved the real JD.
Structured-field extraction (salary, location) now happens post-fetch from
jd_full via parse_structured_fields() (Phase 2c).

Design principles:
  - Never raises — all errors are caught and logged.
  - Returns empty dict when nothing can be enriched.
  - Skips enrichment when job already has all scoring-relevant data.
  - Persists enrichment_tier atomically with enriched fields in one UPDATE.
  - Jobs with enrichment_tier set resume from the NEXT tier up.
  - Exhausted jobs are returned immediately without any API calls.

Exports:
    TIER_ORDER: Ordered list of enrichment tier names.
    enrich_job: Enrich a sparse job record with cost-ordered tier fallback.
    run_enrichment_backfill: Backfill unenriched jobs from the DB.
"""

import json
import logging
from typing import Any

from job_finder.web.company_enricher import (
    enrich_company_info,  # noqa: F401 (re-exported for callers)
)
from job_finder.web.enrichment_sources import merge_apply_urls
from job_finder.web.enrichment_tiers import (
    fetch_ddg_jds,
    fetch_direct_jd,
    query_ats_api,
    scrape_careers,
    search_ddg_web,
    search_duckduckgo,
    search_serpapi,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict cost ordering: free (URL -> ATS -> careers) -> DDG -> SerpAPI -> agentic
TIER_ORDER = ["free", "ddg", "serpapi", "agentic", "exhausted"]

# Allowlist of jobs table columns that _persist() may write. Prevents AI-extracted
# dict keys from injecting arbitrary column names into dynamic SQL SET clauses.
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max", "location"})

# Per-field cost ceilings: highest tier allowed to search for this field.
# After this tier fails for a field, it is abandoned (not escalated further).
FIELD_TIER_CEILINGS = {
    "jd_full": "agentic",  # escalate all the way — critical for downstream scoring
    "salary_min": "ddg",  # cap at ddg — extracted post-fetch from jd_full
    "salary_max": "ddg",
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_job(
    job_row: dict,
    serpapi_key: str | None = None,
    conn: Any = None,
    config: dict | None = None,
) -> dict:
    """Enrich a sparse job record using the cost-ordered tier pipeline.

    Tiers: free (URL -> ATS -> careers) -> DDG -> Haiku -> SerpAPI -> Sonnet.
    Resumes from the next tier after job_row['enrichment_tier'] if set.
    Returns {} immediately for exhausted jobs.

    Persists enrichment_tier + enriched fields atomically to DB after each
    tier that produces data (if conn is provided). Returns the enriched dict.

    Args:
        job_row: Job record dict. Must have 'title' and 'company'.
        serpapi_key: Optional SerpAPI API key for SerpAPI tier.
        anthropic_client: Optional Anthropic client for Haiku/Sonnet tiers.
        conn: Optional SQLite connection for DB persistence and cost recording.
        config: Optional application config dict.

    Returns:
        Dict of enriched fields to UPDATE into the jobs table.
        Returns empty dict if nothing was enriched or job already has data.
    """
    if config is None:
        config = {}

    try:
        # Exhausted jobs: skip immediately
        current_tier = job_row.get("enrichment_tier")
        if current_tier == "exhausted":
            return {}

        # Auto-promote long descriptions to jd_full (DQ-02)
        if (
            not job_row.get("jd_full")
            and job_row.get("description")
            and len(job_row["description"]) > 200
        ):
            job_row["jd_full"] = job_row["description"]
            if conn is not None and job_row.get("dedup_key"):
                try:
                    conn.execute(
                        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ? AND jd_full IS NULL",
                        (job_row["description"][:8000], job_row.get("dedup_key")),
                    )
                    conn.commit()
                except Exception as e:
                    logger.debug("Description promotion DB write failed: %s", e)

        # Check if enrichment is needed
        missing = _find_missing_fields(job_row)
        if not missing:
            return {}

        # Determine start tier (resume from next tier after last attempted)
        start_idx = _start_tier_index(current_tier)

        title = job_row.get("title", "")
        company = job_row.get("company", "")

        # Accumulate fragments across tiers (each tier adds its text/data)
        fragments: dict = {}

        # ---------------------------------------------------------------
        # Tier 0: free — URL fetch + ATS API + careers scrape
        # ---------------------------------------------------------------
        if start_idx <= TIER_ORDER.index("free"):
            try:
                # Sub-tier A: Direct URL fetch
                source_urls = _parse_source_urls(job_row.get("source_urls"))
                for url in source_urls:
                    jd_text = fetch_direct_jd(url)
                    if jd_text:
                        fragments["url_jd"] = jd_text
                        break

                # Sub-tier B: ATS API query (if company has confirmed ATS slug)
                if conn is not None and job_row.get("company_id"):
                    ats_result = query_ats_api(job_row, conn, config)
                    if ats_result:
                        fragments.update(ats_result)

                # Sub-tier C: HTML careers scrape (if company has homepage_url)
                if conn is not None and job_row.get("company_id"):
                    careers_result = scrape_careers(job_row, conn, config)
                    if careers_result:
                        # Don't overwrite ATS result
                        for k, v in careers_result.items():
                            if k not in fragments:
                                fragments[k] = v

                # Resolve what free tier found
                enriched = _resolve_from_fragments(fragments, missing, job_row)
                if enriched:
                    _persist(conn, job_row, enriched, "free")
                    return enriched

            except Exception as e:
                logger.debug("Free tier enrichment failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 1: ddg — DuckDuckGo Instant Answer API
        # ---------------------------------------------------------------
        # Check if remaining missing fields are all below DDG (nothing to do)
        remaining = _find_missing_fields(
            {**job_row, **_resolve_from_fragments(fragments, missing, job_row)}
        )
        if not remaining:
            enriched = _resolve_from_fragments(fragments, missing, job_row)
            _persist(conn, job_row, enriched, "free")
            return enriched

        if start_idx <= TIER_ORDER.index("ddg"):
            try:
                ddg_result = search_ddg_web(title, company)
                ddg_text = ddg_result.get("ddg_snippet", "")

                ddg_jd, ddg_source_url = fetch_ddg_jds(ddg_result.get("ddg_urls", []))
                if ddg_jd:
                    fragments["url_jd"] = ddg_jd

                query = f"{title} {company} job description"
                fallback_text = search_duckduckgo(query)
                ddg_parts = [text for text in (ddg_text, fallback_text) if text]
                if ddg_parts:
                    fragments["ddg"] = "\n\n".join(ddg_parts)

                if ddg_source_url and conn and job_row.get("dedup_key"):
                    merge_apply_urls(conn, job_row["dedup_key"], [ddg_source_url])

                # Resolve what DDG tier found (via Haiku extraction later if needed)
                # DDG doesn't directly provide structured data; it feeds the Haiku tier.
                # If DDG returned nothing, we still continue to Haiku with empty ddg fragment.

            except Exception as e:
                logger.debug("DDG tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 2: serpapi — Google Jobs search (paid)
        # ---------------------------------------------------------------
        # SerpAPI only runs if JD is still missing (salary ceiling is ddg)
        jd_still_missing = not (
            job_row.get("jd_full") or fragments.get("url_jd") or fragments.get("jd_full")
        )

        if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing:
            try:
                query = f"{title} {company}"
                serpapi_result, _apply_urls = search_serpapi(query, serpapi_key)
                if serpapi_result:
                    for k, v in serpapi_result.items():
                        if k not in fragments:
                            fragments[k] = v

                    enriched = _resolve_from_fragments(
                        {**fragments, **serpapi_result}, missing, job_row
                    )
                    if enriched:
                        _persist(conn, job_row, enriched, "serpapi")
                        return enriched

            except Exception as e:
                logger.debug("SerpAPI tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 3: agentic — Ollama-driven query + Playwright fetch
        # ---------------------------------------------------------------
        # Agentic only runs if JD is still missing. Wired in Phase 2b sub-fix 2/2
        # via enrich_one_job() in agentic_enricher.
        jd_still_missing = not (
            job_row.get("jd_full") or fragments.get("url_jd") or fragments.get("jd_full")
        )

        if start_idx <= TIER_ORDER.index("agentic") and jd_still_missing:
            # Placeholder: actual call wired in 2b.3. Branch is left in place so the
            # tier ordering and persistence semantics are stable while the per-job
            # entry point (agentic_enricher.enrich_one_job) is being extracted.
            pass

        # All tiers exhausted
        _persist(conn, job_row, {}, "exhausted")
        return {}

    except Exception as e:
        logger.warning("enrich_job failed for '%s': %s", job_row.get("title"), e)
        return {}


def run_enrichment_backfill(
    db_path: str,
    serpapi_key: str | None = None,
    config: dict | None = None,
    limit: int = 100,
) -> int:
    """Backfill unenriched jobs using the cost-ordered tier pipeline.

    Queries jobs where enrichment_tier IS NULL or in a resumable state
    (not 'exhausted', 'serpapi', or 'sonnet' — those are already done).
    Processes up to `limit` jobs per call.

    Args:
        db_path: Absolute path to the SQLite database file.
        serpapi_key: Optional SerpAPI API key.
        config: Optional application config dict.
        limit: Max number of jobs to process per call.

    Returns:
        Number of jobs that were enriched (had fields added).
    """
    from job_finder.web.db_helpers import standalone_connection

    if config is None:
        config = {}

    with standalone_connection(db_path) as conn:
        # Only select rows that (a) are in a resumable enrichment tier AND
        # (b) are actually missing at least one field _find_missing_fields()
        # checks. Without the (b) clause, LIMIT N silently returns the first
        # N rows in rowid order — which are the oldest, already-enriched
        # rows — and enrich_job() returns {} for each, leaving the real
        # backlog unreached. ORDER BY first_seen DESC further prioritises the
        # freshest rows, which are the ones users are actively viewing.
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE (enrichment_tier IS NULL
                      OR enrichment_tier NOT IN ('exhausted', 'serpapi', 'sonnet'))
                 AND (jd_full IS NULL OR jd_full = '' OR salary_min IS NULL)
               ORDER BY first_seen DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        enriched_count = 0
        for row in rows:
            job_row = dict(row)
            result = enrich_job(
                job_row,
                serpapi_key=serpapi_key,
                conn=conn,
                config=config,
            )
            if result:
                enriched_count += 1

        return enriched_count


# ---------------------------------------------------------------------------
# Private helpers: tier logic utilities
# ---------------------------------------------------------------------------


def _find_missing_fields(job_row: dict) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for Sonnet)
    - salary_min: minimum salary

    Returns empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if not job_row.get("jd_full"):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    return missing


def _filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def _start_tier_index(current_tier: str | None) -> int:
    """Return the index in TIER_ORDER to start from based on current_tier.

    If current_tier is None or 'free', start from 0 (beginning).
    Otherwise, start from the tier AFTER current_tier.

    Args:
        current_tier: The enrichment_tier value from the job row.

    Returns:
        Index into TIER_ORDER to start enrichment from.
    """
    if current_tier is None:
        return 0
    try:
        idx = TIER_ORDER.index(current_tier)
        return idx + 1  # Resume from NEXT tier
    except ValueError:
        return 0


def _parse_source_urls(source_urls_json: str | None) -> list:
    """Parse source_urls JSON field into a list of URL strings.

    Args:
        source_urls_json: JSON string like '["https://..."]' or None.

    Returns:
        List of URL strings. Empty list if None or unparseable.
    """
    if not source_urls_json:
        return []
    try:
        urls = json.loads(source_urls_json)
        return [u for u in urls if isinstance(u, str)]
    except (json.JSONDecodeError, TypeError):
        return []


def _resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Args:
        fragments: Dict of collected data from free-tier sources.
        missing: List of field names that are still missing.
        job_row: Original job row for reference.

    Returns:
        Dict of {field: value} for fields that fragments can satisfy.
    """
    enriched = {}
    for field in missing:
        # Direct key match
        if field in fragments and fragments[field] is not None:
            enriched[field] = fragments[field]
        # url_jd maps to jd_full
        elif field == "jd_full" and fragments.get("url_jd"):
            enriched["jd_full"] = fragments["url_jd"]

    return _filter_non_none(enriched)


def _persist(conn: Any, job_row: dict, enriched: dict, tier_name: str) -> None:
    """Persist enriched fields + enrichment_tier atomically in a single UPDATE.

    Only writes to DB if conn is provided. If enriched is empty, still
    updates enrichment_tier to track progress (unless conn is None).

    Args:
        conn: Open SQLite connection. If None, skip persistence.
        job_row: Job row dict (must have 'dedup_key').
        enriched: Dict of {column_name: value} to update.
        tier_name: The enrichment tier name to record.
    """
    if conn is None:
        return

    dedup_key = job_row.get("dedup_key")
    if not dedup_key:
        return

    try:
        if enriched:
            # Filter to allowlisted columns only — prevents AI-extracted keys from
            # injecting arbitrary column names into the dynamic SQL SET clause.
            safe_enriched = {k: v for k, v in enriched.items() if k in _ENRICHABLE_COLUMNS}
            if safe_enriched != enriched:
                unknown = set(enriched) - _ENRICHABLE_COLUMNS
                logger.warning("_persist: dropping non-allowlisted columns: %s", unknown)
        else:
            safe_enriched = {}

        if safe_enriched:
            set_clauses = ", ".join(f"{k} = ?" for k in safe_enriched)
            set_clauses += ", enrichment_tier = ?"
            values = list(safe_enriched.values()) + [tier_name, dedup_key]
            conn.execute(
                f"UPDATE jobs SET {set_clauses} WHERE dedup_key = ?",
                values,
            )
        else:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (tier_name, dedup_key),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to persist enrichment for '%s': %s", dedup_key, e)
