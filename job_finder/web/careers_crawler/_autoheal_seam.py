"""Careers-crawler autoheal seam — override-first extraction + capture (Phase D / D4).

Shared by the static and Playwright tiers so the override/shadow/capture
logic is identical at every extraction site. Both functions are fail-open:
an override or capture error must never break crawling.

Override-first with generic shadow: when a per-company override recipe
exists and yields title-matched jobs, its results are used and the GENERIC
structural count rides along as ``legacy_count`` — D2's shadow machinery
then retires a stale override that the generic extractor structurally
outperforms ``SHADOW_ROLLBACK_WINS`` times consecutively (costing nothing:
the soup the generic count comes from is already parsed at every site).
"""

from __future__ import annotations

import logging

from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def try_careers_override(
    html: str,
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> tuple[list[dict], int | None]:
    """Apply the per-company careers override to *html*, when one exists.

    Returns ``(filtered_jobs, structural_count)`` — ``([], None)`` when no
    override file exists for the company or anything fails. *structural_count*
    is the override's pre-title-filter yield (I4: the detection signal).
    """
    try:
        from job_finder.web.ats_platforms import _title_matches
        from job_finder.web.autoheal import careers_source_key
        from job_finder.web.autoheal import override_loader as _ol
        from job_finder.web.autoheal.recipe_extractor import careers_recipe_extract

        recipe = _ol.careers_recipe(careers_source_key(url))
        if recipe is None:
            return [], None
        raw = careers_recipe_extract(recipe, html, url)
        matched = [d for d in raw if _title_matches(d["title"], target_titles, exclusions)]
        return matched, len(raw)
    except Exception:
        logger.exception("careers override failed for '%s'; using generic path", url)
        return [], None


def record_careers_capture(
    db_path: str | None,
    url: str,
    html: str,
    *,
    generic_structural: int,
    override_structural: int | None,
    used_override: bool,
    filtered_count: int,
) -> None:
    """Record the per-company corpus sample + break-counter update. Never raises.

    Structural counts only (I4): "your roles were filled" must not look like
    "the page broke". When the override produced the returned jobs, the
    generic structural count is passed as ``legacy_count`` (shadow guard).
    """
    if not db_path:
        return
    try:
        from job_finder.web.autoheal import careers_source_key
        from job_finder.web.autoheal.health_monitor import record_extraction

        with standalone_connection(db_path) as conn:
            record_extraction(
                conn,
                careers_source_key(url),
                "careers",
                html[:50000],
                job_count=override_structural if used_override else generic_structural,
                detect=True,
                legacy_count=generic_structural if used_override else None,
                extractor="override" if used_override else "generic",
                filtered_count=filtered_count,
            )
            conn.commit()
    except Exception:
        pass  # observability must never break ingestion
