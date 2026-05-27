"""Ashby platform scanner (registry form).

Ashby slugs are CASE-SENSITIVE (Research Pitfall 3): jobs.ashbyhq.com/OpenAI
is not jobs.ashbyhq.com/openai. The slug is forwarded verbatim.

A single timeout retry handles the Ashby-side intermittency seen on
2026-05-26 07:41-07:50 when ~20 tenants in sequence returned Read
timeouts inside a 9-minute window. Capped at one retry so a sustained
Ashby outage cannot double the run time of the whole ATS scan.
"""

from __future__ import annotations

import json

from job_finder.web.ats_platforms_internal._registry import (
    PlatformScanner,
    _http_get_json,
)


def _fetch_postings(slug: str) -> list[dict]:
    # NOTE: No lowercasing — Ashby slugs are case-sensitive.
    data = _http_get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        log_label="scan_ashby",
        slug=slug,
        retry_on_timeout=True,
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _posting_to_job(posting: dict, _slug: str) -> dict:
    salary_min = None
    salary_max = None
    comp_json = None
    compensation = posting.get("compensation")
    if compensation:
        comp_json = json.dumps(compensation)
        summary_components = compensation.get("summaryComponents") or []
        for component in summary_components:
            if component.get("compensationType") == "base_salary":
                salary_min = component.get("minValue")
                salary_max = component.get("maxValue")
                break

    location = posting.get("location") or ""
    if not location and posting.get("isRemote"):
        location = "Remote"

    description = posting.get("descriptionPlain") or posting.get("descriptionHtml") or ""

    return {
        "title": posting.get("title", ""),
        "company_source": "Ashby",
        "location": location,
        "description": description,
        "source_url": posting.get("jobUrl") or "",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "comp_json": comp_json,
    }


SCANNER = PlatformScanner(
    name="ashby",
    company_source="Ashby",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
