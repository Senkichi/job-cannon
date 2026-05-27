"""Greenhouse platform scanner (registry form).

CRITICAL: ``pay_input_ranges`` values are in cents — divide by 100 for
dollars (Research Pitfall 7).
"""

from __future__ import annotations

import json

from job_finder.web.ats_platforms_internal._registry import (
    PlatformScanner,
    _http_get_json,
)


def _fetch_postings(slug: str) -> list[dict]:
    data = _http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true",
        log_label="scan_greenhouse",
        slug=slug,
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _posting_to_job(posting: dict, _slug: str) -> dict:
    salary_min = None
    salary_max = None
    comp_json = None
    pay_ranges = posting.get("pay_input_ranges") or []
    if pay_ranges:
        first_range = pay_ranges[0]
        min_cents = first_range.get("min_cents")
        max_cents = first_range.get("max_cents")
        if min_cents is not None:
            salary_min = min_cents // 100
        if max_cents is not None:
            salary_max = max_cents // 100
        comp_json = json.dumps(pay_ranges)

    location_obj = posting.get("location") or {}
    location = location_obj.get("name") or "" if isinstance(location_obj, dict) else ""

    return {
        "title": posting.get("title", ""),
        "company_source": "Greenhouse",
        "location": location,
        "description": posting.get("content") or "",
        "source_url": posting.get("absolute_url") or "",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "comp_json": comp_json,
    }


SCANNER = PlatformScanner(
    name="greenhouse",
    company_source="Greenhouse",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
