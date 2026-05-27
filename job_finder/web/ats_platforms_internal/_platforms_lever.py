"""Lever platform scanner (registry form)."""

from __future__ import annotations

import json

from job_finder.web.ats_platforms_internal._registry import (
    PlatformScanner,
    _http_get_json,
)


def _fetch_postings(slug: str) -> list[dict]:
    data = _http_get_json(
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        log_label="scan_lever",
        slug=slug,
    )
    return data if isinstance(data, list) else []


def _posting_to_job(posting: dict, slug: str) -> dict:
    salary_range = posting.get("salaryRange") or {}
    salary_min = salary_range.get("min") if salary_range else None
    salary_max = salary_range.get("max") if salary_range else None
    comp_json = json.dumps(salary_range) if salary_range else None

    categories = posting.get("categories") or {}
    location = categories.get("location") or categories.get("team") or ""

    return {
        "title": posting.get("text", ""),
        "company_source": "Lever",
        "location": location,
        "description": posting.get("descriptionPlain") or "",
        "source_url": posting.get("hostedUrl") or "",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "comp_json": comp_json,
    }


SCANNER = PlatformScanner(
    name="lever",
    company_source="Lever",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("text", ""),
    posting_to_job=_posting_to_job,
)
