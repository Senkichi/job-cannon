"""SmartRecruiters platform scanner (registry form).

GET-paginated Posting API. Per-job description requires a secondary
GET; ``_fetch_smartrecruiters_description`` lives in ``ats_platforms.py``
because ``tests/test_smartrecruiters_scanner.py`` imports it directly.
This module calls it via lazy import to avoid a circular dependency.
"""

from __future__ import annotations

import logging
import time

import requests

from job_finder.web.ats_platforms_internal._registry import PlatformScanner
from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.location_canonical import JobLocation

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_RESULTS = 500
_DETAIL_FETCH_SLEEP_S = 0.1
# Pacing for the LIST endpoint between successive page fetches. Pre-F1
# (commit b99e1d9) the list-endpoint cadence was incidentally paced by
# the per-matched-posting detail-fetch sleep in the same per-page loop.
# See .planning/specs/2026-05-26-polish-review-audit.md (MAJOR — Workday
# + SmartRecruiters pagination).
_PAGE_FETCH_SLEEP_S = 0.1


def _fetch_postings(slug: str) -> list[dict]:
    """GET + paginate over SmartRecruiters /v1/companies/{slug}/postings."""
    base_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    offset = 0
    out: list[dict] = []
    total_fetched = 0

    while offset < _MAX_RESULTS:
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        try:
            resp = requests.get(
                base_url,
                params={"offset": offset, "limit": _PAGE_SIZE},
                headers={"Accept": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("scan_smartrecruiters('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            logger.debug("scan_smartrecruiters('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("scan_smartrecruiters('%s') JSON parse error: %s", slug, exc)
            break

        total_found = data.get("totalFound", 0)
        postings = data.get("content", [])
        if not postings:
            break

        out.extend(postings)
        total_fetched += len(postings)
        offset += _PAGE_SIZE

        if total_fetched >= total_found:
            break

    return out


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping for SmartRecruiters posting → list[JobLocation].

    SmartRecruiters returns ``location.{city, region, regionCode, country,
    countryCode, remote}``. ``remote: true`` is the workplace_type signal.
    Single location per posting (no multi-location array on the v1 list
    endpoint).
    """
    loc = posting.get("location")
    if not isinstance(loc, dict):
        return []
    city = (loc.get("city") or "").strip() or None
    region = (loc.get("region") or "").strip() or None
    region_code = (loc.get("regionCode") or "").strip().upper() or None
    country = (loc.get("country") or "").strip() or None
    country_code = (loc.get("countryCode") or "").strip().upper() or None
    workplace_type = "REMOTE" if loc.get("remote") else "UNSPECIFIED"
    if not any((city, region, region_code, country, country_code)) and workplace_type == "UNSPECIFIED":
        return []
    raw = ", ".join(
        p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if isinstance(p, str) and p
    )
    return [
        JobLocation(
            city=city,
            region=region,
            region_code=region_code,
            country=country,
            country_code=country_code,
            workplace_type=workplace_type,
            raw=raw,
            unresolved=False,
        )
    ]


def _posting_to_job(posting: dict, slug: str) -> dict:
    from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

    loc = posting.get("location", {})
    if isinstance(loc, dict):
        parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        location = ", ".join(p for p in parts if isinstance(p, str) and p)
    else:
        location = ""

    posting_id = posting.get("id", "")
    source_url = f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else ""

    description = _fetch_smartrecruiters_description(slug, posting_id) if posting_id else ""

    # Polite pacing between per-job detail fetches.
    time.sleep(_DETAIL_FETCH_SLEEP_S)

    return {
        "title": posting.get("name", ""),
        "company_source": "SmartRecruiters",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="smartrecruiters",
    company_source="SmartRecruiters",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name", ""),
    posting_to_job=_posting_to_job,
)
