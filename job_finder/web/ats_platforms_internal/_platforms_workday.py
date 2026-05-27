"""Workday platform scanner (registry form).

Workday exposes a standardized POST JSON API across all tenants at
``/wday/cxs/{tenant}/{board}/jobs``. Slug format is ``"{subdomain}/{board}"``
(e.g. ``"walmart.wd5/WalmartExternal"``).

Per-job description requires a secondary GET against the detail
endpoint. ``_fetch_workday_description`` lives in ``ats_platforms.py``
because it is imported directly by ``tests/test_workday_scanner.py``;
this module calls it via a lazy import to avoid a circular dependency.
"""

from __future__ import annotations

import logging
import time

import requests

from job_finder.web.ats_platforms_internal._registry import PlatformScanner
from job_finder.web.ats_prober import _PROBE_TIMEOUT

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20
_MAX_RESULTS = 200
_DETAIL_FETCH_SLEEP_S = 0.1


def _fetch_postings(slug: str) -> list[dict]:
    """POST + paginate over Workday CXS list endpoint.

    Returns the raw posting list; description fetches happen later in
    ``_posting_to_job`` so the title-match gate runs first and we only
    pay for detail fetches on matched postings.
    """
    parts = slug.split("/", 1)
    if len(parts) != 2:
        logger.warning("scan_workday: invalid slug format '%s'", slug)
        return []

    subdomain, board = parts
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    api_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    offset = 0
    out: list[dict] = []
    total_fetched = 0

    while offset < _MAX_RESULTS:
        body = {
            "appliedFacets": {},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        try:
            resp = requests.post(
                api_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("scan_workday('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            logger.debug("scan_workday('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("scan_workday('%s') JSON parse error: %s", slug, exc)
            break

        total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break

        # Stash the slug-derived URL parts on each posting so _posting_to_job
        # can build source_url + call the detail endpoint without re-parsing.
        for posting in postings:
            posting["__workday_subdomain"] = subdomain
            posting["__workday_tenant"] = tenant
            posting["__workday_board"] = board
        out.extend(postings)

        total_fetched += len(postings)
        offset += _PAGE_SIZE

        if total_fetched >= total:
            break

    return out


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # Lazy import — _fetch_workday_description lives in ats_platforms.py because
    # tests/test_workday_scanner.py imports it directly, and the registry must
    # not depend on the flat module at import time (would risk a cycle once
    # the flat module delegates back to run_platform_scan).
    from job_finder.web.ats_platforms import _fetch_workday_description

    subdomain = posting.get("__workday_subdomain", "")
    tenant = posting.get("__workday_tenant", "")
    board = posting.get("__workday_board", "")
    external_path = posting.get("externalPath", "")
    location = posting.get("locationsText", "")

    # externalPath from the CXS API already begins with "/job/...".
    # Do NOT prepend another "/job/" — earlier templates emitted
    # "/job//job/..." URLs that 406'd at the API.
    source_url = (
        f"https://{subdomain}.myworkdayjobs.com/en-US/{board}{external_path}"
        if external_path
        else ""
    )

    description = (
        _fetch_workday_description(subdomain, tenant, board, external_path)
        if external_path
        else ""
    )

    # Polite pacing between per-job detail fetches.
    time.sleep(_DETAIL_FETCH_SLEEP_S)

    return {
        "title": posting.get("title", ""),
        "company_source": "Workday",
        "location": location,
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="workday",
    company_source="Workday",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
