"""Pinpoint platform scanner (registry form).

Public JSON at ``https://{slug}.pinpointhq.com/postings.json`` → ``{"data": [...]}``.
Single-shot — Pinpoint returns every active posting in one response with
no pagination. Each item carries title, url, location dict
({city, name, province}), compensation_minimum/maximum, employment_type,
workplace_type, and a job.department.name nested under "job".
"""

from __future__ import annotations

from job_finder.web.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from job_finder.web.description_formatter import strip_html_to_text


def _fetch_postings(slug: str) -> list[dict]:
    payload = _http_get_json(
        f"https://{slug}.pinpointhq.com/postings.json",
        log_label="scan_pinpoint",
        slug=slug,
    )
    if not isinstance(payload, dict):
        return []
    postings = payload.get("data")
    if not isinstance(postings, list):
        return []
    # Filter out non-dict items defensively (matches historical scan_pinpoint).
    return [p for p in postings if isinstance(p, dict)]


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # _fetch_postings already filters out non-dicts; this function is only
    # called by run_platform_scan via that filter.
    loc_obj = posting.get("location") or {}
    if isinstance(loc_obj, dict):
        parts = [
            loc_obj.get("city") or "",
            loc_obj.get("province") or loc_obj.get("name") or "",
        ]
        location = ", ".join(p for p in parts if p)
    else:
        location = ""

    description_raw = posting.get("description") or posting.get("description_html") or ""
    description = (
        strip_html_to_text(description_raw) if "<" in description_raw else description_raw
    )

    source_url = posting.get("url") or posting.get("apply_url") or ""

    salary_min = posting.get("compensation_minimum")
    salary_max = posting.get("compensation_maximum")

    return {
        "title": posting.get("title") or "",
        "company_source": "Pinpoint",
        "location": location,
        "description": description,
        "source_url": source_url,
        "salary_min": salary_min if isinstance(salary_min, (int, float)) else None,
        "salary_max": salary_max if isinstance(salary_max, (int, float)) else None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="pinpoint",
    company_source="Pinpoint",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title") or "",
    posting_to_job=_posting_to_job,
)
