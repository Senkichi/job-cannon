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

import requests

from job_finder.models import Job

logger = logging.getLogger(__name__)

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
                title=title,
                company=company,
                location=item.get("location") or "Remote",
                source="portal_remoteok",
                source_url=item.get("apply_url") or item.get("url") or "",
                salary_min=_safe_int(item.get("salary_min")),
                salary_max=_safe_int(item.get("salary_max")),
                description=_truncate(item.get("description")),
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

        salary_min, salary_max = _parse_salary_string(item.get("salary") or "")

        jobs.append(
            Job(
                title=title,
                company=company,
                location=item.get("candidate_required_location") or "Remote",
                source="portal_remotive",
                source_url=item.get("url") or "",
                salary_min=salary_min,
                salary_max=salary_max,
                description=_truncate(item.get("description")),
            )
        )

    logger.info("Remotive: %d jobs matched from %d listings", len(jobs), len(listings))
    return jobs


def _fetch_himalayas(keywords: list[str]) -> list[Job]:
    """Fetch from Himalayas free JSON API. No auth required.

    Supports server-side search via query param, so we make one request
    per keyword to avoid downloading the entire 100K+ listing catalog.
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
            title = item.get("title") or ""
            company = item.get("companyName") or ""
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
                    location=item.get("location") or "Remote",
                    source="portal_himalayas",
                    source_url=url,
                    salary_min=_safe_int(item.get("minSalary")),
                    salary_max=_safe_int(item.get("maxSalary")),
                    description=_truncate(item.get("description")),
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
        title = item.get("jobTitle") or ""
        company = item.get("companyName") or ""
        if not title or not company:
            continue

        text = f"{title} {item.get('jobExcerpt', '')} {item.get('jobDescription', '')}".lower()
        if keywords_lower and not any(kw in text for kw in keywords_lower):
            continue

        jobs.append(
            Job(
                title=title,
                company=company,
                location=item.get("jobGeo") or "Remote",
                source="portal_jobicy",
                source_url=item.get("url") or "",
                salary_min=_safe_int(item.get("annualSalaryMin")),
                salary_max=_safe_int(item.get("annualSalaryMax")),
                description=_truncate(item.get("jobDescription") or item.get("jobExcerpt")),
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

            title = item.get("title") or ""
            company = item.get("companyName") or ""
            if not title or not company:
                continue

            slug = item.get("companySlug") or ""
            source_url = f"https://www.workatastartup.com/companies/{slug}/jobs/{job_id}" if slug and job_id else ""

            salary_min, salary_max = _parse_salary_string(item.get("salary") or "")

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=item.get("location") or "Remote",
                    source="portal_yc_workatastartup",
                    source_url=source_url,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    description=_truncate(item.get("companyOneLiner")),
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
            salary_min = salary_max = None
            if remuneration:
                first = remuneration[0]
                salary_min = _safe_int(first.get("MinimumRange"))
                salary_max = _safe_int(first.get("MaximumRange"))

            user_area = descriptor.get("UserArea", {}) or {}
            details = user_area.get("Details", {}) or {}
            description = details.get("JobSummary") or descriptor.get("QualificationSummary") or ""

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=location_str or "United States",
                    source="portal_usajobs",
                    source_url=descriptor.get("PositionURI") or "",
                    salary_min=salary_min,
                    salary_max=salary_max,
                    description=_truncate(description),
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
            company = company_node.get("display_name") if isinstance(company_node, dict) else (company_node or "")
            if not title or not company:
                continue

            key = (company.lower(), title.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)

            location_node = item.get("location") or {}
            location_str = (
                location_node.get("display_name") if isinstance(location_node, dict) else (location_node or "")
            ) or "Remote"

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=location_str,
                    source="portal_adzuna",
                    source_url=item.get("redirect_url") or "",
                    salary_min=_safe_int(item.get("salary_min")),
                    salary_max=_safe_int(item.get("salary_max")),
                    description=_truncate(item.get("description")),
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

            salary_min, salary_max = _parse_salary_string(item.get("salary") or "")

            all_jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=item.get("location") or "Remote",
                    source="portal_jooble",
                    source_url=item.get("link") or "",
                    salary_min=salary_min,
                    salary_max=salary_max,
                    description=_truncate(item.get("snippet")),
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
    dataforseo_source: object,
    portals: list[dict[str, str]] | None = None,
    max_queries: int = 30,
) -> list[Job]:
    """Run site: queries through DataForSEO for portals without free APIs.

    Batches all queries into a single DataForSEO task submission for efficiency.
    DataForSEO charges ~$0.0006 per 10 results — far cheaper than SerpAPI.

    Args:
        keywords: Search terms.
        dataforseo_source: DataForSEOSource instance.
        portals: Portal list (defaults to SERP_PORTALS).
        max_queries: Cap on total SERP queries to prevent runaway costs.

    Returns:
        Deduplicated list of Job objects.
    """
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

    logger.info(
        "Portal SERP search: submitting %d queries to DataForSEO",
        len(queries),
    )

    try:
        raw_jobs = dataforseo_source.fetch_jobs(queries)
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
                source_id=job.source_id,
                salary_min=job.salary_min,
                salary_max=job.salary_max,
                description=job.description,
                posted_date=job.posted_date,
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
    dataforseo_source: object | None = None,
    max_serp_queries: int = 30,
    serp_portals: list[dict[str, str]] | None = None,
    portal_config: dict | None = None,
) -> list[Job]:
    """Fetch from all portals: free APIs first, then SERP fallback.

    Args:
        keywords: Search terms.
        dataforseo_source: Optional DataForSEOSource for SERP portals.
            If None, only free API portals are searched.
        max_serp_queries: Cap on DataForSEO queries.
        serp_portals: Optional override for SERP portal list.
        portal_config: Optional ``sources.portal_search`` config dict. Used
            to enable/configure the Stage-2 portals (jobicy, yc_workatastartup,
            usajobs, adzuna, jooble). When omitted, those portals are skipped
            and only the three always-on free portals plus SERP run.

    Returns:
        Combined, deduplicated job list.
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

    # Tier 2: SERP portals via DataForSEO (cheap, batched)
    if dataforseo_source is not None:
        serp_jobs = fetch_serp_portals(
            keywords,
            dataforseo_source,
            portals=serp_portals,
            max_queries=max_serp_queries,
        )
        _dedup_extend(serp_jobs)
    else:
        logger.info("Portal search: no DataForSEO backend, skipping SERP portals")

    logger.info("Portal search total: %d jobs from all portals", len(all_jobs))
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


_SALARY_RE = re.compile(r"\$?(\d[\d,]*)\s*[Kk]?\s*[-–—]\s*\$?(\d[\d,]*)\s*[Kk]?")


def _parse_salary_string(s: str) -> tuple[int | None, int | None]:
    """Extract (min, max) from salary strings like '$150K - $200K'."""
    m = _SALARY_RE.search(s)
    if not m:
        return None, None
    low = int(m.group(1).replace(",", ""))
    high = int(m.group(2).replace(",", ""))
    if low < 1000:
        low *= 1000
    if high < 1000:
        high *= 1000
    return low, high


def _truncate(text: str | None, max_len: int = 2000) -> str | None:
    """Truncate description to avoid storing massive HTML blobs."""
    if not text:
        return text
    return text[:max_len] if len(text) > max_len else text


def _detect_portal_from_url(url: str, portals: list[dict[str, str]]) -> str | None:
    """Match a URL to a portal by domain substring."""
    url_lower = url.lower()
    for portal in portals:
        if portal["domain"].lower() in url_lower:
            return portal["name"]
    return None
