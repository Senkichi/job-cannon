"""Data enrichment module for sparse job records.

Cost-ordered enrichment pipeline with 7 tiers. Each tier is only attempted
after all cheaper tiers have been exhausted. Per-job enrichment_tier column
tracks the highest tier attempted so future calls resume from the next tier.

Enrichment tiers (in order):
  1. free — Direct URL fetch, ATS API query, HTML careers scrape
  2. ddg  — DuckDuckGo Instant Answer API (free, no key)
  3. haiku — Haiku extraction from accumulated fragments
  4. serpapi — SerpAPI Google Jobs search (paid, optional key)
  5. sonnet — Sonnet deep extraction from all accumulated fragments
  6. exhausted — All tiers attempted; never re-enrich

Per-field cost ceilings:
  jd_full:    escalates all the way to sonnet (critical for AI scoring)
  salary_min: capped at haiku (not worth SerpAPI/Sonnet for salary alone)
  salary_max: capped at haiku

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
from typing import Optional, Any

from job_finder.web.claude_client import cost_gate
from job_finder.web.domain_policy import is_blocked_domain
from job_finder.web.enrichment_tiers import (
    TransientEnrichmentError,
    fetch_direct_jd, fetch_linkedin_jd, query_ats_api, scrape_careers,
    extract_with_sonnet, search_serpapi, extract_with_haiku,
    search_ddg_web, fetch_ddg_jds,
)
from job_finder.web.company_enricher import enrich_company_info  # noqa: F401 (re-exported for callers)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict cost ordering: free (URL -> ATS -> careers) -> DDG -> Haiku -> SerpAPI -> Sonnet
TIER_ORDER = ["free", "ddg", "haiku", "serpapi", "sonnet", "exhausted"]

# Allowlist of jobs table columns that _persist() may write. Prevents AI-extracted
# dict keys from injecting arbitrary column names into dynamic SQL SET clauses.
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max", "location"})

# Minimum character length for jd_full to be considered a real job description.
# Anything shorter is likely a title restatement or placeholder from the AI.
_MIN_JD_LENGTH = 200

# Per-field cost ceilings: highest tier allowed to search for this field.
# After this tier fails for a field, it is abandoned (not escalated further).
FIELD_TIER_CEILINGS = {
    "jd_full": "sonnet",      # worth escalating all the way (critical for scoring)
    "salary_min": "haiku",    # cap at Haiku — not worth SerpAPI/Sonnet for salary alone
    "salary_max": "haiku",
}


# ---------------------------------------------------------------------------
# Stub JD detection
# ---------------------------------------------------------------------------


def is_stub_jd(jd_text: Optional[str], title: str = "", company: str = "") -> bool:
    """Return True if jd_text is a stub (title restatement or too short to be useful).

    A real job description contains responsibilities, qualifications, etc.
    Stubs are produced when AI extraction echoes the title back as jd_full
    because no real JD content was available in the input fragments.

    Args:
        jd_text: The jd_full value to check.
        title: Job title for overlap detection.
        company: Company name for overlap detection.

    Returns:
        True if jd_text is missing, too short, or a title restatement.
    """
    if not jd_text or not jd_text.strip():
        return True

    if len(jd_text.strip()) < _MIN_JD_LENGTH:
        return True

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_job(
    job_row: dict,
    serpapi_key: Optional[str] = None,
    anthropic_client: Any = None,
    conn: Any = None,
    config: Optional[dict] = None,
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

        # Secondary defense-in-depth guard for agentic tiers.
        # Primary gate is _ELIGIBLE_TIERS_QUERY in backfill_enrichment.py which
        # prevents the batch pipeline from ever fetching these rows. This guard
        # protects all DIRECT callers of enrich_job() that bypass the query gate
        # entirely (e.g., ad-hoc scripts, future callers with pre-fetched rows).
        if current_tier in ("agentic", "agentic_exhausted"):
            return {}

        # Auto-promote long descriptions to jd_full (DQ-02)
        if not job_row.get("jd_full") and job_row.get("description") and len(job_row["description"]) > 200:
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
                    # LinkedIn guest pages need targeted extraction — the
                    # generic fetcher rejects them due to auth-wall chrome.
                    if "linkedin.com/jobs/" in url:
                        jd_text = fetch_linkedin_jd(url)
                    # Blocked domains (glassdoor, indeed, etc.) return 403 or
                    # Cloudflare challenges. Skip via centralized domain policy
                    # rather than the previous inline "glassdoor.com/" string check.
                    elif is_blocked_domain(url):
                        logger.debug("Skipping blocked domain URL: %s", url)
                        continue
                    else:
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
        # Check if remaining missing fields are all below DDG (nothing to do).
        # Re-resolve here handles the partial-exception case where sub-tiers A/B
        # populated fragments but L164 was never reached due to an exception.
        enriched_from_free = _resolve_from_fragments(fragments, missing, job_row)
        if not _find_missing_fields({**job_row, **enriched_from_free}):
            _persist(conn, job_row, enriched_from_free, "free")
            return enriched_from_free

        if start_idx <= TIER_ORDER.index("ddg"):
            try:
                ddg_result = search_ddg_web(title, company)

                # Sub-tier A: Try fetching JDs from DDG URLs
                if ddg_result.get("ddg_urls"):
                    jd_text, source_url = fetch_ddg_jds(
                        ddg_result["ddg_urls"], title=title, company=company,
                    )
                    if jd_text:
                        fragments["url_jd"] = jd_text
                        # Persist discovered URL to source_urls
                        if conn is not None and job_row.get("dedup_key") and source_url:
                            _merge_apply_urls(conn, job_row["dedup_key"], [source_url])

                # Sub-tier B: Save snippets for Haiku extraction
                if ddg_result.get("ddg_snippet"):
                    fragments["ddg"] = ddg_result["ddg_snippet"]

                # If DDG URL fetch found a real JD, resolve and return
                enriched = _resolve_from_fragments(fragments, missing, job_row)
                if enriched and not is_stub_jd(enriched.get("jd_full"), title, company):
                    _persist(conn, job_row, enriched, "ddg")
                    return enriched

            except Exception as e:
                logger.debug("DDG tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 2: haiku — Extract structured data from accumulated fragments
        # ---------------------------------------------------------------
        if start_idx <= TIER_ORDER.index("haiku") and anthropic_client is not None:
            try:
                # Compose search text from all fragments collected so far.
                # None means no meaningful fragments — skip AI extraction entirely
                # to avoid hallucinating JDs from titles alone.
                search_input = _compose_fragment_text(fragments, title, company)
                if search_input is None:
                    logger.debug(
                        "No fragments for '%s' @ '%s' — skipping Haiku extraction",
                        title, company,
                    )
                    # Still advance tier so SerpAPI/Sonnet (which search independently)
                    # can be attempted on next pass.
                else:
                    haiku_result = extract_with_haiku(
                        search_input, job_row, anthropic_client, conn, config
                    )
                    if haiku_result:
                        for k, v in haiku_result.items():
                            fragments[k] = v

                    # Check what is still missing after Haiku
                    salary_fields = {"salary_min", "salary_max"}
                    enriched_so_far = _resolve_from_fragments(fragments, missing, job_row)
                    still_missing_after_haiku = [
                        f for f in missing if f not in enriched_so_far
                    ]

                    if not still_missing_after_haiku:
                        # All fields satisfied — return now
                        _persist(conn, job_row, enriched_so_far, "haiku")
                        return enriched_so_far

                    # Check salary ceiling: if ONLY salary fields remain missing after Haiku,
                    # stop escalating (salary ceiling is Haiku).
                    if all(f in salary_fields for f in still_missing_after_haiku):
                        # Only salary remains missing — don't escalate to SerpAPI/Sonnet for salary
                        _persist(conn, job_row, enriched_so_far if enriched_so_far else {}, "haiku")
                        return enriched_so_far

                    # Otherwise, some non-salary field (jd_full) is still missing —
                    # partial results from Haiku are accumulated in fragments for use by
                    # SerpAPI/Sonnet; continue escalation.

            except Exception as e:
                logger.debug("Haiku tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 3: serpapi — Google Jobs search (paid)
        # ---------------------------------------------------------------
        # SerpAPI only runs if JD is still missing (salary ceiling is Haiku).
        # Use is_stub_jd to catch title restatements from prior AI tiers.
        jd_still_missing = (
            is_stub_jd(job_row.get("jd_full"), title, company)
            and is_stub_jd(fragments.get("url_jd"), title, company)
            and is_stub_jd(fragments.get("jd_full"), title, company)
        )

        # SerpAPI conservation: only use paid credits for jobs with haiku_score >= 40
        # or jobs that have never been scored (haiku_score IS NULL).
        haiku_score = job_row.get("haiku_score")
        serpapi_worth_it = haiku_score is None or (isinstance(haiku_score, (int, float)) and haiku_score >= 40)

        if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing and serpapi_worth_it:
            # Initialize apply_urls BEFORE the call so the variable is always
            # defined even if TransientEnrichmentError is raised (which bypasses
            # the tuple assignment below, leaving apply_urls unbound otherwise).
            apply_urls: list[str] = []
            try:
                query = f"{title} {company}"
                # search_serpapi returns (result_dict, apply_option_urls).
                serpapi_result, apply_urls = search_serpapi(query, serpapi_key)

                if serpapi_result is not None:
                    # DEFECT 009 FIX: merge serpapi_result into fragments with existing
                    # data taking priority (fragments[k] wins if key already present),
                    # then call _resolve_from_fragments(fragments, ...) directly.
                    # DO NOT pass {**fragments, **serpapi_result} — that lets serpapi_result
                    # silently overwrite fragments entries (priority inversion).
                    for k, v in serpapi_result.items():
                        if k not in fragments:
                            fragments[k] = v

                    enriched = _resolve_from_fragments(fragments, missing, job_row)

                    if enriched:
                        _persist(conn, job_row, enriched, "serpapi")
                        # DEFECT 016 FIX: persist source_urls AFTER _persist() succeeds so
                        # both writes succeed or both fail together (best-effort atomicity).
                        # If _persist() raises, apply_urls are not committed, preventing
                        # a partial state where source_urls updated but tier not advanced.
                        # Fall through to the unconditional apply_urls write below when
                        # no enriched fields were found (serpapi_result present but resolved
                        # nothing new) — those ATS URLs are still worth saving for retries.
                        if apply_urls and conn is not None and job_row.get("dedup_key"):
                            _merge_apply_urls(conn, job_row["dedup_key"], apply_urls)
                        return enriched

                # DEFECT 010 FIX: persist apply_urls to source_urls UNCONDITIONALLY
                # whenever apply_urls is non-empty — even when serpapi_result is None
                # (e.g., apply_options existed but no job description was present).
                # ATS URLs from Google Jobs apply_options are valuable regardless of
                # whether the main result dict is populated; they give a direct path
                # to the full JD for future retry runs.
                # Persist via direct SQL UPDATE, bypassing _persist() because
                # source_urls is a JSON array column intentionally excluded from
                # _ENRICHABLE_COLUMNS.  Read-merge-write avoids overwriting existing URLs.
                if apply_urls and conn is not None and job_row.get("dedup_key"):
                    _merge_apply_urls(conn, job_row["dedup_key"], apply_urls)

            except TransientEnrichmentError as e:
                # Transient error (429/5xx/timeout): do NOT advance past serpapi.
                # Persist tier as "haiku" so next call retries serpapi.
                # apply_urls remains [] in this path (initialized before the call).
                logger.info(
                    "SerpAPI transient error for '%s' @ '%s': %s — will retry",
                    title, company, e,
                )
                enriched_so_far = _resolve_from_fragments(fragments, missing, job_row)
                _persist(conn, job_row, enriched_so_far if enriched_so_far else {}, "haiku")
                return enriched_so_far
            except Exception as e:
                logger.debug("SerpAPI tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 4: sonnet — Deep extraction from all accumulated fragments
        # ---------------------------------------------------------------
        # Sonnet only runs if JD is still missing (stubs don't count)
        jd_still_missing = (
            is_stub_jd(job_row.get("jd_full"), title, company)
            and is_stub_jd(fragments.get("url_jd"), title, company)
            and is_stub_jd(fragments.get("jd_full"), title, company)
        )

        if (
            start_idx <= TIER_ORDER.index("sonnet")
            and anthropic_client is not None
            and jd_still_missing
        ):
            try:
                # Check cost gate before calling Sonnet (requires a live DB connection)
                gate_ok = conn is not None and cost_gate(conn, config, "sonnet")
                if not gate_ok:
                    # Budget exceeded: persist as "serpapi" so Sonnet can be retried
                    # next month when budget resets. Do NOT mark as exhausted.
                    logger.debug(
                        "Sonnet cost gate blocked for '%s' @ '%s' — will retry later",
                        title, company,
                    )
                    enriched_so_far = _resolve_from_fragments(fragments, missing, job_row)
                    _persist(conn, job_row, enriched_so_far if enriched_so_far else {}, "serpapi")
                    return enriched_so_far

                sonnet_result = extract_with_sonnet(
                    fragments, job_row, anthropic_client, conn, config
                )
                if sonnet_result:
                    enriched = _filter_non_none(sonnet_result)
                    # Reject stub JDs from Sonnet extraction (title restatements)
                    if "jd_full" in enriched and is_stub_jd(enriched["jd_full"], title, company):
                        logger.debug(
                            "Sonnet returned stub jd_full for '%s' — discarding",
                            title,
                        )
                        enriched.pop("jd_full")
                    if enriched:
                        _persist(conn, job_row, enriched, "sonnet")
                        return enriched

            except Exception as e:
                logger.debug("Sonnet tier failed for '%s': %s", title, e)

        # All tiers exhausted
        _persist(conn, job_row, {}, "exhausted")
        return {}

    except Exception as e:
        logger.warning("enrich_job failed for '%s': %s", job_row.get("title"), e)
        return {}


def run_enrichment_backfill(
    db_path: str,
    serpapi_key: Optional[str] = None,
    anthropic_client: Any = None,
    config: Optional[dict] = None,
    limit: int = 100,
) -> dict:
    """Backfill unenriched jobs using the cost-ordered tier pipeline.

    First resets prematurely-exhausted jobs (exhausted but still missing jd_full),
    then processes jobs where enrichment is incomplete.

    Args:
        db_path: Absolute path to the SQLite database file.
        serpapi_key: Optional SerpAPI API key.
        anthropic_client: Optional Anthropic client for Haiku/Sonnet tiers.
        config: Optional application config dict.
        limit: Max number of jobs to process per call.

    Returns:
        Dict with 'enriched' count and 'reset' count.
    """
    from job_finder.web.db_helpers import standalone_connection

    if config is None:
        config = {}

    result = {"enriched": 0, "reset": 0, "processed": 0}

    with standalone_connection(db_path) as conn:
        # Phase 1: Reset prematurely-exhausted jobs (exhausted but short/missing jd_full).
        # These were marked exhausted when SerpAPI/Sonnet failed transiently.
        # CRITICAL: 'agentic_exhausted' rows are INTENTIONALLY excluded — they had
        # Playwright + Ollama attempts fail and must remain stranded. Re-queuing them
        # into the standard pipeline would waste API quota and overwrite valid state.
        reset_count = conn.execute(
            """UPDATE jobs SET enrichment_tier = NULL
               WHERE enrichment_tier = 'exhausted'
                 AND (jd_full IS NULL OR TRIM(jd_full) = '' OR LENGTH(TRIM(jd_full)) < 200)""",
        ).rowcount
        conn.commit()
        result["reset"] = reset_count
        if reset_count:
            logger.info("Enrichment backfill: reset %d prematurely-exhausted jobs", reset_count)

        # Phase 1b: Reset agentic_exhausted jobs older than 7 days (TTL recovery).
        # Most job postings expire within 2-4 weeks, so 30 days is too late.
        # 7 days balances retry opportunity against wasting cycles on dead listings.
        aged_reset = conn.execute(
            """UPDATE jobs SET enrichment_tier = 'exhausted'
               WHERE enrichment_tier = 'agentic_exhausted'
                 AND jd_full IS NULL
                 AND last_seen < datetime('now', '-7 days')""",
        ).rowcount
        conn.commit()
        if aged_reset:
            logger.info("Reset %d agentic_exhausted jobs (>7 days old) for retry", aged_reset)
            result["reset"] += aged_reset

        # Phase 2: Enrich jobs needing enrichment.
        # 'agentic' is excluded to prevent the 6-hourly backfill from re-enqueueing
        # agentic-enriched jobs through enrich_job() which would overwrite valid data.
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE enrichment_tier IS NULL
                  OR enrichment_tier NOT IN ('exhausted', 'agentic', 'agentic_exhausted', 'serpapi', 'sonnet')
               ORDER BY first_seen DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        for row in rows:
            job_row = dict(row)
            result["processed"] += 1
            enriched = enrich_job(
                job_row,
                serpapi_key=serpapi_key,
                anthropic_client=anthropic_client,
                conn=conn,
                config=config,
            )
            if enriched:
                result["enriched"] += 1

        logger.info(
            "Enrichment backfill: processed %d, enriched %d, reset %d",
            result["processed"], result["enriched"], result["reset"],
        )

    return result


# ---------------------------------------------------------------------------
# Private helpers: tier logic utilities
# ---------------------------------------------------------------------------


def _find_missing_fields(job_row: dict) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for Sonnet). Stubs (title
      restatements < 200 chars) are treated as missing.
    - salary_min: minimum salary

    Returns empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if is_stub_jd(job_row.get("jd_full"), job_row.get("title", ""), job_row.get("company", "")):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    return missing


def _filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def _start_tier_index(current_tier: Optional[str]) -> int:
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


def _parse_source_urls(source_urls_json: Optional[str]) -> list:
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


def _compose_fragment_text(fragments: dict, title: str, company: str) -> Optional[str]:
    """Compose a single text string from all accumulated fragments.

    Args:
        fragments: Dict of fragment texts collected from prior tiers.
        title: Job title for fallback context.
        company: Company name for fallback context.

    Returns:
        Aggregated text string for use in Haiku/Sonnet extraction,
        or None if no meaningful fragments exist (prevents AI tiers from
        hallucinating JDs from titles alone).
    """
    parts = []
    for key, text in fragments.items():
        if text and isinstance(text, str):
            parts.append(str(text)[:1000])

    if parts:
        return "\n\n".join(parts)
    return None


def _resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Rejects stub JDs (title restatements) via is_stub_jd check.

    Args:
        fragments: Dict of collected data from free-tier sources.
        missing: List of field names that are still missing.
        job_row: Original job row for reference.

    Returns:
        Dict of {field: value} for fields that fragments can satisfy.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")
    enriched = {}
    for field in missing:
        # Direct key match
        if field in fragments and fragments[field] is not None:
            # Reject stub jd_full values
            if field == "jd_full" and is_stub_jd(fragments[field], title, company):
                continue
            enriched[field] = fragments[field]
        # url_jd maps to jd_full
        elif field == "jd_full" and fragments.get("url_jd"):
            if is_stub_jd(fragments["url_jd"], title, company):
                continue
            enriched["jd_full"] = fragments["url_jd"]

    return _filter_non_none(enriched)


def _merge_apply_urls(conn: Any, dedup_key: str, apply_urls: list) -> None:
    """Read-merge-write source_urls JSON column with new ATS URLs from SerpAPI apply_options.

    Designed to be called AFTER _persist() succeeds so both writes succeed or
    both fail together (best-effort atomicity — SQLite has no multi-statement
    rollback across these two UPDATE calls, but failure of the second is non-critical).

    Args:
        conn: Open SQLite connection.
        dedup_key: Job dedup key to update.
        apply_urls: New URL strings to merge in (deduplicated against existing list).
    """
    if not conn or not dedup_key or not apply_urls:
        return
    try:
        existing_row = conn.execute(
            "SELECT source_urls FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        existing_json = existing_row["source_urls"] if existing_row else None
        try:
            existing_list = json.loads(existing_json) if existing_json else []
            if not isinstance(existing_list, list):
                existing_list = []
        except (json.JSONDecodeError, TypeError):
            existing_list = []
        merged = existing_list + [u for u in apply_urls if u not in existing_list]
        url_cursor = conn.execute(
            "UPDATE jobs SET source_urls = ? WHERE dedup_key = ?",
            (json.dumps(merged), dedup_key),
        )
        if url_cursor.rowcount == 0:
            logger.warning(
                "source_urls UPDATE matched 0 rows for dedup_key=%s "
                "(job deleted between enrich_job start and write?)",
                dedup_key,
            )
        conn.commit()
        logger.debug(
            "source_urls: merged %d new ATS URL(s) for dedup_key=%s",
            len(merged) - len(existing_list),
            dedup_key,
        )
    except Exception as exc:
        logger.debug("_merge_apply_urls failed for dedup_key=%s: %s", dedup_key, exc)


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
