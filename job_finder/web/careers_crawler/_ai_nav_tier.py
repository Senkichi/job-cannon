"""AI-navigation tier (tier 4) for the careers crawler.

The fallback path used when the static, URL-param, and Playwright tiers
all fail to produce matched jobs. Replays a cached "navigation recipe"
when one exists, otherwise hands the page to `ai_career_navigator` to
discover a new recipe — the recipe is then cached on the company row
for fast replay on the next crawl.

`ai_career_navigator` is imported lazily inside the function so this
module's import surface stays narrow and so the AI-nav path can be
disabled at runtime by failing to import the recipe-replay helpers
without bringing the rest of the crawler down.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


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
            wait_for_snapshot_ready,
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
        # Poll the accessibility snapshot for up to 8s so SPA careers pages
        # (AMD, similar Workday-on-React shells) get a chance to render before
        # discovery's 50-char snapshot guard rejects them as "too short".
        # Returns early as soon as the snapshot exceeds the threshold.
        wait_for_snapshot_ready(page)

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
                wait_for_snapshot_ready(page)
            except (json.JSONDecodeError, Exception) as e:
                logger.debug("ai_nav: recipe parse/replay error: %s", e)
                clear_nav_recipe(db_path, company_id)
                page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
                wait_for_snapshot_ready(page)

        # Phase A: Discover new recipe
        recipe = discover_navigation_recipe(page, careers_url, target_titles, config)
        if recipe:
            cache_nav_recipe(db_path, company_id, recipe)

            # Re-navigate and replay the freshly discovered recipe
            page.goto(careers_url, timeout=15000, wait_until="domcontentloaded")
            wait_for_snapshot_ready(page)

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
