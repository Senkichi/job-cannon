"""Tier cache replay for the careers crawler.

The orchestrator records which extraction tier last produced jobs for a
given company on the `careers_crawl_tier` column. On the next run,
`_try_cached_tier` short-circuits the full escalation chain by trying
that tier first; on success the orchestrator skips the rest of the
escalation, on failure (empty result) it falls through to the full
chain.

This module sits at the top of the tier dependency graph — it composes
the api-cache, Playwright, and AI-nav tiers — and is therefore the
last leaf to extract before the orchestrator itself.
"""

from __future__ import annotations

import json
import logging

from job_finder.web.careers_crawler._api_cache import _cache_api_endpoint, _try_cached_api
from job_finder.web.careers_crawler._playwright_tier import (
    _try_playwright_active,
    _try_playwright_extract,
)

logger = logging.getLogger(__name__)


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
