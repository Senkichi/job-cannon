"""Playwright tiers for the careers crawler — tier 2 (passive) + tier 3 (active).

Both functions accept the Playwright `Browser` instance as their first
parameter; the browser's lifetime is owned by the orchestrator (the
`with sync_playwright() as pw:` block in `_crawl_companies` over in
`careers_crawler/__init__.py`). This explicit-parameter contract is the
reason the Playwright tiers extract cleanly: the browser-context
lifetime concern flagged by `module-shapes.md` is contained at the
orchestrator boundary, not leaked across module boundaries.

Tier 2 (`_try_playwright_extract`) is the passive path: render once,
parse the DOM, hand off to the static-tier extractor.

Tier 3 (`_try_playwright_active`) is the active path: render, then
exercise the page (load-more clicks, infinite scroll, pagination follow,
search-form submission) and capture API endpoints discovered along the
way. Returns both the matched jobs AND a discovered API endpoint URL
when one was the source of the matches, so the caller can persist it
for fast-path access on the next crawl.
"""

from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup

from job_finder.web._http_constants import _HEADERS, _TIMEOUT
from job_finder.web.careers_crawler._autoheal_seam import (
    record_careers_capture,
    try_careers_override,
)
from job_finder.web.careers_crawler._static_tier import (
    _extract_candidates,
    _extract_jobs_from_soup,
    _filter_candidates,
)

logger = logging.getLogger(__name__)

_PLAYWRIGHT_TIMEOUT_MS = 15000  # Page load timeout
_JS_SETTLE_MS = 2000  # Wait for JS to finish rendering
_INTERACTION_DELAY_S = 0.5  # Delay between intra-company requests


def _try_playwright_extract(
    browser,
    url: str,
    target_titles: list[str],
    exclusions: list[str],
    db_path: str | None = None,
) -> list[dict]:
    """Render page with Playwright and extract jobs from rendered DOM.

    Args:
        browser: Playwright Browser instance (already launched).
        url: Careers page URL to render.
        target_titles: Target title keywords.
        exclusions: Exclusion keywords.
        db_path: Optional path to SQLite DB for Phase-B raw-HTML capture.

    Returns:
        List of matched job dicts. Empty on timeout/error.
    """
    page = None
    try:
        page = browser.new_page()
        page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(_JS_SETTLE_MS)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        candidates = _extract_candidates(soup, url)
        generic_jobs = _filter_candidates(candidates, target_titles, exclusions)

        # --- Autoheal D4: override first; generic is the shadow comparator ---
        ovr_jobs, ovr_structural = try_careers_override(html, url, target_titles, exclusions)
        used_override = bool(ovr_jobs)
        jobs = ovr_jobs if used_override else generic_jobs

        record_careers_capture(
            db_path,
            url,
            html,
            generic_structural=len(candidates),
            override_structural=ovr_structural,
            used_override=used_override,
            filtered_count=len(jobs),
        )

        return jobs

    except Exception as e:
        logger.debug("Playwright render failed for '%s': %s", url, e)
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _try_playwright_active(
    browser,
    url: str,
    target_titles: list[str],
    exclusions: list[str],
    search_keywords: list[str],
    config: dict,
    db_path: str | None = None,
) -> tuple[list[dict], str | None]:
    """Render page with Playwright, interact to discover more jobs.

    Combines passive rendering with active interaction: load-more clicking,
    infinite scroll, pagination following, search form submission, and API
    endpoint interception.

    Args:
        browser: Playwright Browser instance (already launched).
        url: Careers page URL to render.
        target_titles: Target title keywords for filtering.
        exclusions: Title keywords for exclusion filter.
        search_keywords: Deduplicated keywords for search form submission.
        config: App config dict (for interaction limits).
        db_path: Optional path to SQLite DB for Phase-B raw-HTML capture.

    Returns:
        Tuple of (jobs_list, discovered_api_endpoint_or_None).
    """
    from job_finder.web.careers_page_interactions import (
        click_load_more,
        follow_pagination,
        parse_api_response,
        scroll_for_content,
        setup_api_capture,
        submit_search_form,
    )

    crawl_cfg = config.get("careers_crawl", {})
    max_load_more = crawl_cfg.get("max_load_more_clicks", 5)
    max_pages = crawl_cfg.get("max_pagination_pages", 5)

    page = None
    discovered_api: str | None = None

    try:
        page = browser.new_page()

        # Set up API request capture before navigation
        captured_apis = setup_api_capture(page)

        page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(_JS_SETTLE_MS)

        # Extract initial jobs from rendered DOM
        all_jobs: list[dict] = []
        seen_urls: set[str] = set()

        def _merge_jobs(new_jobs: list[dict]) -> None:
            for job in new_jobs:
                job_url = job.get("url", "")
                if job_url and job_url in seen_urls:
                    continue
                if job_url:
                    seen_urls.add(job_url)
                all_jobs.append(job)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # --- Autoheal D4: override-first, applied ONCE to the initial render.
        # When the override yields, its (filtered) results are the answer and
        # the interaction loop is skipped — a healed page is extracted by its
        # recipe, not by exercising generic interaction heuristics. ---
        ovr_jobs, ovr_structural = try_careers_override(html, url, target_titles, exclusions)
        if ovr_jobs:
            record_careers_capture(
                db_path,
                url,
                html,
                generic_structural=len(_extract_candidates(soup, url)),
                override_structural=ovr_structural,
                used_override=True,
                filtered_count=len(ovr_jobs),
            )
            return ovr_jobs, None

        initial = _extract_jobs_from_soup(soup, url, target_titles, exclusions)
        _merge_jobs(initial)

        # --- Interaction sequence ---

        # 1. Click "Load more" buttons
        if click_load_more(page, max_clicks=max_load_more):
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            _merge_jobs(_extract_jobs_from_soup(soup, url, target_titles, exclusions))

        # 2. Scroll for infinite scroll
        if scroll_for_content(page):
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            _merge_jobs(_extract_jobs_from_soup(soup, url, target_titles, exclusions))

        # 3. Pagination (only if still 0 jobs)
        if not all_jobs:
            page_urls = follow_pagination(page, url, max_pages=max_pages)
            for page_url in page_urls:
                try:
                    resp = requests.get(
                        page_url,
                        timeout=_TIMEOUT,
                        headers=_HEADERS,
                    )
                    if resp.status_code < 400:
                        page_soup = BeautifulSoup(resp.text, "html.parser")
                        _merge_jobs(
                            _extract_jobs_from_soup(
                                page_soup,
                                page_url,
                                target_titles,
                                exclusions,
                            )
                        )
                except Exception:
                    pass
                time.sleep(_INTERACTION_DELAY_S)

        # 4. Search form submission (only if still 0 jobs)
        if not all_jobs and search_keywords:
            for keyword in search_keywords[:2]:
                if submit_search_form(page, keyword):
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    _merge_jobs(
                        _extract_jobs_from_soup(
                            soup,
                            url,
                            target_titles,
                            exclusions,
                        )
                    )
                    if all_jobs:
                        break
                    time.sleep(_INTERACTION_DELAY_S)

        # 5. Check captured API endpoints
        if captured_apis:
            for api_url in captured_apis:
                try:
                    resp = requests.get(
                        api_url,
                        timeout=_TIMEOUT,
                        headers=_HEADERS,
                    )
                    if resp.status_code < 400:
                        data = resp.json()
                        api_jobs = parse_api_response(
                            data,
                            target_titles,
                            exclusions,
                            url,
                        )
                        if api_jobs:
                            _merge_jobs(api_jobs)
                            discovered_api = api_url
                            break
                except Exception:
                    continue

        if all_jobs:
            logger.info(
                "playwright_active('%s'): %d jobs via interaction",
                url,
                len(all_jobs),
            )

        # --- Autoheal D3: record final rendered page once at exit (detect=True).
        # There is no single extraction call here (six interaction-driven
        # extraction points accumulate all_jobs), so the structural count is
        # taken from the FINAL page DOM: interactions accumulate, and the
        # final DOM is what a heal recipe would face. filtered_count carries
        # the accumulated matched total for yield metrics (I4). ---
        if db_path:
            try:
                final_html = page.content()
                final_candidates = _extract_candidates(
                    BeautifulSoup(final_html, "html.parser"), url
                )
                record_careers_capture(
                    db_path,
                    url,
                    final_html,
                    generic_structural=len(final_candidates),
                    override_structural=None,
                    used_override=False,
                    filtered_count=len(all_jobs),
                )
            except Exception:
                pass  # observability must never break ingestion

        return all_jobs, discovered_api

    except Exception as e:
        logger.debug("Playwright active failed for '%s': %s", url, e)
        return [], None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
