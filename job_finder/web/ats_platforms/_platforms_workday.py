"""Workday platform scanner (registry form).

Workday exposes a standardized POST JSON API across all tenants at
``/wday/cxs/{tenant}/{board}/jobs``. Slug format is ``"{subdomain}/{board}"``
(e.g. ``"walmart.wd5/WalmartExternal"``).

Per-job description requires a secondary GET against the detail
endpoint. ``_fetch_workday_description`` lives in ``ats_platforms.py``
because it is imported directly by ``tests/test_workday_scanner.py``;
this module calls it via a lazy import to avoid a circular dependency.

Layer-1 emission (Phase 48.02):
  - ``source_id``: the posting's ``externalPath`` (unique per job per
    Workday board; the Workday requisition ID is embedded in the path,
    e.g. ``"/job/Senior-Data-Scientist_R-12345"``). Using ``externalPath``
    rather than attempting to parse ``bulletFields`` array entries avoids
    reliance on tenant-specific field names while still providing a stable
    per-posting identifier.
  - ``posted_date``: parsed from ``postedOn`` (date string, typically
    ``"MM/DD/YYYY"`` or ISO ``"YYYY-MM-DD"``). Treated as UTC midnight.
  - ``locations_structured``: Layer-1 ``JobLocation`` list parsed from
    ``locationsText`` with workplace-type detection (REMOTE/HYBRID) and
    best-effort ``City, ST`` extraction for US addresses.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from job_finder.web.ats_platforms._registry import PlatformScanner
from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.location_canonical import JobLocation, WorkplaceType, dedupe_locations

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20
_MAX_RESULTS = 200
_DETAIL_FETCH_SLEEP_S = 0.1
# Pacing for the LIST endpoint between successive page fetches. Pre-F1
# (commit b99e1d9) the list-endpoint cadence was incidentally paced by
# the per-matched-posting detail-fetch sleep in the same per-page loop.
# Restoring an explicit inter-page delay preserves the polite-pacing
# intent for high-page-count Workday tenants. See
# .planning/specs/2026-05-26-polish-review-audit.md (MAJOR — Workday +
# SmartRecruiters pagination).
_PAGE_FETCH_SLEEP_S = 0.1

# ---------------------------------------------------------------------------
# Location-parsing helpers (Layer-1)
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)

# "City Name, XX" where XX is a 2-letter code (US state or CA province).
# Anchored so "Hybrid - San Francisco, CA" doesn't match the raw text;
# callers strip workplace-type prefixes before applying this pattern.
_US_CITY_STATE_RE = re.compile(r"^([A-Za-z][A-Za-z\s\-\.\']+),\s*([A-Z]{2})\s*$")

# Tokens that are purely workplace-type keywords (handled specially).
_WORKPLACE_ONLY_TOKENS: frozenset[str] = frozenset({"remote", "hybrid", "onsite", "on-site"})

# Prefixes that Workday sometimes prepends to a city ("Hybrid - City, ST").
_WORKPLACE_PREFIX_RE = re.compile(r"^(?:remote|hybrid|onsite|on-site)\s*[-–—]\s*", re.IGNORECASE)


def _detect_workplace_type(text: str) -> WorkplaceType:
    """Infer WorkplaceType from a location token string."""
    if _REMOTE_RE.search(text):
        return "REMOTE"
    if _HYBRID_RE.search(text):
        return "HYBRID"
    return "UNSPECIFIED"


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping: Workday posting → list[JobLocation].

    Parses ``locationsText`` (a flat semicolon/pipe-separated string) into
    ``JobLocation`` objects. Each segment is:
      - A pure workplace-type keyword (``"Remote"``, ``"Hybrid"``) →
        workplace-type-only ``JobLocation`` with ``unresolved=True``.
      - A ``"City, ST"`` US pattern (after stripping any leading keyword
        prefix) → fully-structured ``JobLocation`` with ``unresolved=False``.
      - Anything else → ``unresolved=True`` preserving ``raw``.

    Multi-location postings (e.g. ``"New York, NY; Remote"``) produce one
    ``JobLocation`` per resolved segment. Duplicates are removed by
    ``dedupe_locations``.
    """
    locations_text = (posting.get("locationsText") or "").strip()
    if not locations_text:
        return []

    # Split on semicolons and pipes (Workday uses both as multi-location
    # separators depending on tenant configuration).
    segments = [s.strip() for s in re.split(r"[;|]", locations_text) if s.strip()]

    results: list[JobLocation] = []
    for segment in segments:
        workplace_type = _detect_workplace_type(segment)

        # Pure keyword segments — no city/region data to extract.
        if segment.lower() in _WORKPLACE_ONLY_TOKENS:
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )
            continue

        # Strip any leading workplace-type prefix before trying city parse.
        clean = _WORKPLACE_PREFIX_RE.sub("", segment).strip()

        m = _US_CITY_STATE_RE.match(clean)
        if m:
            city = m.group(1).strip()
            region_code = m.group(2).upper()
            results.append(
                JobLocation(
                    city=city,
                    region=None,
                    region_code=region_code,
                    country="United States",
                    country_code="US",
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=False,
                )
            )
        else:
            # Can't structurally resolve — preserve raw for audit/display.
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )

    return dedupe_locations(results)


def _parse_posted_date(value: str | None) -> datetime | None:
    """Parse a Workday ``postedOn`` string to a naive UTC ``datetime``.

    Handles two formats seen across Workday tenants:
      - ``"MM/DD/YYYY"`` (most common, e.g. ``"01/15/2024"``)
      - ``"YYYY-MM-DD"`` (ISO date, e.g. ``"2024-01-15"``)

    Other formats (relative strings like ``"Posted 3 Days Ago"``, or empty)
    are silently dropped; caller receives ``None`` and a NULL is written to
    ``posted_date`` (per D-08 — no synthesis from first_seen).
    """
    if not value:
        return None
    # ISO date: "YYYY-MM-DD"
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        pass
    # US date: "MM/DD/YYYY"
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y")
    except ValueError:
        pass
    logger.debug("scan_workday: unrecognised postedOn format %r — skipping", value)
    return None


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _fetch_postings_with_completeness(slug: str) -> tuple[list[dict], bool]:
    """POST + paginate over Workday CXS list endpoint, tracking completeness.

    Returns ``(postings, complete)`` where ``complete`` is ``True`` only
    when the board was **fully** fetched:

    - First-page error (network / HTTP / JSON) → ``complete=False``.
    - ``total > _MAX_RESULTS`` → ``complete=False`` (board too large to paginate).
    - Pagination stops before ``total_fetched >= total`` → ``complete=False``.
    - Genuine empty board (``total=0``) → ``complete=True``.

    The completeness flag is the gate used by the ATS reconciler to decide
    whether expiry-reconciliation is safe for a Workday tenant.  A warning
    is logged whenever the board is incomplete so operators can see which
    tenants exceed the pagination cap.
    """
    parts = slug.split("/", 1)
    if len(parts) != 2:
        logger.warning("scan_workday: invalid slug format '%s'", slug)
        return [], False

    subdomain, board = parts
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    api_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    offset = 0
    out: list[dict] = []
    total_fetched = 0
    saw_total = False
    total = 0

    while offset < _MAX_RESULTS:
        # Inter-page pacing — does not run on the first iteration; the wait
        # is before the *next* POST, not before the current one.
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

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
        saw_total = True

        if total > _MAX_RESULTS:
            logger.warning(
                "scan_workday('%s') board has %d postings (cap %d) — incomplete; "
                "reconciliation will skip this tenant",
                slug,
                total,
                _MAX_RESULTS,
            )
            break

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

    complete = saw_total and total_fetched >= total
    return out, complete


def _fetch_postings(slug: str) -> list[dict]:
    """POST + paginate over Workday CXS list endpoint.

    Returns the raw posting list; description fetches happen later in
    ``_posting_to_job`` so the title-match gate runs first and we only
    pay for detail fetches on matched postings.

    Thin wrapper around :func:`_fetch_postings_with_completeness` — the
    completeness signal is consumed by the ATS reconciler but is not
    needed by the standard scanner flow.
    """
    return _fetch_postings_with_completeness(slug)[0]


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

    # --- Layer-1 emission (Phase 48.02) ------------------------------------
    # source_id: use externalPath as the stable per-job identifier.
    # externalPath is unique per posting per board (e.g. "/job/Title_R-12345")
    # and is already the key used to build source_url and fetch descriptions.
    # Using the full path avoids parsing the requisition suffix, which varies
    # by tenant configuration.
    source_id: str | None = external_path if external_path else None

    # posted_date: parsed from postedOn (varies by tenant format).
    posted_date = _parse_posted_date(posting.get("postedOn"))

    # locations_structured: Layer-1 parse of locationsText with
    # workplace-type detection and best-effort City, ST extraction.
    locations_structured = _to_canonical(posting)
    # -----------------------------------------------------------------------

    return {
        "title": posting.get("title", ""),
        "company_source": "Workday",
        "location": location,
        "locations_structured": locations_structured,
        "description": description,
        "source_url": source_url,
        "source_id": source_id,
        "posted_date": posted_date,
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
