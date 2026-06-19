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
  - Persists enrichment_tier with enriched fields; jd_full and salary routed
    through sanctioned helpers before the UPDATE so invariant violations in one
    field cannot discard the tier bookmark or sibling fields (I-02 / I-13).
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

from job_finder.config import JD_STORAGE_MAX_CHARS
from job_finder.db._direct_link import set_direct_url
from job_finder.db._jd_full import set_jd_full as _set_jd_full
from job_finder.db._jobs import _reconcile_salary_for_write
from job_finder.db._locations import apply_location_observation
from job_finder.enrichment_states import (
    EnrichmentTier,
    backfill_skip_sql,
    resume_index,
)
from job_finder.json_utils import utc_now_iso
from job_finder.web.direct_link import pick_direct_link
from job_finder.web.enrichment_sources import merge_apply_urls, parse_source_urls
from job_finder.web.enrichment_tiers import (
    fetch_ddg_jds,
    fetch_direct_jd,
    parse_structured_fields,
    query_ats_api,
    scrape_careers,
    search_ddg_web,
    search_duckduckgo,
    search_serpapi,
)
from job_finder.web.primary_source_merge import merge_primary_posting_fields

logger = logging.getLogger(__name__)


def _maybe_reconcile_ats_identity(
    conn: Any,
    job_row: dict,
    config: dict | None,
    *,
    reason: str,
) -> None:
    """After ``source_urls`` gains ATS links, reconcile company ATS identity.

    Logs at WARNING (not DEBUG) on exception because ``reconcile_company_ats``
    returns a status dict for operator-meaningful outcomes (``slug_collision``,
    ``verify_failed``, ``abstain_conflict``) rather than raising. Any exception
    that reaches this handler is therefore a programmer/infra error (DB lock,
    AttributeError on a malformed row, import failure) that an operator needs
    to see. The swallow is kept so a single reconcile failure does not fail
    the surrounding enrichment run.
    """

    if conn is None:
        return
    cid = job_row.get("company_id")
    if cid is None:
        return
    try:
        from job_finder.web.ats_identity_reconcile import reconcile_company_ats

        reconcile_company_ats(conn, int(cid), reason=reason, config=config)
    except Exception as exc:
        logger.warning(
            "ATS identity reconcile failed (company_id=%s dedup_key=%s reason=%s): %s",
            cid,
            job_row.get("dedup_key"),
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict cost ordering: free (URL -> ATS -> careers) -> DDG -> SerpAPI -> agentic.
# Backed by job_finder.enrichment_states (single source of truth, F1 fix). Kept as a
# list of raw strings for backward-compatible callers/tests that index by tier name.
TIER_ORDER = [
    EnrichmentTier.FREE.value,
    EnrichmentTier.DDG.value,
    EnrichmentTier.SERPAPI.value,
    EnrichmentTier.AGENTIC.value,
    EnrichmentTier.EXHAUSTED.value,
]

# Allowlist of jobs table columns that _persist() may write directly. Prevents
# AI-extracted dict keys from injecting arbitrary column names into dynamic SQL
# SET clauses. ``location`` is deliberately NOT here: an extracted location is
# routed through ``apply_location_observation`` (the D-5 single-writer funnel)
# rather than side-door-written to the ``location`` column — that side-door
# write with an empty ``locations_raw`` was the S4 wipe (next crawler
# re-sighting rebuilt ``location`` from ``locations_raw=[]`` and reverted it).
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max"})

# Per-field cost ceilings: highest tier allowed to search for this field.
# After this tier fails for a field, it is abandoned (not escalated further).
FIELD_TIER_CEILINGS = {
    "jd_full": "agentic",  # escalate all the way — critical for downstream scoring
    "salary_min": "ddg",  # cap at ddg — extracted post-fetch from jd_full
    "salary_max": "ddg",
}

# Minimum acceptable jd_full length when accepting a fetched JD from the
# agentic tier. Real fetched job postings are virtually always >= 200 chars;
# anything shorter is residual auth-wall noise that slipped past
# is_short_auth_page() (which uses < 2000 chars + signal-keyword detection).
# Apply ONLY to the agentic branch — earlier tiers have their own length
# guards (fetch_ddg_jds requires >= 200 chars, fetch_direct_jd is unbounded
# but already filters auth walls).
MIN_FETCH_JD_CHARS = 200

# Minimum character length for jd_full to be considered a real job description
# (not a stub title-restatement). Used by _is_stub_jd() to gate _find_missing_fields
# and _resolve_from_fragments so the pipeline escalates past title-only stubs.
_MIN_JD_LENGTH = 200

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

    Tiers: free (URL -> ATS -> careers) -> DDG -> SerpAPI -> agentic.
    Resumes from the next tier after job_row['enrichment_tier'] if set.
    Returns {} immediately for exhausted jobs.

    Persists enrichment_tier + enriched fields atomically to DB after each
    tier that produces data (if conn is provided). Returns the enriched dict.

    Args:
        job_row: Job record dict. Must have 'title' and 'company'.
        serpapi_key: Optional SerpAPI API key for SerpAPI tier.
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

        # Auto-promote long descriptions to jd_full (DQ-02) — routed through
        # set_jd_full() (Phase 46.03) for the content-density gate.
        if (
            not job_row.get("jd_full")
            and job_row.get("description")
            and len(job_row["description"]) > 200
        ):
            job_row["jd_full"] = job_row["description"]
            if conn is not None and job_row.get("dedup_key"):
                try:
                    _set_jd_full(
                        conn,
                        job_row["dedup_key"],
                        job_row["description"][:JD_STORAGE_MAX_CHARS],
                        source="data_enricher",
                    )
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
                ats_result: dict = {}
                careers_result: dict = {}

                # Sub-tier A: Direct URL fetch
                source_urls = parse_source_urls(job_row.get("source_urls"))
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

                # Capture the direct company-posting link from data the ATS
                # scan / careers scrape already fetched (zero new network).
                if conn is not None and job_row.get("dedup_key"):
                    direct = pick_direct_link(source_urls, ats_result, careers_result)
                    if direct:
                        set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1])

                    # Strict-matched primary posting: fold its authoritative
                    # fields (salary metadata, posted date, locations, the ATS
                    # URL itself) into the row via the canonical upsert merge.
                    primary_posting = ats_result.get("_primary_posting") or careers_result.get(
                        "_primary_posting"
                    )
                    if primary_posting:
                        merge_primary_posting_fields(conn, job_row, primary_posting)

                # Resolve what free tier found
                enriched = _resolve_from_fragments(fragments, missing, job_row)
                if enriched:
                    enriched = _apply_post_fetch_extraction(enriched, job_row, conn, config)
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
            enriched = _apply_post_fetch_extraction(enriched, job_row, conn, config)
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
                    _maybe_reconcile_ats_identity(
                        conn, job_row, config, reason="enrichment_ddg_apply_url"
                    )

                # Resolve + persist what DDG found (mirrors the free tier).
                # _resolve_from_fragments maps fragments["url_jd"] -> jd_full
                # and applies _is_stub_jd's 200-char gate; a stub yields
                # enriched == {} so escalation to SerpAPI/agentic proceeds.
                enriched = _resolve_from_fragments(fragments, missing, job_row)
                if enriched:
                    enriched = _apply_post_fetch_extraction(enriched, job_row, conn, config)
                    _persist(conn, job_row, enriched, "ddg")
                    return enriched

            except Exception as e:
                logger.debug("DDG tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 2: serpapi — Google Jobs search (paid)
        # ---------------------------------------------------------------
        # SerpAPI only runs if JD is still missing (salary ceiling is ddg)
        jd_still_missing = not (
            job_row.get("jd_full") or fragments.get("url_jd") or fragments.get("jd_full")
        )

        # Gate 1: sources.serpapi.enabled must be true (or absent — treat absent
        # as enabled for backward-compat with configs predating this key).
        _serpapi_cfg = (config or {}).get("sources", {}).get("serpapi", {})
        _serpapi_enabled = _serpapi_cfg.get("enabled", True)

        # Gate 2: optional daily call cap (config key sources.serpapi.daily_call_cap).
        # Absent or 0 means uncapped.  Checked against the scoring_costs ledger so
        # the cap survives Flask restarts (mirrors the google_cse_source pattern).
        _daily_cap: int = int(_serpapi_cfg.get("daily_call_cap", 0))
        _cap_reached = bool(_daily_cap > 0 and _serpapi_daily_calls_used(conn) >= _daily_cap)

        if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing:
            if not _serpapi_enabled:
                logger.debug("SerpAPI tier skipped for '%s': sources.serpapi.enabled=false", title)
            elif _cap_reached:
                logger.warning(
                    "SerpAPI tier skipped for '%s': daily_call_cap=%d reached", title, _daily_cap
                )
            else:
                try:
                    query = f"{title} {company}"
                    serpapi_result, apply_url_list = search_serpapi(query, serpapi_key)
                    _record_serpapi_call(conn)
                    if conn and job_row.get("dedup_key") and apply_url_list:
                        merge_apply_urls(conn, job_row["dedup_key"], apply_url_list)
                        _maybe_reconcile_ats_identity(
                            conn, job_row, config, reason="enrichment_serpapi_apply_urls"
                        )

                    if serpapi_result:
                        for k, v in serpapi_result.items():
                            if k not in fragments:
                                fragments[k] = v

                        enriched = _resolve_from_fragments(
                            {**fragments, **serpapi_result}, missing, job_row
                        )
                        if enriched:
                            enriched = _apply_post_fetch_extraction(
                                enriched, job_row, conn, config
                            )
                            _persist(conn, job_row, enriched, "serpapi")
                            return enriched

                except Exception as e:
                    logger.debug("SerpAPI tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 3: agentic — Ollama-driven query + Playwright fetch
        # ---------------------------------------------------------------
        # Agentic only runs if JD is still missing. Calls into the per-job
        # entry point in agentic_enricher (Playwright + Ollama; expensive).
        jd_still_missing = not (
            job_row.get("jd_full") or fragments.get("url_jd") or fragments.get("jd_full")
        )

        if start_idx <= TIER_ORDER.index("agentic") and jd_still_missing:
            try:
                from job_finder.web.agentic_enricher import enrich_one_job

                agentic_result = enrich_one_job(job_row, conn, config)
                jd = agentic_result.get("jd_full")
                if jd and len(jd) >= MIN_FETCH_JD_CHARS:
                    enriched = {"jd_full": jd}
                    enriched = _apply_post_fetch_extraction(enriched, job_row, conn, config)
                    _persist(conn, job_row, enriched, "agentic")
                    return enriched
            except Exception as e:
                logger.debug("Agentic tier failed for '%s': %s", title, e)

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
    limit: int | None = 100,
) -> int:
    """Backfill unenriched jobs using the cost-ordered tier pipeline.

    Queries jobs where enrichment_tier IS NULL or in a resumable state.
    Skips known terminal tiers: 'exhausted', 'serpapi', 'agentic', 'mid'
    (standard pipeline terminals), 'agentic_exhausted' (written by agentic_enricher
    after exhausting the agentic tier), and legacy migration tiers 'low'/'high'
    (left by m050). Unknown/future stray tier values are treated as terminal by
    _start_tier_index (fail-closed), but are not excluded at the SQL level.
    Processes up to `limit` jobs per call (omit ``limit`` / pass ``None`` to
    process the full backlog in one run — no SQL ``LIMIT``).

    Args:
        db_path: Absolute path to the SQLite database file.
        serpapi_key: Optional SerpAPI API key.
        config: Optional application config dict.
        limit: Max number of jobs to process per call, or ``None`` for no cap.

    Returns:
        Number of jobs that were enriched (had fields added).
    """
    from job_finder.web.db_helpers import standalone_connection

    if config is None:
        config = {}

    with standalone_connection(db_path) as conn:
        # Only select rows that (a) are in a resumable enrichment tier AND
        # (b) are actually missing at least one field _find_missing_fields()
        # checks. Without the (b) clause, a capped LIMIT silently returns the first
        # N rows in rowid order — which are the oldest, already-enriched
        # rows — and enrich_job() returns {} for each, leaving the real
        # backlog unreached. ORDER BY first_seen DESC further prioritises the
        # freshest rows, which are the ones users are actively viewing.
        # Terminal tiers: 'serpapi' (got JD), 'agentic' (got JD via Playwright),
        # 'exhausted' (all tiers tried and failed). 'mid' is terminal for the
        # enrichment pipeline (fully enriched at balanced tier).
        # location IS NULL / '' is added here so empty-location rows enter the
        # regular tier cascade AND are eligible for extraction-only (D-5, #388).
        # The extraction-only pass (run_location_extraction_backfill) handles
        # terminal-tier rows separately; the clause here ensures non-terminal
        # rows with a good jd_full already don't get skipped by _find_missing_fields
        # returning a non-empty list.
        base_sql = f"""SELECT * FROM jobs
               WHERE (enrichment_tier IS NULL
                      OR {backfill_skip_sql()})
                 AND (jd_full IS NULL OR jd_full = ''
                      OR salary_min IS NULL
                      OR location IS NULL OR location = '')
               ORDER BY first_seen DESC"""
        if limit is None:
            rows = conn.execute(base_sql).fetchall()
        else:
            rows = conn.execute(base_sql + "\n               LIMIT ?", (limit,)).fetchall()

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

        # Per-run SerpAPI call summary (helps operators track paid spend).
        serpapi_calls_today = _serpapi_daily_calls_used(conn)
        _serpapi_cfg = config.get("sources", {}).get("serpapi", {})
        _daily_cap: int = int(_serpapi_cfg.get("daily_call_cap", 0))
        cap_info = f"/{_daily_cap}" if _daily_cap > 0 else " (uncapped)"
        logger.info(
            "Enrichment backfill complete: %d enriched, SerpAPI calls today: %d%s",
            enriched_count,
            serpapi_calls_today,
            cap_info,
        )

        return enriched_count


# Minimum jd_full length for the extraction-only pass. Mirrors MIN_FETCH_JD_CHARS
# — anything shorter is a stub that extraction won't improve.
_EXTRACTION_ONLY_MIN_JD_CHARS = 200


def run_location_extraction_backfill(
    db_path: str,
    config: dict | None = None,
    limit: int = 50,
) -> int:
    """Drain the empty-location backlog via a cheap, no-fetch extraction pass.

    Targets rows WHERE location IS NULL OR location = '' AND jd_full IS NOT NULL
    AND length(jd_full) >= _EXTRACTION_ONLY_MIN_JD_CHARS, regardless of
    enrichment_tier (including terminal tiers like 'exhausted', 'agentic',
    etc.). This lets careers-crawl rows that exhausted the fetch cascade but
    already have a good jd_full be drained without new network calls.

    Per run: calls _apply_post_fetch_extraction (LLM structured extraction)
    and routes any returned location through apply_location_observation (D-5,
    single-writer funnel). No tier update is written — this pass does not
    advance enrichment_tier.

    On first extraction miss (LLM returned no location despite a good jd_full),
    appends 'location_missing' to unresolved_reasons (YAGNI option: tag after
    first miss so /admin/review surfaces it; admin can clear to retry). The tag
    prevents the row from being re-attempted on every run indefinitely.

    Design: D-5 (single writer), D-3 (salvage before discard, flag before
    salvage-guess), D-9 (quarantine via unresolved_reasons). #388.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Optional application config dict.
        limit: Max rows to process per call (default 50; 3×/day ≈ 2-3 days to
               drain the ~325-row backlog organically).

    Returns:
        Number of rows where a location was successfully extracted.
    """
    from job_finder.web.db_helpers import standalone_connection

    if config is None:
        config = {}

    resolved_count = 0

    with standalone_connection(db_path) as conn:
        # Select empty-location rows that have a substantive jd_full, regardless
        # of tier. Exclude rows already tagged 'location_missing' in
        # unresolved_reasons — those are "tried and failed" (YAGNI stop-retry).
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE (location IS NULL OR location = '')
                 AND jd_full IS NOT NULL
                 AND length(jd_full) >= ?
                 AND (unresolved_reasons IS NULL
                      OR json_extract(unresolved_reasons, '$') IS NULL
                      OR NOT EXISTS (
                          SELECT 1 FROM json_each(unresolved_reasons)
                          WHERE value = 'location_missing'
                      ))
               ORDER BY first_seen DESC
               LIMIT ?""",
            (_EXTRACTION_ONLY_MIN_JD_CHARS, limit),
        ).fetchall()

        logger.info(
            "Location extraction backfill: %d candidate row(s) selected (limit=%d)",
            len(rows),
            limit,
        )

        for row in rows:
            job_row = dict(row)
            dedup_key = job_row.get("dedup_key")
            if not dedup_key:
                continue

            try:
                # Extraction-only: run post-fetch extraction against the existing
                # jd_full. No HTTP fetch, no tier cascade. _apply_post_fetch_extraction
                # will skip if jd_full is too short (< MIN_FETCH_JD_CHARS), so the
                # length guard in SQL above provides early termination only.
                extraction_result = _apply_post_fetch_extraction(
                    enriched={},  # nothing freshly fetched — work from row's jd_full
                    job_row=job_row,
                    conn=conn,
                    config=config,
                )

                location_obs = extraction_result.get("location")
                if location_obs and str(location_obs).strip():
                    # Route through the D-5 single-writer funnel (never write
                    # location column directly — that was the S4 wipe bug).
                    changed = apply_location_observation(
                        conn, dedup_key, str(location_obs), source="location_extract"
                    )
                    if changed:
                        resolved_count += 1
                        logger.debug(
                            "Location extraction backfill: resolved %r -> %r [key=%s]",
                            location_obs,
                            str(location_obs),
                            dedup_key,
                        )
                        continue

                # Extraction yielded nothing (or apply was a no-op after dedup).
                # Tag as location_missing so this row is skipped on future runs
                # (D-9: quarantine via unresolved_reasons, YAGNI stop-retry).
                try:
                    existing_reasons_raw = job_row.get("unresolved_reasons") or "[]"
                    try:
                        reasons = json.loads(existing_reasons_raw)
                    except (json.JSONDecodeError, TypeError):
                        reasons = []
                    if not isinstance(reasons, list):
                        reasons = list(reasons) if reasons else []
                    if "location_missing" not in reasons:
                        reasons.append("location_missing")
                        conn.execute(
                            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
                            (json.dumps(reasons), dedup_key),
                        )
                        conn.commit()
                        logger.debug(
                            "Location extraction backfill: tagged location_missing [key=%s]",
                            dedup_key,
                        )
                except Exception as tag_exc:
                    logger.warning(
                        "Location extraction backfill: could not tag location_missing "
                        "[key=%s]: %s",
                        dedup_key,
                        tag_exc,
                    )

            except Exception as exc:
                logger.warning(
                    "Location extraction backfill: error processing row [key=%s]: %s",
                    dedup_key,
                    exc,
                )

    logger.info(
        "Location extraction backfill complete: %d/%d resolved",
        resolved_count,
        len(rows),
    )
    return resolved_count


# ---------------------------------------------------------------------------
# Private helpers: tier logic utilities
# ---------------------------------------------------------------------------


def _is_stub_jd(jd_text: str | None, title: str = "", company: str = "") -> bool:
    """Return True if jd_text is a stub (falsy or title-restatement < _MIN_JD_LENGTH chars).

    Stubs are treated as missing jd_full so the pipeline escalates to richer tiers
    that may provide a real job description, rather than persisting noise.

    Args:
        jd_text: The jd_full text to check.
        title:   Job title (carried for API symmetry; unused in current check).
        company: Company name (carried for API symmetry; unused in current check).
    """
    if not jd_text:
        return True
    return len(jd_text.strip()) < _MIN_JD_LENGTH


def _find_missing_fields(job_row: dict) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for AI scoring). Stubs (title
      restatements shorter than _MIN_JD_LENGTH chars) are treated as missing.
    - salary_min: minimum salary
    - location: canonical location string (D-5; empty string counts as missing)

    Returns empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if _is_stub_jd(
        job_row.get("jd_full"),
        job_row.get("title", ""),
        job_row.get("company", ""),
    ):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    if not job_row.get("location"):
        # Empty string or NULL — location joins the enrichment contract (D-5, #388)
        missing.append("location")
    return missing


def _serpapi_daily_calls_used(conn: Any) -> int:
    """Return today's SerpAPI enrichment call count from the scoring_costs ledger.

    Uses the same calendar-day window as google_cse_source: UTC timestamps
    stored by utc_now_iso(), compared via local_day_utc_window() so the reset
    aligns with the user's clock.  Falls back to 0 on any DB error so a
    quota-read failure never blocks enrichment outright.

    Args:
        conn: SQLite connection (may be None — returns 0 immediately).
    """
    if conn is None:
        return 0
    try:
        from job_finder.json_utils import local_day_utc_window

        start, end = local_day_utc_window()
        row = conn.execute(
            "SELECT COUNT(*) FROM scoring_costs "
            "WHERE provider=? AND timestamp >= ? AND timestamp < ?",
            ("serpapi_enrichment", start, end),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:
        logger.warning(
            "SerpAPI daily quota read failed (%s); quota gate skipped",
            type(exc).__name__,
        )
        return 0


def _record_serpapi_call(conn: Any) -> None:
    """Append a quota-ledger row to scoring_costs for one SerpAPI enrichment call.

    Uses provider='serpapi_enrichment' and cost_usd=0 (cost is real but
    untracked per-call — this row exists only as a daily quota counter,
    mirroring the google_cse_source pattern).  Silent no-op when conn is
    None or the INSERT fails (best-effort; never raises).

    Args:
        conn: SQLite connection (may be None — skips silently).
    """
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO scoring_costs "
            "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (NULL, ?, ?, 0, 0, 0, ?, ?)",
            ("serpapi_enrichment", "serpapi_enrichment", utc_now_iso(), "serpapi_enrichment"),
        )
        conn.commit()
    except Exception as exc:
        logger.warning(
            "SerpAPI quota ledger write failed (%s); call not counted against cap",
            type(exc).__name__,
        )


def _filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def _start_tier_index(current_tier: str | None) -> int:
    """Return the index in TIER_ORDER to start from based on current_tier.

    Thin wrapper over ``job_finder.enrichment_states.resume_index`` (the single
    source of truth, F1 fix). If current_tier is None, start from 0; a known tier
    resumes from the NEXT tier; an unknown tier is treated as terminal (fail-closed)
    and logs a warning. Retained as a module-level name for backward-compatible
    callers/tests that import ``_start_tier_index``.

    Args:
        current_tier: The enrichment_tier value from the job row.

    Returns:
        Index into TIER_ORDER to start enrichment from.
    """
    return resume_index(current_tier)


def _resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Rejects stub jd_full values (title restatements < _MIN_JD_LENGTH chars) via
    _is_stub_jd() — same gate as _find_missing_fields() — so stubs from cheaper
    tiers don't block escalation to richer tiers that may have the real JD.

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
            # Reject stub jd_full values — treat them as not found so the
            # pipeline can escalate to a tier with a real description.
            if field == "jd_full" and _is_stub_jd(fragments[field], title, company):
                continue
            enriched[field] = fragments[field]
        # url_jd maps to jd_full
        elif field == "jd_full" and fragments.get("url_jd"):
            if _is_stub_jd(fragments["url_jd"], title, company):
                continue
            enriched["jd_full"] = fragments["url_jd"]

    return _filter_non_none(enriched)


def _apply_post_fetch_extraction(
    enriched: dict,
    job_row: dict,
    conn: Any,
    config: dict,
) -> dict:
    """Augment ``enriched`` with structured fields parsed from the fetched JD.

    Runs ``parse_structured_fields`` exactly once per successful cascade tier
    when (a) a jd_full is now available (from this tier or already on the row)
    and (b) at least one of salary_min/salary_max/location is still empty in
    BOTH ``enriched`` and ``job_row``. Returned values fill ONLY empty fields
    — never overwrite existing values from the row or from this tier.

    Returns a NEW dict (immutability — does not mutate the input ``enriched``).

    Replaces the salary-extraction side-effect of the deleted Haiku/Sonnet
    synthesis tiers (Phase 2b sub-fix RC4). See parse_structured_fields()
    docstring for the no-summarize guarantee.
    """
    # Effective jd_full: prefer the freshly-enriched value, fall back to the row
    effective_jd = enriched.get("jd_full") or job_row.get("jd_full")
    if not effective_jd or len(effective_jd) < MIN_FETCH_JD_CHARS:
        return dict(enriched)

    # An "empty" structured field is missing from BOTH enriched and job_row
    structured_fields = ("salary_min", "salary_max", "location")

    def _is_empty(field: str) -> bool:
        return enriched.get(field) is None and not job_row.get(field)

    if not any(_is_empty(f) for f in structured_fields):
        return dict(enriched)

    merged = dict(enriched)

    # Fast path: deterministic regex salary extraction. Runs first so
    # the common-format JDs ($120K-$150K, "salary range: 120K-150K",
    # USD 120,000-150,000, etc.) don't burn an LLM call. Only fills
    # salary_{min,max} both-or-neither — the regex helper guarantees
    # both-present-or-both-None semantics.
    from job_finder.web.salary_extractor import extract_salary_from_text

    if _is_empty("salary_min") and _is_empty("salary_max"):
        regex_min, regex_max = extract_salary_from_text(effective_jd)
        if regex_min is not None and regex_max is not None:
            merged["salary_min"] = regex_min
            merged["salary_max"] = regex_max

    # Recompute is-empty after regex pass — the LLM only needs to run
    # if there's something it can still help with (location, or salary
    # the regex couldn't find).
    def _still_empty(field: str) -> bool:
        return merged.get(field) is None and not job_row.get(field)

    if not any(_still_empty(f) for f in structured_fields):
        return merged

    parsed = parse_structured_fields(
        jd_full=effective_jd,
        job_row=job_row,
        conn=conn,
        config=config,
    )
    if not parsed:
        return merged

    for field, value in parsed.items():
        if field not in structured_fields:
            continue  # ignore unknown keys; schema already restricts
        if _still_empty(field):  # only fill empty fields — never overwrite
            merged[field] = value
    return merged


def _persist(conn: Any, job_row: dict, enriched: dict, tier_name: str) -> None:
    """Persist enriched fields + enrichment_tier, routing each field through its
    sanctioned write path so m078 invariant violations cannot silently discard
    the full enrichment.

    Write order:
    1. ``jd_full`` — routed through ``_set_jd_full()`` (I-13 junk gate) as a
       separate commit.  A junk JD is logged and skipped; ``_set_jd_full`` never
       raises, so a bad JD cannot abort the remaining writes.
    2. ``salary_min`` / ``salary_max`` — reconciled via
       ``_reconcile_salary_for_write()`` before the UPDATE (I-02 inversion fix).
       A single new value that would invert against the existing stored
       counterpart is dropped (keeping existing) so the I-02 trigger cannot abort
       the persist; a both-field same-unit inversion is swapped; an extreme
       mismatch drops the incoming pair. Drops log a WARNING.
    3. Remaining fields (``location``, normalised salary) + ``enrichment_tier``
       in one UPDATE.  If that UPDATE fails unexpectedly, a fallback tier-only
       UPDATE ensures the tier bookmark is always recorded so the job is not
       re-fetched indefinitely.

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

    # --- Step 0: location — route through the D-5 single-writer funnel ---
    # An extracted location is an *observation*, not a direct column write. The
    # funnel merges it into locations_raw + rewrites all five canonical location
    # columns atomically, so a later crawler re-sighting (empty incoming
    # location) cannot wipe it (the S4 bug). Pop it before the allowlist filter
    # so it is neither dropped-as-unknown nor written directly.
    if enriched:
        location_obs = enriched.get("location")
        if location_obs and str(location_obs).strip():
            apply_location_observation(conn, dedup_key, str(location_obs), source="llm_extract")

    if enriched:
        # Filter to allowlisted columns only — prevents AI-extracted keys from
        # injecting arbitrary column names into the dynamic SQL SET clause.
        # ``location`` is intentionally excluded (handled by the funnel above);
        # drop it silently rather than logging it as an unknown column.
        safe_enriched = {k: v for k, v in enriched.items() if k in _ENRICHABLE_COLUMNS}
        unknown = set(enriched) - _ENRICHABLE_COLUMNS - {"location"}
        if unknown:
            logger.warning("_persist: dropping non-allowlisted columns: %s", unknown)
    else:
        safe_enriched = {}

    # --- Step 1: jd_full — routed through set_jd_full() (I-13 junk gate) ---
    # Extracted from the multi-column UPDATE so a junk JD trigger (I-13) cannot
    # abort and discard the enrichment_tier bookmark and all sibling fields.
    # _set_jd_full() handles its own commit; it never raises.
    jd_full_value = safe_enriched.pop("jd_full", None)
    if jd_full_value is not None:
        try:
            _set_jd_full(conn, dedup_key, jd_full_value, source="data_enricher._persist")
        except Exception as e:
            logger.warning("_persist: jd_full write failed for '%s': %s", dedup_key, e)

    # --- Step 2: salary — reconcile before writing (I-02 inversion fix) ---
    # _reconcile_salary_for_write() validates the EFFECTIVE pair the I-02 trigger
    # will see: a single-field update leaves the unset column at its stored
    # value, so a new value that inverts against the existing counterpart trips
    # tg_jobs_salary_range and aborts the whole persist. The helper drops such an
    # incoming value (keeping existing) rather than letting the trigger fire.
    sal_min = safe_enriched.pop("salary_min", None)
    sal_max = safe_enriched.pop("salary_max", None)
    if sal_min is not None or sal_max is not None:
        salary_cols, dropped = _reconcile_salary_for_write(
            sal_min, sal_max, job_row.get("salary_min"), job_row.get("salary_max")
        )
        if dropped:
            logger.warning(
                "_persist: salary dropped for '%s' (would invert the stored range; "
                "incoming min=%s max=%s, existing min=%s max=%s)",
                dedup_key,
                sal_min,
                sal_max,
                job_row.get("salary_min"),
                job_row.get("salary_max"),
            )
        safe_enriched.update(salary_cols)

    # --- Step 3: remaining fields + enrichment_tier ---
    # enrichment_tier is always written — even when every enriched field was
    # junk-gated or dropped — so the job is not re-fetched on the next backfill.
    # The fallback tier-only UPDATE in the except handler is the last resort for
    # any unexpected violation that slips past the Python-layer guards above.
    try:
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
        # Fallback: at minimum record the tier so this job is not re-fetched
        # with the same data on every subsequent backfill.
        try:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (tier_name, dedup_key),
            )
            conn.commit()
        except Exception as tier_e:
            logger.warning("_persist: tier fallback also failed for '%s': %s", dedup_key, tier_e)
