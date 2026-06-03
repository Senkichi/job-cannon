"""Greenhouse platform scanner (registry form).

Salary resolution (D-07 / F-06): ``pay_input_ranges`` fields are named
``min_cents``/``max_cents`` but Greenhouse sometimes returns whole-dollar
values at those paths (e.g. hourly $64 stored as ``min_cents=64``).

Resolution rule by ``unit``/``interval`` field:
- ``"year"`` AND value > 1_000 → value is cents; divide by 100.
- Any other case (``"hour"``, unknown, missing) → value is already dollars;
  store as-is.
- Missing ``unit``/``interval`` AND value ≤ 1_000 → ambiguous but small;
  store as-is.
- Missing ``unit``/``interval`` AND value > 1_000 → ambiguous large value;
  store as-is so Phase 49.02 unit-tagging can flag the suspect row.

The ``_normalize_salary`` inversion check in db/_jobs.py still applies after
this scanner; this module's job is only to resolve the cents-vs-dollars
question before writing salary_min/salary_max.
"""

from __future__ import annotations

import json
import logging

from job_finder.web.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)

logger = logging.getLogger(__name__)


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


def _resolve_salary(value: int | None, interval: str | None) -> int | None:
    """Convert a ``pay_input_ranges`` value to whole dollars.

    Greenhouse names the salary fields ``min_cents``/``max_cents`` but the
    unit depends on the posting's ``unit``/``interval`` field:

    - ``"year"`` AND value > 1_000 → value is in cents; divide by 100.
    - Any other interval (``"hour"``, ``"month"``, unknown, missing) →
      value is already in dollars; return as-is.

    This is a pure function so it can be unit-tested independently of HTTP.
    """
    if value is None:
        return None
    if interval == "year" and value > 1_000:
        return value // 100
    return value


def _to_canonical(posting: dict) -> list:
    """Layer-1 mapping for Greenhouse posting → list[JobLocation].

    Greenhouse exposes only a freeform ``location.name`` string (no
    structured city/region/country fields). We run it through
    ``parse_locations()`` (Layer 2/3) at ingest time so structured-location
    data is available immediately rather than waiting for the m067 backfill.

    Returns ``[]`` when the location field is absent or blank.
    """
    from job_finder.web.location_parser import parse_locations

    location_obj = posting.get("location")
    if not isinstance(location_obj, dict):
        return []
    location_name = (location_obj.get("name") or "").strip()
    if not location_name:
        return []
    try:
        return parse_locations(location_name)
    except Exception as exc:
        logger.debug(
            "greenhouse _to_canonical: parse_locations failed for %r: %s", location_name, exc
        )
        return []


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # ── Salary (F-06 cents/dollars resolution) ──────────────────────────────
    salary_min = None
    salary_max = None
    comp_json = None
    pay_ranges = posting.get("pay_input_ranges") or []
    if pay_ranges:
        first_range = pay_ranges[0]
        # Greenhouse may use "unit" or "interval" for the pay period field.
        interval = first_range.get("unit") or first_range.get("interval") or None
        min_val = first_range.get("min_cents")
        max_val = first_range.get("max_cents")
        salary_min = _resolve_salary(min_val, interval)
        salary_max = _resolve_salary(max_val, interval)
        comp_json = json.dumps(pay_ranges)

    # ── Location (flat string + structured Layer-1 emission) ─────────────────
    location_obj = posting.get("location") or {}
    location = location_obj.get("name") or "" if isinstance(location_obj, dict) else ""

    # ── source_id (F-04: was missing on 98% of rows) ─────────────────────────
    posting_id = posting.get("id")
    source_id = str(posting_id) if posting_id is not None else None

    # ── posted_date (F-02: was missing on 100% of rows) ──────────────────────
    # Greenhouse returns ISO-8601 strings (e.g. "2024-01-15T10:30:00Z").
    posted_date: str | None = posting.get("updated_at") or posting.get("created_at") or None

    return {
        "title": posting.get("title", ""),
        "company_source": "Greenhouse",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": posting.get("content") or "",
        "source_url": posting.get("absolute_url") or "",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "comp_json": comp_json,
        "source_id": source_id,
        "posted_date": posted_date,
    }


SCANNER = PlatformScanner(
    name="greenhouse",
    company_source="Greenhouse",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
