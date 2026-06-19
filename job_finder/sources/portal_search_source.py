"""Portal-targeted job discovery — free APIs first, SERP fallback second.

Three tiers of portal discovery, in order:
  1. FREE API portals — RemoteOK, Remotive, Himalayas have free JSON APIs.
     Zero cost, keyword-filtered client-side.
  2. SERP portals — site: queries through DataForSEO (cheapest SERP provider)
     for portals without APIs. Batched into a single task submission.
  3. Skip — if no DataForSEO key is configured, SERP portals are silently skipped.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Protocol

import requests
from bs4 import BeautifulSoup
from ftfy import fix_text

from job_finder.models import Job
from job_finder.sources._error_envelope import VendorAccountError

logger = logging.getLogger(__name__)


class _SerpBackend(Protocol):
    """Duck-type for DataForSEOSource / GoogleCSESource interchangeability.

    Both backends accept ``site:domain keyword`` queries through a common
    entry point and return Job objects. ``fetch_serp_portals`` selects which
    backend to use based on which is configured.
    """

    def fetch_jobs(self, queries: list[dict]) -> list[Job]: ...


_REQUEST_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (compatible; JobCannon/1.0)"

# ---------------------------------------------------------------------------
# SERP-only portals (need site: queries via DataForSEO)
# ---------------------------------------------------------------------------

SERP_PORTALS: list[dict[str, str]] = [
    {"domain": "wellfound.com", "name": "wellfound"},
    {"domain": "weworkremotely.com", "name": "weworkremotely"},
    {"domain": "trueup.io", "name": "trueup"},
    {"domain": "builtin.com/jobs", "name": "builtin"},
    {"domain": "ycombinator.com/jobs", "name": "yc_jobs"},
    {"domain": "jobs.workable.com", "name": "workable"},
    {"domain": "job-boards.greenhouse.io", "name": "greenhouse_boards"},
    {"domain": "jobs.ashbyhq.com", "name": "ashby_boards"},
    {"domain": "jobs.lever.co", "name": "lever_boards"},
    {"domain": "ai-jobs.net", "name": "ai_jobs"},
    {"domain": "startup.jobs", "name": "startup_jobs"},
    {"domain": "remotefrontendjobs.com", "name": "remote_frontend"},
]


# ---------------------------------------------------------------------------
# Free API fetchers — zero cost, keyword-filtered client-side
# ---------------------------------------------------------------------------


def _fetch_remoteok(keywords: list[str]) -> list[Job]:
    """Fetch from RemoteOK free JSON API. No auth required."""
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("RemoteOK API failed: %s", e)
        return []

    # First item is metadata, skip it
    listings = data[1:] if isinstance(data, list) and len(data) > 1 else []
    keywords_lower = [k.lower() for k in keywords]
    jobs = []

    for item in listings:
        title = item.get("position") or ""
        company = item.get("company") or ""
        if not title or not company:
            continue

        # Client-side keyword filter
        text = f"{title} {item.get('description', '')} {' '.join(item.get('tags', []))}".lower()
        if not any(kw in text for kw in keywords_lower):
            continue

        jobs.append(
            Job(
                title=_clean_text(title),
                company=_clean_text(company),
                location=_clean_text(item.get("location") or "") or "Remote",
                source="portal_remoteok",
                source_url=item.get("apply_url") or item.get("url") or "",
                **_feed_salary_from_values(
                    item.get("salary_min"), item.get("salary_max"), raw_text="remoteok"
                ),
                description=_truncate(_clean_text(item.get("description"))),
            )
        )

    logger.info("RemoteOK: %d jobs matched from %d listings", len(jobs), len(listings))
    return jobs


def _fetch_remotive(keywords: list[str]) -> list[Job]:
    """Fetch from Remotive free JSON API. No auth required."""
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Remotive API failed: %s", e)
        return []

    listings = data.get("jobs", [])
    keywords_lower = [k.lower() for k in keywords]
    jobs = []

    for item in listings:
        title = item.get("title") or ""
        company = item.get("company_name") or ""
        if not title or not company:
            continue

        text = f"{title} {item.get('description', '')} {' '.join(item.get('tags', []))}".lower()
        if not any(kw in text for kw in keywords_lower):
            continue

        jobs.append(
            Job(
                title=_clean_text(title),
                company=_clean_text(company),
                location=_clean_text(item.get("candidate_required_location") or "") or "Remote",
                source="portal_remotive",
                source_url=item.get("url") or "",
                **_feed_salary_from_text(item.get("salary") or ""),
                description=_truncate(_clean_text(item.get("description"))),
            )
        )

    logger.info("Remotive: %d jobs matched from %d listings", len(jobs), len(listings))
    return jobs


def _fetch_himalayas(keywords: list[str]) -> list[Job]:
    """Fetch from Himalayas free JSON API. No auth required.

    Supports server-side search via query param, so we make one request
    per keyword to avoid downloading the entire 100K+ listing catalog.

    Stage 7.5 parse hygiene:
      - description is raw HTML; we strip tags via BeautifulSoup before storage
        so the scoring prompt sees clean prose, not <div>/<a>/<p> markup.
      - truncate cap widened from 2000 -> 8000 chars to match the jd_full
        eager-promote write width in job_finder/db/_jobs.py (post-strip the
        actual char counts are well under this).
      - posted_date populated from `pubDate` (Unix seconds).
      - ftfy.fix_text applied to text fields; the source itself sometimes
        serves mangled UTF-8 (e.g., `we\\u2019re` round-tripped through
        cp1252 surfacing as U+FFFD / mojibake).
    """
    all_jobs: list[Job] = []
    seen_urls: set[str] = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                "https://himalayas.app/jobs/api",
                params={"q": keyword, "limit": 50},
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Himalayas API failed for '%s': %s", keyword, e)
            continue

        listings = data.get("jobs", [])

        for item in listings:
            title = _clean_text(item.get("title") or "")
            company = _clean_text(item.get("companyName") or "")
            url = item.get("applicationLink") or ""
            if not title or not company:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=_clean_text(item.get("location") or "") or "Remote",
                    source="portal_himalayas",
                    source_url=url,
                    **_feed_salary_from_values(
                        item.get("minSalary"), item.get("maxSalary"), raw_text="himalayas"
                    ),
                    description=_truncate(_strip_html(item.get("description")), max_len=8000),
                    posted_date=_unix_to_datetime(item.get("pubDate")),
                    # Feed pubDate is a machine first-published timestamp (#363).
                    # Job.__post_init__ clears the marker if pubDate fails to parse.
                    posted_date_precision="exact",
                )
            )

        time.sleep(0.5)  # Polite delay between keyword requests

    logger.info("Himalayas: %d jobs matched", len(all_jobs))
    return all_jobs


# ---------------------------------------------------------------------------
# Stage-2 free-portal fetchers
#
# Five additional free or free-keyed sources added in 2026-05 to compensate
# for unkeyed installs losing SerpAPI/Thordata/DataForSEO. Order in this file
# mirrors the user-friction order: keyless first (Jobicy, YC), then keyed
# (USAJobs, Adzuna, Jooble). See `.planning/NO-KEY-COMPENSATION-PLAN.md`.
# ---------------------------------------------------------------------------


def _fetch_jobicy(keywords: list[str]) -> list[Job]:
    """Fetch from Jobicy v2 public JSON API. No auth required.

    Jobicy guidance is "no more than once per hour" (see jobicy.com/jobs-rss-feed),
    so we issue ONE catch-all request returning up to 50 listings rather than
    per-keyword fan-out, and filter client-side. Tag-substring matching mirrors
    how Remotive handles its keyword filter.
    """
    try:
        resp = requests.get(
            "https://jobicy.com/api/v2/remote-jobs",
            params={"count": 50},
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Jobicy API failed: %s", e)
        return []

    listings = data.get("jobs", [])
    keywords_lower = [k.lower() for k in keywords]
    jobs: list[Job] = []

    for item in listings:
        title = _clean_text(item.get("jobTitle") or "")
        company = _clean_text(item.get("companyName") or "")
        if not title or not company:
            continue

        text = f"{title} {item.get('jobExcerpt', '')} {item.get('jobDescription', '')}".lower()
        if keywords_lower and not any(kw in text for kw in keywords_lower):
            continue

        jobs.append(
            Job(
                title=title,
                company=company,
                location=_clean_text(item.get("jobGeo") or "") or "Remote",
                source="portal_jobicy",
                source_url=item.get("url") or "",
                **_feed_salary_from_values(
                    item.get("annualSalaryMin"), item.get("annualSalaryMax"), raw_text="jobicy"
                ),
                description=_truncate(
                    _clean_text(item.get("jobDescription") or item.get("jobExcerpt") or "")
                ),
            )
        )

    logger.info("Jobicy: %d jobs matched from %d listings", len(jobs), len(listings))
    return jobs


def _fetch_yc_workatastartup(keywords: list[str]) -> list[Job]:
    """Fetch from Y Combinator's Work at a Startup. No documented JSON API.

    The site is a single-page Inertia.js app — each HTML response embeds the
    full page state as a JSON blob in the root ``<div data-page="...">``
    attribute. We GET the public ``/jobs`` page with the ``query=`` param and
    parse ``props.jobs`` out of the data-page payload.

    This is structurally HTML scraping and breaks if YC migrates off Inertia,
    but the pattern has been stable for years. Spike confirmed 2026-05-21.
    See plan Stage 2 / open question Q3-style "permission to drop if fragile";
    chose to ship rather than drop.

    Stage 7.5 parse hygiene:
      - The unauthenticated detail URL is gated (login wall, no data-page
        attr on GET), so we cannot fetch the canonical JD body during
        ingestion. We instead synthesize a structured description from the
        listing-payload metadata (title / role type / company one-liner /
        location / salary / batch / employment type). The synthesized text
        exceeds the 200-char eager-promote threshold in
        `job_finder/db/_jobs.py:174-180` so `jd_full` populates and the
        row becomes scoreable on its metadata signal.
      - ftfy.fix_text applied to text fields (YC sometimes ships mangled
        UTF-8 in `location` for non-US offices).
      - posted_date is not exposed on the listing payload; leave NULL.
    """
    import html as _html
    import json as _json
    import re as _re

    all_jobs: list[Job] = []
    seen_ids: set[str] = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                "https://www.workatastartup.com/jobs",
                params={"query": keyword},
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html",
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            match = _re.search(r'data-page="([^"]+)"', resp.text)
            if not match:
                logger.warning("YC workatastartup: data-page attr missing for %r", keyword)
                continue
            payload = _json.loads(_html.unescape(match.group(1)))
            listings = payload.get("props", {}).get("jobs", []) or []
        except Exception as e:
            logger.warning("YC workatastartup failed for '%s': %s", keyword, e)
            continue

        for item in listings:
            job_id = str(item.get("id") or "")
            if job_id and job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)

            title = _clean_text(item.get("title") or "")
            company = _clean_text(item.get("companyName") or "")
            if not title or not company:
                continue

            slug = item.get("companySlug") or ""
            source_url = (
                f"https://www.workatastartup.com/companies/{slug}/jobs/{job_id}"
                if slug and job_id
                else ""
            )

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=_clean_text(item.get("location") or "") or "Remote",
                    source="portal_yc_workatastartup",
                    source_url=source_url,
                    **_feed_salary_from_text(item.get("salary") or ""),
                    description=_synthesize_yc_description(item),
                )
            )

        time.sleep(0.5)  # Polite delay between keyword requests

    logger.info("YC workatastartup: %d jobs matched", len(all_jobs))
    return all_jobs


def _fetch_usajobs(
    keywords: list[str],
    *,
    user_agent_email: str,
    authorization_key: str,
) -> list[Job]:
    """Fetch from USAJobs.gov public JSON API. Free with email registration.

    Requires ``User-Agent: <email>`` and ``Authorization-Key`` headers per
    https://developer.usajobs.gov/tutorials/search-jobs. Both obtained free
    via developer.usajobs.gov registration. Returns [] if either header is
    missing so the orchestrator can short-circuit unconfigured installs
    without a network call.
    """
    if not user_agent_email or not authorization_key:
        return []

    all_jobs: list[Job] = []
    seen_keys: set[tuple[str, str]] = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                "https://data.usajobs.gov/api/search",
                params={"Keyword": keyword, "ResultsPerPage": 50},
                headers={
                    "Host": "data.usajobs.gov",
                    "User-Agent": user_agent_email,
                    "Authorization-Key": authorization_key,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("USAJobs failed for '%s': %s", keyword, e)
            continue

        items = data.get("SearchResult", {}).get("SearchResultItems", []) or []

        for item in items:
            descriptor = item.get("MatchedObjectDescriptor", {}) or {}
            title = descriptor.get("PositionTitle") or ""
            company = descriptor.get("OrganizationName") or descriptor.get("DepartmentName") or ""
            if not title or not company:
                continue

            key = (company.lower(), title.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)

            locations = descriptor.get("PositionLocation") or []
            location_str = locations[0].get("LocationName", "") if locations else ""

            remuneration = descriptor.get("PositionRemuneration") or []
            raw_salary_min = raw_salary_max = None
            if remuneration:
                first = remuneration[0]
                raw_salary_min = first.get("MinimumRange")
                raw_salary_max = first.get("MaximumRange")

            user_area = descriptor.get("UserArea", {}) or {}
            details = user_area.get("Details", {}) or {}
            description = details.get("JobSummary") or descriptor.get("QualificationSummary") or ""

            all_jobs.append(
                Job(
                    title=_clean_text(title),
                    company=_clean_text(company),
                    location=_clean_text(location_str) or "United States",
                    source="portal_usajobs",
                    source_url=descriptor.get("PositionURI") or "",
                    **_feed_salary_from_values(raw_salary_min, raw_salary_max, raw_text="usajobs"),
                    description=_truncate(_clean_text(description)),
                )
            )

        time.sleep(0.5)

    logger.info("USAJobs: %d jobs matched", len(all_jobs))
    return all_jobs


def _fetch_adzuna(
    keywords: list[str],
    *,
    app_id: str,
    app_key: str,
    country: str = "us",
) -> list[Job]:
    """Fetch from Adzuna public JSON API. Free dev tier (~250 calls/day/country).

    https://api.adzuna.com/v1/api/jobs/{country}/search/1 — required params:
    app_id, app_key, what (keyword). Defaults to country='us'. Returns [] if
    either credential is missing.
    """
    if not app_id or not app_key:
        return []

    all_jobs: list[Job] = []
    seen_keys: set[tuple[str, str]] = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": keyword,
                    "results_per_page": 50,
                    "content-type": "application/json",
                },
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Adzuna failed for '%s': %s", keyword, e)
            continue

        for item in data.get("results", []) or []:
            title = item.get("title") or ""
            company_node = item.get("company") or {}
            company = (
                company_node.get("display_name")
                if isinstance(company_node, dict)
                else (company_node or "")
            )
            if not title or not company:
                continue

            key = (company.lower(), title.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)

            location_node = item.get("location") or {}
            location_str = (
                location_node.get("display_name")
                if isinstance(location_node, dict)
                else (location_node or "")
            ) or "Remote"

            all_jobs.append(
                Job(
                    title=_clean_text(title),
                    company=_clean_text(company),
                    location=_clean_text(location_str),
                    source="portal_adzuna",
                    source_url=item.get("redirect_url") or "",
                    **_feed_salary_from_values(
                        item.get("salary_min"), item.get("salary_max"), raw_text="adzuna"
                    ),
                    description=_truncate(_clean_text(item.get("description"))),
                )
            )

        time.sleep(0.5)

    logger.info("Adzuna: %d jobs matched", len(all_jobs))
    return all_jobs


def _fetch_jooble(keywords: list[str], *, api_key: str) -> list[Job]:
    """Fetch from Jooble public JSON POST API. Free with email registration.

    POST https://jooble.org/api/{api_key} with a JSON body of search params.
    See https://help.jooble.org/en/support/solutions/articles/60001448238. The
    salary field is a free-text string; we run it through ``_parse_salary_string``
    when present.
    """
    if not api_key:
        return []

    all_jobs: list[Job] = []
    seen_keys: set[tuple[str, str]] = set()

    for keyword in keywords:
        try:
            resp = requests.post(
                f"https://jooble.org/api/{api_key}",
                json={"keywords": keyword, "ResultOnPage": 50},
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Jooble failed for '%s': %s", keyword, e)
            continue

        for item in data.get("jobs", []) or []:
            title = item.get("title") or ""
            company = item.get("company") or ""
            if not title or not company:
                continue

            key = (company.lower(), title.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)

            all_jobs.append(
                Job(
                    title=_clean_text(title),
                    company=_clean_text(company),
                    location=_clean_text(item.get("location") or "") or "Remote",
                    source="portal_jooble",
                    source_url=item.get("link") or "",
                    **_feed_salary_from_text(item.get("salary") or ""),
                    description=_truncate(_clean_text(item.get("snippet"))),
                )
            )

        time.sleep(0.5)

    logger.info("Jooble: %d jobs matched", len(all_jobs))
    return all_jobs


# ---------------------------------------------------------------------------
# SERP-backed portal search (DataForSEO)
# ---------------------------------------------------------------------------


def fetch_serp_portals(
    keywords: list[str],
    dataforseo_source: _SerpBackend | None,
    portals: list[dict[str, str]] | None = None,
    max_queries: int = 30,
    google_cse_source: _SerpBackend | None = None,
) -> list[Job]:
    """Run site: queries through DataForSEO or Google CSE for portals without free APIs.

    Backend selection (per PLAN.md §3 load-bearing decision #2): DataForSEO is
    preferred when both backends are configured because it has no daily quota
    and supports query batching. Google CSE is the free-tier fallback used
    when only CSE is configured. When neither is configured the function
    returns ``[]`` silently.

    DataForSEO charges ~$0.0006 per 10 results; CSE is free up to 100/day with
    a 95-query defense-in-depth gate enforced inside ``GoogleCSESource``.

    Args:
        keywords: Search terms.
        dataforseo_source: Optional DataForSEOSource instance. Preferred when set.
        portals: Portal list (defaults to SERP_PORTALS).
        max_queries: Cap on total SERP queries to prevent runaway costs.
        google_cse_source: Optional GoogleCSESource instance. Used only when
            ``dataforseo_source`` is None and CSE is configured.

    Returns:
        Deduplicated list of Job objects.
    """
    backend = dataforseo_source if dataforseo_source is not None else google_cse_source
    if backend is None:
        return []

    portal_list = portals if portals is not None else SERP_PORTALS
    seen_urls: set[str] = set()
    all_jobs: list[Job] = []

    # Build all queries, respecting the cap
    queries: list[dict] = []
    portal_map: dict[str, str] = {}  # query string -> portal name
    for keyword in keywords:
        for portal in portal_list:
            if len(queries) >= max_queries:
                break
            q = f"site:{portal['domain']} {keyword}"
            queries.append({"query": q, "location": ""})
            portal_map[q] = portal["name"]
        if len(queries) >= max_queries:
            break

    if not queries:
        return []

    backend_name = "DataForSEO" if dataforseo_source is not None else "Google CSE"
    logger.info(
        "Portal SERP search: submitting %d queries to %s",
        len(queries),
        backend_name,
    )

    try:
        raw_jobs = backend.fetch_jobs(queries)
    except VendorAccountError:
        # Account / credential / quota / expiry failures (#437) must surface,
        # not be masked as an empty portal run — propagate to
        # _fetch_portal_search. Transient transport errors (a bare RuntimeError
        # such as "CSE 503") are NOT account failures and fall through to the
        # best-effort swallow below.
        raise
    except Exception:
        logger.warning("Portal SERP search failed", exc_info=True)
        return []

    for job in raw_jobs:
        if job.source_url in seen_urls:
            continue
        seen_urls.add(job.source_url)

        # Determine which portal this came from by matching the source_url
        # against portal domains (more reliable than query mapping for batched results).
        portal_name = _detect_portal_from_url(job.source_url, portal_list)

        all_jobs.append(
            Job(
                title=job.title,
                company=job.company,
                location=job.location,
                source=f"portal_{portal_name}" if portal_name else "portal_serp",
                source_url=job.source_url,
                # No source_id: portal aggregator IDs are search-result tokens,
                # not per-job-stable platform IDs (I-11).
                salary_min=job.salary_min,
                salary_max=job.salary_max,
                # Carry the salary provenance + lossless observation the backend
                # (DataForSEO / CSE) already captured through the normalizer (D-1).
                salary_currency=job.salary_currency,
                salary_period=job.salary_period,
                salary_provenance=job.salary_provenance,
                salary_observations=list(job.salary_observations),
                description=job.description,
                posted_date=job.posted_date,
                posted_date_precision=job.posted_date_precision,
            )
        )

    logger.info(
        "Portal SERP search: %d queries -> %d jobs",
        len(queries),
        len(all_jobs),
    )
    return all_jobs


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def fetch_all_portals(
    keywords: list[str],
    dataforseo_source: _SerpBackend | None = None,
    max_serp_queries: int = 30,
    serp_portals: list[dict[str, str]] | None = None,
    portal_config: dict | None = None,
    google_cse_source: _SerpBackend | None = None,
    target_titles: list[str] | None = None,
    exclusions: list[str] | None = None,
) -> list[Job]:
    """Fetch from all portals: free APIs first, then SERP fallback.

    Args:
        keywords: Search terms.
        dataforseo_source: Optional DataForSEOSource for SERP portals.
            Preferred over CSE when both are configured.
        max_serp_queries: Cap on SERP queries (applies to whichever backend runs).
        serp_portals: Optional override for SERP portal list.
        portal_config: Optional ``sources.portal_search`` config dict. Used
            to enable/configure the Stage-2 portals (jobicy, yc_workatastartup,
            usajobs, adzuna, jooble). When omitted, those portals are skipped
            and only the three always-on free portals plus SERP run.
        google_cse_source: Optional GoogleCSESource backend. Used only when
            ``dataforseo_source`` is None — Stage 3's free substitute for the
            DataForSEO ``site:`` query path.
        target_titles: Optional title-gate list. When provided (non-empty),
            ``_title_matches`` (word-boundary regex over normalized titles)
            is applied to the merged result set; jobs whose title does not
            match any target_title are dropped. Mirrors the per-job inline
            filter that ``ats_platforms.scan_*`` already enforces, closing
            a documented Stage-0 gap (free portals' upstream ``q=`` is
            full-text and lets non-title-matching rows through). When None
            or empty, the gate is skipped (legacy / benchmark behavior).
        exclusions: Optional exclusion-keyword list paired with
            ``target_titles``. Ignored when ``target_titles`` is None/empty.

    Returns:
        Combined, deduplicated job list (post title-gate when configured).
    """
    seen_urls: set[str] = set()
    all_jobs: list[Job] = []

    def _dedup_extend(jobs: list[Job]) -> None:
        for job in jobs:
            if job.source_url and job.source_url in seen_urls:
                continue
            if job.source_url:
                seen_urls.add(job.source_url)
            all_jobs.append(job)

    # Tier 1a: Always-on free API portals (zero cost, no keys)
    _dedup_extend(_fetch_remoteok(keywords))
    _dedup_extend(_fetch_remotive(keywords))
    _dedup_extend(_fetch_himalayas(keywords))

    # Tier 1b: Stage-2 free portals (some keyless, some free with key registration).
    # Each fetcher already returns [] when its credentials are missing, so the
    # config-gated dispatch here only controls whether the fetcher RUNS at all
    # (avoids wasted network calls for opt-out portals).
    cfg = portal_config or {}
    if cfg.get("jobicy", {}).get("enabled", False):
        _dedup_extend(_fetch_jobicy(keywords))
    if cfg.get("yc_workatastartup", {}).get("enabled", False):
        _dedup_extend(_fetch_yc_workatastartup(keywords))
    usajobs_cfg = cfg.get("usajobs", {})
    if usajobs_cfg.get("enabled", False):
        _dedup_extend(
            _fetch_usajobs(
                keywords,
                user_agent_email=usajobs_cfg.get("user_agent_email", "") or "",
                authorization_key=usajobs_cfg.get("authorization_key", "") or "",
            )
        )
    adzuna_cfg = cfg.get("adzuna", {})
    if adzuna_cfg.get("enabled", False):
        _dedup_extend(
            _fetch_adzuna(
                keywords,
                app_id=adzuna_cfg.get("app_id", "") or "",
                app_key=adzuna_cfg.get("app_key", "") or "",
                country=adzuna_cfg.get("country", "us") or "us",
            )
        )
    jooble_cfg = cfg.get("jooble", {})
    if jooble_cfg.get("enabled", False):
        _dedup_extend(
            _fetch_jooble(
                keywords,
                api_key=jooble_cfg.get("api_key", "") or "",
            )
        )

    # Tier 2: SERP portals — DataForSEO (paid, batched) preferred, Google CSE
    # (free, 95/day quota) as fallback when only CSE is configured.
    if dataforseo_source is not None or google_cse_source is not None:
        serp_jobs = fetch_serp_portals(
            keywords,
            dataforseo_source,
            portals=serp_portals,
            max_queries=max_serp_queries,
            google_cse_source=google_cse_source,
        )
        _dedup_extend(serp_jobs)
    else:
        logger.info("Portal search: no SERP backend (DataForSEO or CSE), skipping SERP portals")

    pre_gate_count = len(all_jobs)

    # Stage 7.6 title-gate: applied after merge so per-portal logs above stay
    # comparable to historical numbers (raw fetched count) while persistence
    # downstream sees only title-matching rows. Mirrors the inline
    # ``_title_matches`` filter used by every ats_platforms.scan_* function.
    # Closes the documented Stage-0 gap where free portals' upstream ``q=``
    # is full-text and routed off-target rows into scoring (the apply-bias
    # surfaced by the 2026-05-23 option-D shakedown).
    if target_titles:
        from job_finder.web.ats_platforms import _title_matches

        excl = exclusions or []
        all_jobs = [j for j in all_jobs if _title_matches(j.title, target_titles, excl)]
        logger.info(
            "Portal search title-gate: %d → %d jobs (target_titles=%d, exclusions=%d)",
            pre_gate_count,
            len(all_jobs),
            len(target_titles),
            len(excl),
        )
    else:
        logger.info("Portal search total: %d jobs from all portals", pre_gate_count)

    return all_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_int(val) -> int | None:
    """Convert to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _feed_salary_from_text(s: str) -> dict:
    """Capture a free-text feed salary as a ``feed_string`` observation (D-1/D-2/D-3).

    Replaces the bespoke unbounded ``_parse_salary_string`` regex (plan §1.2
    item 1) with delegation to the single normalizer. Returns the salary-related
    ``Job`` kwargs (canonical pair only when the salvage ladder resolves it;
    sub-floor / period-less / cross-unit junk quarantines to a NULL pair with the
    observation retained). Spread into the Job: ``Job(..., **_feed_salary_from_text(s))``.
    """
    from job_finder.salary_normalizer import parse_salary_text, salary_capture_fields

    return salary_capture_fields(parse_salary_text(s, provenance="feed_string"))


def _feed_salary_from_values(min_value, max_value, *, raw_text: str | None = None) -> dict:
    """Capture a passthrough numeric feed salary as a ``feed_string`` observation.

    Direct-value feeds (RemoteOK/Himalayas/Jobicy/USAJobs/Adzuna) expose no
    period, so the observation records period 'unknown' and the normalizer's
    salvage ladder floors/salvages it (D-3): an in-window value is kept as annual,
    a sub-floor passthrough (e.g. RemoteOK ``46``) quarantines to NULL with the
    observation retained. Returns the salary-related ``Job`` kwargs (empty when
    the source asserted no salary at all).
    """
    from job_finder.salary_normalizer import SalaryObservation, salary_capture_fields

    lo = _safe_int(min_value)
    hi = _safe_int(max_value)
    if lo is None and hi is None:
        return {}
    obs = SalaryObservation(
        min_value=float(lo) if lo is not None else None,
        max_value=float(hi) if hi is not None else None,
        period="unknown",
        currency="USD",
        provenance="feed_string",
        raw_text=raw_text if raw_text is not None else f"feed_values:{min_value}-{max_value}",
    )
    return salary_capture_fields(obs)


def _truncate(text: str | None, max_len: int = 2000) -> str | None:
    """Truncate description to avoid storing massive HTML blobs.

    Default cap (2000) preserved for portals where the source already
    truncates and the body was used verbatim. Himalayas passes
    ``max_len=8000`` to match the ``jd_full`` eager-promote write width
    in ``job_finder/db/_jobs.py``.
    """
    if not text:
        return text
    return text[:max_len] if len(text) > max_len else text


def _clean_text(text: str | None) -> str:
    """Run ftfy on a free-text field; tolerant of None.

    Repairs upstream mojibake (UTF-8 bytes mis-decoded as cp1252 then
    re-encoded as UTF-8). Some portal sources (Himalayas, occasionally YC
    location fields for non-US offices) ship pre-mangled bytes; ftfy is
    the canonical Python remedy.
    """
    if not text:
        return ""
    return fix_text(text)


def _strip_html(text: str | None) -> str:
    """Strip HTML markup, returning plain text. Tolerant of None / non-strings.

    Used by Himalayas, whose ``description`` field is raw HTML
    (``<div>``, ``<p>``, ``<a>``, etc.). ftfy is applied after stripping
    so the cleaner sees normalized text rather than markup.
    """
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        # get_text with a space separator keeps word boundaries intact
        # across removed tags (e.g., "<b>Foo</b><i>Bar</i>" -> "Foo Bar"
        # not "FooBar").
        stripped = soup.get_text(separator=" ", strip=True)
    except Exception:
        # Defensive: if BS4 chokes on a pathological input, fall back
        # to a regex-based tag-strip rather than dropping the row.
        stripped = re.sub(r"<[^>]+>", " ", text)
        stripped = re.sub(r"\s+", " ", stripped).strip()
    return fix_text(stripped)


def _synthesize_yc_description(item: dict) -> str:
    """Build a structured description from a YC Inertia listing payload.

    YC's detail URL requires login, so we can't fetch the canonical JD
    body. The listing payload exposes useful metadata fields; we render
    them into a stable, scoreable description that crosses the 200-char
    ``jd_full`` eager-promote threshold in
    ``job_finder/db/_jobs.py:174-180``.

    The output is deliberately honest about being a metadata summary
    rather than a JD — the final line tells the LLM scorer (and any
    human reader) that the full JD lives behind a login wall.
    """
    title = _clean_text(item.get("title") or "")
    company = _clean_text(item.get("companyName") or "")
    one_liner = _clean_text(item.get("companyOneLiner") or "")
    role_type = _clean_text(item.get("roleType") or "")
    job_type = _clean_text(item.get("jobType") or "")
    location = _clean_text(item.get("location") or "")
    salary = _clean_text(item.get("salary") or "")
    batch = _clean_text(item.get("companyBatch") or "")

    parts: list[str] = []
    header = title
    if role_type:
        header += f" — {role_type} role"
    if company:
        header += f" at {company}"
    if batch:
        header += f" ({batch} YC company)"
    if header:
        header += "."
        parts.append(header)

    if one_liner:
        parts.append(f"Company overview: {one_liner}")

    facts: list[str] = []
    if location:
        facts.append(f"Location: {location}")
    if salary:
        facts.append(f"Compensation: {salary}")
    if job_type:
        facts.append(f"Employment type: {job_type}")
    if facts:
        parts.append("\n".join(facts))

    parts.append("(Posted via Work at a Startup; full job description requires YC login.)")
    return "\n\n".join(parts)


def _unix_to_datetime(value) -> datetime | None:
    """Convert a Unix-seconds value to a UTC datetime, returning None on bad input."""
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _detect_portal_from_url(url: str, portals: list[dict[str, str]]) -> str | None:
    """Match a URL to a portal by domain substring."""
    url_lower = url.lower()
    for portal in portals:
        if portal["domain"].lower() in url_lower:
            return portal["name"]
    return None
