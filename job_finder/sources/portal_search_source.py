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
) -> list[Job]:
    """Fetch from all portals: free APIs first, then SERP fallback.

    Args:
        keywords: Search terms.
        dataforseo_source: Optional DataForSEOSource for SERP portals.
            If None, only free API portals are searched.
        max_serp_queries: Cap on DataForSEO queries.
        serp_portals: Optional override for SERP portal list.

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

    # Tier 1: Free API portals (zero cost)
    _dedup_extend(_fetch_remoteok(keywords))
    _dedup_extend(_fetch_remotive(keywords))
    _dedup_extend(_fetch_himalayas(keywords))

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
