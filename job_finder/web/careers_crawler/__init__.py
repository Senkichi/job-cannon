"""Active careers page crawler — re-discovers and originates job discovery.

Provides crawl_careers_batch() — a daily scheduled job that selects companies
in two lanes (#220):
1a. RE-DISCOVERY (uncapped): companies that have ever had a high-scoring job
    (classification IN ('apply','consider')), due for a re-crawl.
1b. ORIGINATION (capped at careers_crawl.origination_batch_limit, default 25):
    never-crawled companies that have a careers_url but no apply/consider
    history yet — typically NULL-ATS companies the crawler has never touched.
    This lets the crawler *originate* discovery rather than only re-discover
    proven-relevant companies.
2. Multi-tier extraction: cached API → static HTML → URL param search →
   Playwright with interaction (load-more, scroll, pagination, search)
3. Feeds matched jobs into the existing upsert/score pipeline

Architecture:
- Thread-safe: creates own sqlite3 connections (standalone_connection pattern)
- TESTING guard: returns early when config.get('TESTING') is True
- Browser launched per invocation, not kept alive between runs
- Zero API cost — all extraction is mechanical (JSON-LD, link matching,
  form interaction, API interception)
"""

import concurrent.futures
import logging
import time
from typing import Any

import requests  # noqa: F401  — bound here so test_careers_crawler patches resolve

from job_finder.json_utils import utc_now_iso

# Title hygiene + URL-path navigation filters — extracted to _title_filters.
# Re-imported here so the public surface (job_finder.web.careers_crawler.X)
# is preserved for tests/test_careers_crawler.py and for any downstream
# code that imports these names.
from job_finder.web.careers_crawler._title_filters import (
    _CITY_SUFFIX_RE,
    _LOCATION_SUFFIX_RE,
    _NAV_PATH_PREFIXES,
    _clean_title,
)
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRESHNESS_DAYS = 1  # Re-crawl daily to catch new postings early
_POLITE_DELAY = 1.0  # Seconds between companies
# Origination lane: never-crawled companies with a careers_url but no
# apply/consider history yet (often NULL-ATS). Capped per run so the
# crawler can *originate* discovery without flooding (#220). At 25/run a
# ~1,000-company backlog drains in ~6 weeks.
_ORIGINATION_BATCH_LIMIT = 25

# Static extraction — extracted to _static_tier. Re-exported here so
# both internal callers (_try_cached_tier, _crawl_companies) and the
# public surface (test patches on _try_static_extract; lazy imports of
# _extract_jobs_from_soup from careers_page_interactions and
# ai_career_navigator) keep resolving from
# job_finder.web.careers_crawler.X.
# ---------------------------------------------------------------------------
# API cache helpers — extracted to _api_cache.py
# ---------------------------------------------------------------------------
from job_finder.web.careers_crawler._api_cache import (
    _cache_api_endpoint,
    _clear_api_cache,
    _try_cached_api,
)

# ---------------------------------------------------------------------------
# Embedded JSON tier (Tier 2.5) — sits between URL-param and Playwright.
# ---------------------------------------------------------------------------
from job_finder.web.careers_crawler._embedded_json_tier import (
    _try_embedded_json_extract,
    _try_embedded_json_extract_from_html,
)

# ---------------------------------------------------------------------------
# Playwright tiers (passive + active) — extracted to _playwright_tier.
# Re-exported so test patches like
# @patch('job_finder.web.careers_crawler._try_playwright_active') keep
# resolving and so internal callers (_try_cached_tier, _crawl_companies)
# can continue dispatching through the package namespace.
# ---------------------------------------------------------------------------
from job_finder.web.careers_crawler._playwright_tier import (
    _INTERACTION_DELAY_S,
    _JS_SETTLE_MS,
    _PLAYWRIGHT_TIMEOUT_MS,
    _try_playwright_active,
    _try_playwright_extract,
)

# ---------------------------------------------------------------------------
# Sitemap / RSS tier (Stage 5) — sits between API-cache and static tiers
# in the escalation chain. Re-exported here so test patches that target
# `job_finder.web.careers_crawler._try_sitemap_extract` resolve.
# ---------------------------------------------------------------------------
from job_finder.web.careers_crawler._sitemap_tier import _try_sitemap_extract

# ---------------------------------------------------------------------------
# Static extraction — extracted to _static_tier. Re-exported here so
# both internal callers (_try_cached_tier, _crawl_companies) and the
# public surface (test patches on _try_static_extract; lazy imports of
# _extract_jobs_from_soup from careers_page_interactions and
# ai_career_navigator) keep resolving from
# job_finder.web.careers_crawler.X.
# ---------------------------------------------------------------------------
from job_finder.web.careers_crawler._static_tier import (
    _STATIC_MIN_TEXT_LEN,
    _STATIC_TEXT_RATIO,
    _extract_jobs_from_soup,
    _extract_jsonld_postings,
    _try_static_extract,
)

# Explicit re-export surface for the careers_crawler package.
#
# Every symbol imported above into this `__init__.py` is intentionally
# re-exposed at the package namespace (e.g. so test files can patch
# `job_finder.web.careers_crawler._try_playwright_active`). Listing them
# in `__all__` tells ruff that these "unused" imports are intentional
# re-exports — the documented alternative to per-line `noqa: F401`
# annotations on every multi-line import block.
# Grouped by source module:
#   _title_filters:    _CITY_SUFFIX_RE, _LOCATION_SUFFIX_RE, _NAV_PATH_PREFIXES, _clean_title
#   _api_cache:        _cache_api_endpoint, _clear_api_cache, _try_cached_api
#   _playwright_tier:  _INTERACTION_DELAY_S, _JS_SETTLE_MS, _PLAYWRIGHT_TIMEOUT_MS,
#                      _try_playwright_active, _try_playwright_extract
#   _sitemap_tier:     _try_sitemap_extract                       (Stage 5)
#   _static_tier:      _STATIC_MIN_TEXT_LEN, _STATIC_TEXT_RATIO,
#                      _extract_jobs_from_soup, _extract_jsonld_postings, _try_static_extract
#   _embedded_json_tier: _try_embedded_json_extract, _try_embedded_json_extract_from_html (Tier 2.5)
#   _ai_nav_tier:      _try_ai_navigation
#   _tier_cache:       _try_cached_tier
#   _persistence:      _upsert_and_log, _update_timestamp_on_error
#   _scoring:          _score_new_jobs
__all__ = [
    "_CITY_SUFFIX_RE",
    "_INTERACTION_DELAY_S",
    "_JS_SETTLE_MS",
    "_LOCATION_SUFFIX_RE",
    "_NAV_PATH_PREFIXES",
    "_PLAYWRIGHT_TIMEOUT_MS",
    "_STATIC_MIN_TEXT_LEN",
    "_STATIC_TEXT_RATIO",
    "_cache_api_endpoint",
    "_clean_title",
    "_clear_api_cache",
    "_extract_jobs_from_soup",
    "_extract_jsonld_postings",
    "_score_new_jobs",
    "_try_ai_navigation",
    "_try_cached_api",
    "_try_cached_tier",
    "_try_embedded_json_extract",
    "_try_embedded_json_extract_from_html",
    "_try_playwright_active",
    "_try_playwright_extract",
    "_try_sitemap_extract",
    "_try_static_extract",
    "_update_timestamp_on_error",
    "_upsert_and_log",
    "crawl_careers_batch",
    "sync_playwright",
]


def __getattr__(name: str):
    """Lazy-load playwright symbols on first access (PEP 562).

    ``playwright`` is an optional heavy dependency (multi-hundred-MB browser
    download). It must not be imported at module load time so that
    ``job-cannon --help`` and ``job-cannon --version`` work in environments
    where playwright is not installed (e.g. a clean ``pipx install`` before
    the user has run ``playwright install``).

    The name is stored in ``globals()`` after the first import so subsequent
    accesses and ``unittest.mock.patch`` restore both work without re-triggering
    this hook.
    """
    if name == "sync_playwright":
        try:
            from playwright.sync_api import sync_playwright as _sp
        except ImportError as exc:
            raise ImportError(
                "Playwright is not installed. "
                "Install it with: pipx inject job-cannon playwright && playwright install chromium"
            ) from exc
        globals()["sync_playwright"] = _sp
        return _sp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def crawl_careers_batch(db_path: str, config: dict) -> dict:
    """Crawl careers pages for companies with multi-tier active extraction.

    Thread-safe: creates own sqlite3 connections (standalone_connection pattern).
    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Load batch of miss companies with careers_url, ordered by staleness
    2. For each company: try static extraction, fall back to Playwright
    3. For each matched job: create Job object and upsert
    4. Score new jobs via scoring_orchestrator (single-tier v3.0 ordinal rubric)
    5. Log activity and update company timestamps

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict.

    Returns:
        Summary dict with companies_crawled, jobs_found, jobs_new,
        scored, classified_apply, classified_consider, classified_skip,
        classified_reject, playwright_rendered, errors.
    """
    if config.get("TESTING"):
        logger.debug("crawl_careers_batch: TESTING mode — skipping")
        return _new_summary()

    profile_cfg = config.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    summary: dict[str, Any] = _new_summary()
    all_new_job_keys: list[str] = []

    # Two-lane company selection (#220):
    #   Lane 1 — RE-DISCOVERY (unchanged, uncapped): companies that have ever
    #     had a high-scoring job (classification IN ('apply','consider')).
    #   Lane 2 — ORIGINATION (new, capped): never-crawled companies that have a
    #     careers_url but NO apply/consider history yet — typically NULL-ATS
    #     companies the crawler has never touched. This lets the crawler
    #     *originate* discovery (detect an ATS / find a careers page) instead of
    #     only re-discovering known-good companies.
    # Both lanes share the same hard gates (careers_url present, scan_enabled,
    # not an ATS 'hit', and not in the 5-strike penalty box). Lane 2 is bounded
    # by `careers_crawl.origination_batch_limit` (default 25/run) so origination
    # cannot flood the run; the title-filter gate bounds blast radius downstream.
    with standalone_connection(db_path) as conn:
        freshness_days = config.get("careers_crawl", {}).get("freshness_days", _FRESHNESS_DAYS)
        origination_limit = config.get("careers_crawl", {}).get(
            "origination_batch_limit", _ORIGINATION_BATCH_LIMIT
        )

        # Lane 1: re-discovery — proven-relevant companies due for a re-crawl.
        rediscovery = conn.execute(
            """SELECT c.id, c.name_raw, c.careers_url, c.careers_api_endpoint,
                      c.careers_crawl_tier, c.careers_nav_recipe
               FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND (c.careers_crawl_last_at IS NULL
                      OR c.careers_crawl_last_at < datetime('now', ? || ' days'))
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT COUNT(*) AS total,
                                SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits
                         FROM company_scan_log WHERE company_id = c.id
                     ) s WHERE s.total >= 5 AND s.hits = 0
                 )
               ORDER BY c.careers_crawl_last_at ASC NULLS FIRST""",
            (f"-{freshness_days}",),
        ).fetchall()

        # Lane 2: origination — never-crawled companies with a careers_url and
        # no apply/consider history. Capped and ordered by id for determinism.
        origination = conn.execute(
            """SELECT c.id, c.name_raw, c.careers_url, c.careers_api_endpoint,
                      c.careers_crawl_tier, c.careers_nav_recipe
               FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND c.careers_crawl_last_at IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT COUNT(*) AS total,
                                SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits
                         FROM company_scan_log WHERE company_id = c.id
                     ) s WHERE s.total >= 5 AND s.hits = 0
                 )
               ORDER BY c.id ASC
               LIMIT ?""",
            (origination_limit,),
        ).fetchall()

    # Re-discovery runs first (proven relevance), then the capped origination
    # cohort. De-dup defensively in case a company qualifies for both lanes
    # (it cannot today — lane 2 requires no apply/consider history — but the
    # guard keeps the contract robust against future lane-predicate drift).
    seen_ids: set[int] = set()
    companies = []
    for row in (*rediscovery, *origination):
        if row[0] in seen_ids:
            continue
        seen_ids.add(row[0])
        companies.append(row)

    if not companies:
        logger.info("careers_crawler: no companies due for crawling")
        return summary

    logger.info(
        "careers_crawler: %d companies in batch (%d re-discovery, %d origination)",
        len(companies),
        len(rediscovery),
        len(origination),
    )

    merged_summary, merged_keys = _crawl_companies(
        companies,
        db_path,
        config,
        target_titles,
        title_exclusions,
    )
    # Merge worker results into top-level summary
    for key in merged_summary:
        if key == "errors":
            summary["errors"].extend(merged_summary["errors"])
        else:
            summary[key] += merged_summary.get(key, 0)
    all_new_job_keys.extend(merged_keys)

    # --- Score newly discovered jobs (v3.0 unified scorer) ---
    if all_new_job_keys:
        _score_new_jobs(db_path, config, all_new_job_keys, summary)

    # --- Activity feed entry ---
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                """INSERT INTO runs
                   (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    utc_now_iso(),
                    "careers_crawl",
                    summary["jobs_found"],
                    summary["jobs_new"],
                    summary["scored"],
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to insert careers_crawl activity entry: %s", e)

    logger.info(
        "careers_crawler complete: %d crawled, %d found, %d new, "
        "%d playwright, %d interactive, %d api-cached, %d sitemap, "
        "%d url-param, %d embedded-json, %d ai-navigated, %d ai-replayed, %d ats-link-promoted, "
        "%d scored (apply=%d, consider=%d, skip=%d, reject=%d)",
        summary["companies_crawled"],
        summary["jobs_found"],
        summary["jobs_new"],
        summary["playwright_rendered"],
        summary.get("interactive", 0),
        summary.get("api_cached", 0),
        summary.get("sitemap_hits", 0),
        summary.get("url_param_hits", 0),
        summary.get("embedded_json_hits", 0),
        summary.get("ai_navigated", 0),
        summary.get("ai_replayed", 0),
        summary.get("ats_link_promoted", 0),
        summary["scored"],
        summary.get("classified_apply", 0),
        summary.get("classified_consider", 0),
        summary.get("classified_skip", 0),
        summary.get("classified_reject", 0),
    )
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_SUMMARY_KEYS = [
    "companies_crawled",
    "jobs_found",
    "jobs_new",
    "scored",
    "classified_apply",
    "classified_consider",
    "classified_skip",
    "classified_reject",
    "playwright_rendered",
    "interactive",
    "api_cached",
    "url_param_hits",
    "sitemap_hits",
    "embedded_json_hits",
    "ai_navigated",
    "ai_replayed",
    "ats_link_promoted",
]


def _new_summary() -> dict:
    """Return a zero-filled summary dict matching the careers_crawl schema.

    Single source of truth for summary keys — used by the TESTING-skip
    return, the top-level orchestrator summary, and the per-worker
    local_summary.
    """
    return {**dict.fromkeys(_SUMMARY_KEYS, 0), "errors": []}


def _try_ats_link_promotion(
    html: str,
    page_url: str,
    company_id: int,
    company_name: str,
    db_path: str,
    config: dict,
    local_summary: dict,
) -> bool:
    """Discover an outbound ATS link in a rendered custom page and promote (#453).

    Classifies the highest-specificity Greenhouse/Lever/Ashby/Workday/
    SmartRecruiters link in *html* via ``best_ats_candidate`` and, on a clean
    (non-conflicting) hit, promotes the company to that existing scanner
    through the audited ``promote_from_careers_link`` writer. Increments
    ``local_summary['ats_link_promoted']`` and returns ``True`` only when the
    company was actually flipped to ``hit`` (caller then skips remaining
    tiers). Fail-open: any error is swallowed so discovery never breaks the
    crawl.
    """
    from job_finder.web.ats_identity_reconcile import promote_from_careers_link
    from job_finder.web.careers_crawler._ats_link_discovery import best_ats_candidate

    try:
        candidate = best_ats_candidate(html, page_url)
        if candidate is None:
            return False
        platform, slug = candidate
        with standalone_connection(db_path) as conn:
            res = promote_from_careers_link(
                conn,
                company_id,
                platform,
                slug,
                page_url=page_url,
                config=config,
            )
        if res.get("outcome") == "promoted":
            local_summary["ats_link_promoted"] += 1
            logger.info(
                "careers_crawler: ats_link_promoted %s → %s/%s via %s",
                company_name,
                platform,
                slug[:48],
                page_url,
            )
            return True
        logger.debug(
            "careers_crawler: ats_link discovery for %s outcome=%s",
            company_name,
            res.get("outcome"),
        )
        return False
    except Exception as exc:
        logger.debug(
            "careers_crawler: ats_link discovery failed for %s: %s",
            company_name,
            exc,
        )
        return False


def _crawl_companies(
    companies: list,
    db_path: str,
    config: dict,
    target_titles: list[str],
    title_exclusions: list[str],
) -> tuple[dict, list[str]]:
    """Crawl companies in parallel with per-worker Playwright browsers.

    Each worker gets its own Playwright context + browser instance (sync API
    is not thread-safe). Companies are distributed round-robin so stalest-first
    ordering is preserved within each batch.

    Returns:
        (merged_summary, all_new_keys) — summary counters and list of new job dedup_keys.
    """
    from job_finder.web.careers_page_interactions import (
        deduplicate_keywords,
        probe_url_params,
    )

    crawl_cfg = config.get("careers_crawl", {})
    max_workers = crawl_cfg.get("max_workers", 4)
    interactive_enabled = crawl_cfg.get("interactive_enabled", True)
    search_keywords = deduplicate_keywords(target_titles)

    # --- Per-worker function (own browser + DB connection) ---
    def _crawl_worker(company_batch: list) -> tuple[dict, list[str]]:
        local_summary: dict[str, Any] = _new_summary()
        local_new_keys: list[str] = []

        # Lazy import: playwright is optional (not in base dependencies).
        # Accessing the package attribute triggers __getattr__ which imports
        # playwright and stores it in module globals for subsequent lookups.
        # If playwright is absent this raises ImportError with install instructions.
        import job_finder.web.careers_crawler as _cc

        _sp = _cc.sync_playwright

        with _sp() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for company in company_batch:
                    company_id = company["id"]
                    company_name = company["name_raw"]
                    careers_url = company["careers_url"]
                    api_endpoint = company["careers_api_endpoint"]
                    cached_tier = company["careers_crawl_tier"]
                    now = utc_now_iso()
                    tier_used = "static"
                    # Holds the Playwright-rendered DOM so the #453 ATS-link
                    # discovery pass can reuse it without a second navigation.
                    rendered_html: list[str] = []
                    promoted_via_ats_link = False

                    logger.info(
                        "careers_crawler: crawling %s via %s",
                        company_name,
                        careers_url,
                    )

                    try:
                        jobs: list[dict] = []

                        # === Tier cache: try last-successful tier first ===
                        # Skip cache replay for `static` and `sitemap` — both
                        # are cheap pre-static tiers and always run at the top
                        # of the full chain, so cache replay would be redundant.
                        if cached_tier and cached_tier not in ("static", "sitemap"):
                            jobs = _try_cached_tier(
                                cached_tier,
                                browser,
                                company,
                                careers_url,
                                api_endpoint,
                                target_titles,
                                title_exclusions,
                                search_keywords,
                                config,
                                db_path,
                                company_id,
                                local_summary,
                            )
                            if jobs:
                                tier_used = cached_tier

                        # === Full escalation chain (if cache miss) ===
                        if not jobs:
                            # Fast path: cached API endpoint
                            if api_endpoint:
                                api_jobs = _try_cached_api(
                                    api_endpoint,
                                    target_titles,
                                    title_exclusions,
                                )
                                if api_jobs is not None:
                                    jobs = api_jobs
                                    tier_used = "api_cached"
                                    local_summary["api_cached"] += 1
                                else:
                                    _clear_api_cache(db_path, company_id)

                            # Tier 0.5: Sitemap / RSS (Stage 5) — pre-static
                            # cheap probe. Returns [] if no sitemap or RSS
                            # candidates found, falling through to static.
                            if not jobs and tier_used != "api_cached":
                                sitemap_jobs = _try_sitemap_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                )
                                if sitemap_jobs:
                                    jobs = sitemap_jobs
                                    tier_used = "sitemap"
                                    local_summary["sitemap_hits"] += 1

                            # Tier 1: Static HTML
                            if not jobs and tier_used != "api_cached":
                                static_result = _try_static_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                    db_path=db_path,
                                )
                                if static_result:
                                    jobs = static_result
                                    tier_used = "static"

                            # Tier 2: URL param search
                            if not jobs and tier_used != "api_cached":
                                if search_keywords:
                                    param_jobs = probe_url_params(
                                        careers_url,
                                        search_keywords,
                                        target_titles,
                                        title_exclusions,
                                    )
                                    if param_jobs:
                                        jobs = param_jobs
                                        tier_used = "url_param"
                                        local_summary["url_param_hits"] += 1

                            # Tier 2.5: Embedded JSON extraction
                            if not jobs and tier_used != "api_cached":
                                embedded_jobs = _try_embedded_json_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                )
                                if embedded_jobs:
                                    jobs = embedded_jobs
                                    tier_used = "embedded_json"
                                    local_summary["embedded_json_hits"] += 1

                            # Tier 3: Playwright active
                            if not jobs and tier_used != "api_cached":
                                if interactive_enabled:
                                    pw_jobs, discovered_api = _try_playwright_active(
                                        browser,
                                        careers_url,
                                        target_titles,
                                        title_exclusions,
                                        search_keywords,
                                        config,
                                        db_path=db_path,
                                        html_sink=rendered_html,
                                    )
                                    jobs = pw_jobs
                                    tier_used = "playwright"
                                    local_summary["playwright_rendered"] += 1

                                    if discovered_api:
                                        _cache_api_endpoint(
                                            db_path,
                                            company_id,
                                            discovered_api,
                                        )
                                else:
                                    jobs = _try_playwright_extract(
                                        browser,
                                        careers_url,
                                        target_titles,
                                        title_exclusions,
                                        db_path=db_path,
                                        html_sink=rendered_html,
                                    )
                                    tier_used = "playwright"
                                    local_summary["playwright_rendered"] += 1

                            # === ATS-link discovery (#453): custom-site dead end ===
                            # Playwright rendered the page but title-filtered
                            # extraction found 0 jobs. Many "custom" sites are
                            # thin shells linking out to a real Greenhouse /
                            # Lever / Ashby / Workday / SmartRecruiters board.
                            # Harvest that outbound link from the already-
                            # rendered DOM and promote the company to the
                            # matching existing scanner — no second navigation,
                            # no new extractor. On a hit, skip the remaining
                            # tiers (the scanner cohort picks it up next scan).
                            ats_link_enabled = config.get("careers_crawl", {}).get(
                                "ats_link_discovery_enabled",
                                True,
                            )
                            if (
                                not jobs
                                and ats_link_enabled
                                and tier_used == "playwright"
                                and rendered_html
                            ):
                                promoted_via_ats_link = _try_ats_link_promotion(
                                    rendered_html[-1],
                                    careers_url,
                                    company_id,
                                    company_name,
                                    db_path,
                                    config,
                                    local_summary,
                                )

                            # === Tier 4: AI-navigated (replay cached recipe, or discover new) ===
                            ai_nav_enabled = config.get("careers_crawl", {}).get(
                                "ai_navigation_enabled",
                                True,
                            )
                            if not jobs and not promoted_via_ats_link and ai_nav_enabled:
                                jobs, tier_used = _try_ai_navigation(
                                    browser,
                                    company,
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
                                    config,
                                    db_path,
                                    local_summary,
                                )

                        _upsert_and_log(
                            jobs,
                            company_id,
                            company_name,
                            now,
                            db_path,
                            local_summary,
                            local_new_keys,
                            tier_used,
                        )

                    except Exception as company_err:
                        error_msg = f"{company_name}: {company_err}"
                        local_summary["errors"].append(error_msg)
                        logger.error(
                            "careers_crawler error for '%s': %s",
                            company_name,
                            company_err,
                        )
                        _update_timestamp_on_error(db_path, company_id, now)

                    time.sleep(_POLITE_DELAY)
            finally:
                browser.close()

        return local_summary, local_new_keys

    # --- Distribute companies round-robin across workers ---
    batches = [companies[i::max_workers] for i in range(max_workers)]

    merged_summary: dict[str, Any] = dict.fromkeys(_SUMMARY_KEYS, 0)
    merged_summary["errors"] = []
    all_new_keys: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_crawl_worker, batch) for batch in batches if batch]
        for future in concurrent.futures.as_completed(futures):
            try:
                worker_summary, worker_keys = future.result()
                for key in _SUMMARY_KEYS:
                    merged_summary[key] += worker_summary.get(key, 0)
                merged_summary["errors"].extend(worker_summary.get("errors", []))
                all_new_keys.extend(worker_keys)
            except Exception as worker_err:
                merged_summary["errors"].append(f"Worker error: {worker_err}")
                logger.error("careers_crawler worker failed: %s", worker_err)

    return merged_summary, all_new_keys


# _try_cached_tier has been extracted to _tier_cache.py.
# _try_ai_navigation has been extracted to _ai_nav_tier.py.
# Both re-imported into the package namespace so tests and the
# orchestrator (_crawl_companies, above) keep dispatching through
# job_finder.web.careers_crawler.X.
from job_finder.web.careers_crawler._ai_nav_tier import _try_ai_navigation

# Persistence helpers — extracted to _persistence.py and re-exported.
from job_finder.web.careers_crawler._persistence import (
    _update_timestamp_on_error,
    _upsert_and_log,
)

# Scoring trigger — extracted to _scoring.py and re-exported.
from job_finder.web.careers_crawler._scoring import _score_new_jobs
from job_finder.web.careers_crawler._tier_cache import _try_cached_tier
