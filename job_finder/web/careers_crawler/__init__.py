"""Active careers page crawler for companies with proven relevance.

Provides crawl_careers_batch() — a daily scheduled job that:
1. Loads all companies that have ever had a high-scoring job (classification IN ('apply','consider'))
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
import json
import logging
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from job_finder.db import derive_classification
from job_finder.web.db_helpers import standalone_connection
from job_finder.web._http_constants import _HEADERS, _TIMEOUT

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRESHNESS_DAYS = 1  # Re-crawl daily to catch new postings early
_POLITE_DELAY = 1.0  # Seconds between companies

# Static extraction — extracted to _static_tier. Re-exported here so
# both internal callers (_try_cached_tier, _crawl_companies) and the
# public surface (test patches on _try_static_extract; lazy imports of
# _extract_jobs_from_soup from careers_page_interactions and
# ai_career_navigator) keep resolving from
# job_finder.web.careers_crawler.X.
from job_finder.web.careers_crawler._static_tier import (  # noqa: E402
    _STATIC_MIN_TEXT_LEN,
    _STATIC_TEXT_RATIO,
    _extract_jobs_from_soup,
    _extract_jsonld_postings,
    _try_static_extract,
)


# ---------------------------------------------------------------------------
# Playwright tiers (passive + active) — extracted to _playwright_tier.
# Re-exported so test patches like
# @patch('job_finder.web.careers_crawler._try_playwright_active') keep
# resolving and so internal callers (_try_cached_tier, _crawl_companies)
# can continue dispatching through the package namespace.
# ---------------------------------------------------------------------------

from job_finder.web.careers_crawler._playwright_tier import (  # noqa: E402
    _INTERACTION_DELAY_S,
    _JS_SETTLE_MS,
    _PLAYWRIGHT_TIMEOUT_MS,
    _try_playwright_active,
    _try_playwright_extract,
)


# ---------------------------------------------------------------------------
# API cache helpers — extracted to _api_cache.py
# ---------------------------------------------------------------------------

from job_finder.web.careers_crawler._api_cache import (  # noqa: E402
    _cache_api_endpoint,
    _clear_api_cache,
    _try_cached_api,
)


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
    4. Score new jobs via scoring_orchestrator (Haiku → Sonnet)
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
        return {
            "companies_crawled": 0,
            "jobs_found": 0,
            "jobs_new": 0,
            "scored": 0,
            "classified_apply": 0,
            "classified_consider": 0,
            "classified_skip": 0,
            "classified_reject": 0,
            "playwright_rendered": 0,
            "interactive": 0,
            "api_cached": 0,
            "url_param_hits": 0,
            "ai_navigated": 0,
            "ai_replayed": 0,
            "errors": [],
        }

    profile_cfg = config.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    summary = {
        "companies_crawled": 0,
        "jobs_found": 0,
        "jobs_new": 0,
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "playwright_rendered": 0,
        "interactive": 0,
        "api_cached": 0,
        "url_param_hits": 0,
        "ai_navigated": 0,
        "ai_replayed": 0,
        "errors": [],
    }
    all_new_job_keys: list[str] = []

    # Load all companies that have ever had a high-scoring job
    # (v3.0 Phase 34 Plan 3 Commit A: classification IN ('apply','consider')
    # replaces haiku_score >= threshold)
    with standalone_connection(db_path) as conn:
        freshness_days = config.get("careers_crawl", {}).get("freshness_days", _FRESHNESS_DAYS)

        companies = conn.execute(
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

    if not companies:
        logger.info("careers_crawler: no companies due for crawling")
        return summary

    logger.info("careers_crawler: %d companies in batch", len(companies))

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

    # --- Haiku/Sonnet scoring for newly discovered jobs ---
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
                    datetime.now().isoformat(),
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
        "%d playwright, %d interactive, %d api-cached, %d url-param, "
        "%d ai-navigated, %d ai-replayed, %d scored "
        "(apply=%d, consider=%d, skip=%d, reject=%d)",
        summary["companies_crawled"],
        summary["jobs_found"],
        summary["jobs_new"],
        summary["playwright_rendered"],
        summary.get("interactive", 0),
        summary.get("api_cached", 0),
        summary.get("url_param_hits", 0),
        summary.get("ai_navigated", 0),
        summary.get("ai_replayed", 0),
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
    "ai_navigated",
    "ai_replayed",
]


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
        local_summary = dict.fromkeys(_SUMMARY_KEYS, 0)
        local_summary["errors"] = []
        local_new_keys: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for company in company_batch:
                    company_id = company["id"]
                    company_name = company["name_raw"]
                    careers_url = company["careers_url"]
                    api_endpoint = company["careers_api_endpoint"]
                    cached_tier = company["careers_crawl_tier"]
                    now = datetime.now().isoformat()
                    tier_used = "static"

                    logger.info(
                        "careers_crawler: crawling %s via %s",
                        company_name,
                        careers_url,
                    )

                    try:
                        jobs: list[dict] = []

                        # === Tier cache: try last-successful tier first ===
                        if cached_tier and cached_tier != "static":
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

                            # Tier 1: Static HTML
                            if not jobs and tier_used != "api_cached":
                                static_result = _try_static_extract(
                                    careers_url,
                                    target_titles,
                                    title_exclusions,
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
                                    )
                                    tier_used = "playwright"
                                    local_summary["playwright_rendered"] += 1

                            # === Tier 4: AI-navigated (replay cached recipe, or discover new) ===
                            ai_nav_enabled = config.get("careers_crawl", {}).get(
                                "ai_navigation_enabled",
                                True,
                            )
                            if not jobs and ai_nav_enabled:
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

    merged_summary: dict = dict.fromkeys(_SUMMARY_KEYS, 0)
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


def _try_cached_tier(
    cached_tier: str,
    browser,
    company: dict,
    careers_url: str,
    api_endpoint: str | None,
    target_titles: list[str],
    title_exclusions: list[str],
    search_keywords: list[str],
    config: dict,
    db_path: str,
    company_id: int,
    local_summary: dict,
) -> list[dict]:
    """Attempt extraction using the previously successful tier.

    Returns a list of job dicts on success, empty list on failure (triggering
    full escalation chain in the caller).
    """
    from job_finder.web.careers_page_interactions import probe_url_params

    try:
        if cached_tier == "api_cached" and api_endpoint:
            api_jobs = _try_cached_api(api_endpoint, target_titles, title_exclusions)
            if api_jobs is not None:
                local_summary["api_cached"] += 1
                return api_jobs
        elif cached_tier == "url_param" and search_keywords:
            param_jobs = probe_url_params(
                careers_url,
                search_keywords,
                target_titles,
                title_exclusions,
            )
            if param_jobs:
                local_summary["url_param_hits"] += 1
                return param_jobs
        elif cached_tier == "playwright":
            crawl_cfg = config.get("careers_crawl", {})
            interactive_enabled = crawl_cfg.get("interactive_enabled", True)
            if interactive_enabled:
                pw_jobs, discovered_api = _try_playwright_active(
                    browser,
                    careers_url,
                    target_titles,
                    title_exclusions,
                    search_keywords,
                    config,
                )
                if pw_jobs:
                    local_summary["playwright_rendered"] += 1
                    if discovered_api:
                        _cache_api_endpoint(db_path, company_id, discovered_api)
                    return pw_jobs
            else:
                pw_jobs = _try_playwright_extract(
                    browser,
                    careers_url,
                    target_titles,
                    title_exclusions,
                )
                if pw_jobs:
                    local_summary["playwright_rendered"] += 1
                    return pw_jobs
        elif cached_tier in ("ai_replay", "ai_navigate"):
            try:
                nav_recipe_raw = company["careers_nav_recipe"]
            except (KeyError, IndexError):
                nav_recipe_raw = None
            if nav_recipe_raw:
                try:
                    from job_finder.web.ai_career_navigator import (
                        RecipeStaleError,
                        replay_navigation_recipe,
                    )

                    recipe = json.loads(nav_recipe_raw)
                    page = browser.new_page()
                    try:
                        page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                        jobs = replay_navigation_recipe(
                            page,
                            recipe,
                            target_titles,
                            title_exclusions,
                        )
                        if jobs:
                            local_summary["ai_replayed"] += 1
                            return jobs
                    except RecipeStaleError:
                        from job_finder.web.ai_career_navigator import clear_nav_recipe

                        clear_nav_recipe(db_path, company_id)
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
                except (json.JSONDecodeError, ImportError):
                    pass
    except Exception:
        pass  # Cache miss — fall through to full escalation

    return []


def _try_ai_navigation(
    browser,
    company: dict,
    careers_url: str,
    target_titles: list[str],
    title_exclusions: list[str],
    config: dict,
    db_path: str,
    local_summary: dict,
) -> tuple[list[dict], str]:
    """Try AI-navigated extraction: replay cached recipe, or discover new one.

    Returns:
        Tuple of (jobs_list, tier_used_string). tier_used defaults to "static"
        if AI navigation produces nothing.
    """
    try:
        from job_finder.web.ai_career_navigator import (
            RecipeStaleError,
            cache_nav_recipe,
            clear_nav_recipe,
            discover_navigation_recipe,
            replay_navigation_recipe,
        )
    except ImportError:
        return [], "static"

    company_id = company["id"]
    try:
        nav_recipe_raw = company["careers_nav_recipe"]
    except (KeyError, IndexError):
        nav_recipe_raw = None

    page = None
    try:
        page = browser.new_page()
        page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Phase B: Try replaying cached recipe first
        if nav_recipe_raw:
            try:
                recipe = json.loads(nav_recipe_raw)
                jobs = replay_navigation_recipe(
                    page,
                    recipe,
                    target_titles,
                    title_exclusions,
                )
                if jobs:
                    local_summary["ai_replayed"] += 1
                    return jobs, "ai_replay"
            except RecipeStaleError:
                logger.info(
                    "ai_nav: stale recipe for %s — re-discovering",
                    company.get("name_raw", company_id),
                )
                clear_nav_recipe(db_path, company_id)
                # Re-navigate for fresh discovery
                page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except (json.JSONDecodeError, Exception) as e:
                logger.debug("ai_nav: recipe parse/replay error: %s", e)
                clear_nav_recipe(db_path, company_id)
                page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

        # Phase A: Discover new recipe
        recipe = discover_navigation_recipe(page, careers_url, target_titles, config)
        if recipe:
            cache_nav_recipe(db_path, company_id, recipe)

            # Re-navigate and replay the freshly discovered recipe
            page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            try:
                jobs = replay_navigation_recipe(
                    page,
                    recipe,
                    target_titles,
                    title_exclusions,
                )
                if jobs:
                    local_summary["ai_navigated"] += 1
                    return jobs, "ai_navigate"
            except RecipeStaleError:
                pass

    except Exception as e:
        logger.debug("ai_nav: error for %s: %s", careers_url, e)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    return [], "static"


def _upsert_and_log(
    jobs: list[dict],
    company_id: int,
    company_name: str,
    now: str,
    db_path: str,
    summary: dict,
    all_new_job_keys: list[str],
    tier_used: str,
) -> None:
    """Upsert discovered jobs and update company timestamps."""
    from job_finder.db import upsert_job
    from job_finder.models import Job

    company_jobs_found = len(jobs)
    company_jobs_new = 0
    summary["jobs_found"] += company_jobs_found

    with standalone_connection(db_path) as upsert_conn:
        for scraped_job in jobs:
            try:
                job = Job(
                    title=scraped_job["title"],
                    company=company_name,
                    location="",
                    source="careers_crawl",
                    source_url=scraped_job.get("url") or "",
                    salary_min=None,
                    salary_max=None,
                    description=scraped_job.get("description", ""),
                )
                is_new = upsert_job(upsert_conn, job)
                if is_new:
                    summary["jobs_new"] += 1
                    company_jobs_new += 1
                    all_new_job_keys.append(job.dedup_key)
            except Exception as job_err:
                error_msg = f"{company_name} job error: {job_err}"
                summary["errors"].append(error_msg)
                logger.warning("careers_crawler job error: %s", error_msg)

    with standalone_connection(db_path) as ts_conn:
        ts_conn.execute(
            """UPDATE companies
               SET careers_crawl_last_at = ?,
                   last_scanned_at = ?,
                   careers_crawl_tier = ?,
                   jobs_found_total = (
                       SELECT COUNT(*) FROM jobs WHERE company_id = ?
                   )
               WHERE id = ?""",
            (now, now, tier_used, company_id, company_id),
        )
        ts_conn.execute(
            """INSERT INTO company_scan_log
               (company_id, scanned_at, jobs_found, jobs_matched)
               VALUES (?, ?, ?, ?)""",
            (company_id, now, company_jobs_new, company_jobs_found),
        )
        ts_conn.commit()

    summary["companies_crawled"] += 1

    if company_jobs_found:
        logger.info(
            "careers_crawler: %s — %d jobs found (%d new) [%s]",
            company_name,
            company_jobs_found,
            company_jobs_new,
            tier_used,
        )


def _update_timestamp_on_error(
    db_path: str,
    company_id: int,
    now: str,
) -> None:
    """Update crawl timestamp on error so company doesn't block the queue."""
    try:
        with standalone_connection(db_path) as err_conn:
            err_conn.execute(
                "UPDATE companies SET careers_crawl_last_at = ? WHERE id = ?",
                (now, company_id),
            )
            err_conn.commit()
    except Exception:
        pass


def _score_new_jobs(
    db_path: str,
    config: dict,
    new_job_keys: list[str],
    summary: dict,
) -> None:
    """Score newly discovered jobs via the unified v3.0 scorer.

    v3.0 (Phase 34 Plan 3 Commit A): routes through score_and_persist_job so the
    `classification` column populates on every scored row; per-classification
    counters replace haiku_scored / sonnet_evaluated.
    """
    try:
        from job_finder.web.scoring_orchestrator import score_and_persist_job
    except ImportError:
        logger.debug("scoring_orchestrator not available — skipping scoring")
        return

    try:
        from job_finder.web.model_provider import tier_has_configured_provider
    except ImportError:
        logger.debug("model_provider not available — skipping scoring")
        return

    try:
        from job_finder.web.data_enricher import enrich_job
    except ImportError:
        enrich_job = None  # type: ignore[assignment]

    # Build scoring client
    _scoring_client = None
    try:
        import anthropic

        _scoring_client = anthropic.Anthropic()
    except (ImportError, Exception):
        pass

    if not tier_has_configured_provider("scoring", config, _scoring_client):
        logger.debug("No routable scoring provider — skipping careers_crawl scoring")
        return

    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")

    with standalone_connection(db_path) as conn:
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue

                job_row = dict(row)

                # Enrich BEFORE scoring — careers_crawl produces title+URL only
                # shells, so the scorer would otherwise read an empty description.
                if enrich_job is not None and (
                    not job_row.get("jd_full")
                    or job_row.get("salary_min") is None
                    or not job_row.get("location")
                ):
                    try:
                        enriched = enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "careers_crawl enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

                result = score_and_persist_job(
                    job_row,
                    conn,
                    config,
                )
                if result is None:
                    continue
                summary["scored"] = summary.get("scored", 0) + 1
                if getattr(result, "status", None) != "ok" or result.data is None:
                    continue
                cls = derive_classification(result.data.sub_scores, job_row.get("legitimacy_note"))
                key = f"classified_{cls}"
                summary[key] = summary.get(key, 0) + 1
            except Exception as e:
                logger.warning(
                    "careers_crawl scoring error for '%s': %s",
                    dedup_key,
                    e,
                    exc_info=True,
                )
