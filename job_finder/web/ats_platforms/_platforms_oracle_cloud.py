"""Oracle Recruiting Cloud (ORC / Fusion Candidate Experience) platform scanner.

Oracle's Fusion HCM Recruiting exposes a public, unauthenticated REST finder for
every Candidate-Experience site::

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.workLocation,requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit={N},offset={M},sortBy=POSTING_DATES_DESC

``{host}`` is the full Fusion pod hostname (``{pod}.fa.{region}.oraclecloud.com``,
e.g. ``ibtcjb.fa.ocs.oraclecloud.com``) and ``{site}`` is the CE site number
(``CX_1`` is the near-universal default for single-site tenants). The registry
``slug`` packs both as ``"{host}|{site}"`` — analogous to Workday's
``"{subdomain}/{board}"`` and Eightfold's ``"host|domain"``.

Response shape: ``{"items": [{"TotalJobsCount": N, "requisitionList": [...]}]}``.
Each requisition carries ``Id``, ``Title``, ``PostedDate`` (already ISO),
``PrimaryLocation``, ``WorkplaceTypeCode`` and a short ``ShortDescriptionStr``.
The list endpoint omits the full job description; ``jd_full`` is filled later by
enrichment from the per-requisition detail endpoint.

Offset pagination (page size :data:`_PAGE_SIZE`) up to ``TotalJobsCount``, capped
at :data:`_MAX_RESULTS`. A first-page 404/410 means the pod/site stopped
resolving → :class:`BoardGoneError` so the stale ``hit`` is demoted. Any other
error returns ``[]``.
"""

from __future__ import annotations

import logging
import time

import requests

from job_finder.web.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    coerce_remote_bool,
    label_or_str,
)
from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.location_parser import parse_locations

logger = logging.getLogger(__name__)

_REST_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_PAGE_SIZE = 50
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.2
_DEFAULT_SITE = "CX_1"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _split_slug(slug: str) -> tuple[str, str]:
    """``"{host}|{site}"`` → ``(host, site)``; missing site defaults to CX_1."""
    host, _, site = (slug or "").partition("|")
    return host.strip(), (site.strip() or _DEFAULT_SITE)


def _job_url(host: str, site: str, req_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}"


def _is_remote(posting: dict) -> bool | None:
    """Tri-state remote flag from Oracle's WorkplaceTypeCode / WorkplaceType."""
    code = (posting.get("WorkplaceTypeCode") or "").upper()
    if "REMOTE" in code:
        return True
    if "ON_SITE" in code or "HYBRID" in code:
        return False
    text = (posting.get("WorkplaceType") or "").lower()
    if "remote" in text:
        return True
    if "on-site" in text or "hybrid" in text:
        return False
    return coerce_remote_bool(None)


def _fetch_postings(slug: str) -> list[dict]:
    """Fetch one ORC Candidate-Experience site, offset-paginated.

    Raises :class:`BoardGoneError` when the FIRST page returns 404/410 (the
    pod/site stopped resolving). Returns ``[]`` on any other error so one dead
    tenant cannot crash a multi-company scan.
    """
    host, site = _split_slug(slug)
    if not host:
        return []

    base = f"https://{host}{_REST_PATH}"
    out: list[dict] = []
    offset = 0

    while offset < _MAX_RESULTS:
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        # Oracle's finder syntax uses literal ; , = delimiters — build the query
        # string by hand rather than via params= (which would percent-encode the
        # delimiters and break the finder). site is [A-Za-z0-9_], offset/limit are
        # ints, so no escaping is required.
        url = (
            f"{base}?onlyData=true"
            "&expand=requisitionList.workLocation,requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},limit={_PAGE_SIZE},"
            f"offset={offset},sortBy=POSTING_DATES_DESC"
        )
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_PROBE_TIMEOUT)
        except Exception as exc:
            logger.warning("scan_oracle_cloud('%s') request failed: %s", slug, exc)
            break

        if offset == 0 and resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code != 200:
            logger.debug("scan_oracle_cloud('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_oracle_cloud('%s') JSON parse error: %s", slug, exc)
            break

        items = payload.get("items") or []
        if not items:
            break
        item0 = items[0] or {}
        reqs = item0.get("requisitionList") or []
        if not reqs:
            break

        out.extend(reqs)

        total = int(item0.get("TotalJobsCount") or 0)
        offset += _PAGE_SIZE
        if offset >= total or len(reqs) < _PAGE_SIZE:
            break

    return out


def _posting_to_job(posting: dict, slug: str) -> dict | None:
    req_id = posting.get("Id")
    if req_id is None:
        return None
    host, site = _split_slug(slug)
    location = posting.get("PrimaryLocation") or ""

    return {
        "title": posting.get("Title", ""),
        "company_source": "Oracle Cloud",
        "location": location,
        "locations_structured": parse_locations(location),
        # List endpoint only carries a short blurb; jd_full is filled by
        # enrichment from the per-requisition detail endpoint.
        "description": posting.get("ShortDescriptionStr", "") or "",
        "source_url": _job_url(host, site, str(req_id)),
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": str(req_id),
        "posted_date": posting.get("PostedDate") or None,
        "is_remote": _is_remote(posting),
        "employment_type": label_or_str(posting.get("JobSchedule")),
        "department": label_or_str(posting.get("Department") or posting.get("Organization")),
    }


SCANNER = PlatformScanner(
    name="oracle_cloud",
    company_source="Oracle Cloud",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("Title", ""),
    posting_to_job=_posting_to_job,
)
