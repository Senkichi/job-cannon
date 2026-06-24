"""Discovered API-endpoint cache for the careers crawler.

Three small functions that manage the per-company `careers_api_endpoint`
column on the `companies` table. The orchestrator caches an endpoint
when the Playwright tier intercepts an XHR that returns matchable
postings; subsequent runs short-circuit to the API directly.

All functions are best-effort — exceptions are logged at debug level
and swallowed so a transient DB hiccup never aborts a crawl.
"""

from __future__ import annotations

import logging

import requests

from job_finder.web._http_constants import _HEADERS, _TIMEOUT
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)


def _try_cached_api(
    api_endpoint: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Try fetching jobs from a previously discovered API endpoint.

    Returns:
        list[dict] — jobs found (may be empty but endpoint is working)
        None — endpoint is broken/unreachable (caller should clear cache)
    """
    from job_finder.web.careers_page_interactions import parse_api_response

    try:
        resp = fetch_with_deadline(
            api_endpoint, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
        )
        if resp.status_code >= 400:
            logger.debug(
                "Cached API endpoint returned %d: %s",
                resp.status_code,
                api_endpoint,
            )
            return None

        data = resp.json()
        return parse_api_response(data, target_titles, exclusions)

    except Exception as e:
        logger.debug("Cached API endpoint failed: %s — %s", api_endpoint, e)
        return None


def _cache_api_endpoint(
    db_path: str,
    company_id: int,
    api_endpoint: str,
) -> None:
    """Store a discovered API endpoint for future fast-path access."""
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE companies SET careers_api_endpoint = ? WHERE id = ?",
                (api_endpoint, company_id),
            )
            conn.commit()
        logger.info(
            "Cached API endpoint for company %d: %s",
            company_id,
            api_endpoint,
        )
    except Exception as e:
        logger.debug("Failed to cache API endpoint: %s", e)


def _clear_api_cache(db_path: str, company_id: int) -> None:
    """Clear a stale cached API endpoint."""
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE companies SET careers_api_endpoint = NULL WHERE id = ?",
                (company_id,),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Failed to clear API cache: %s", e)
