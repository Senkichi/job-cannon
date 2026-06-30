"""Phenom platform scanner — sitemap-based (no public JSON API).

Phenom-hosted career portals do not expose a public unauthenticated JSON endpoint
for job listings. Job URLs are exposed through sitemaps at
``https://{host}/{locale}/sitemap_index.xml`` which reference sitemapN.xml files
containing individual job URLs in the pattern:
``https://{host}/{locale}/job/{job_id}/{title_slug}``

The scanner:
1. Fetches the sitemap index to discover sitemapN.xml files
2. Parses each sitemap to extract job URLs
3. Derives candidate titles from URL slugs (cheap)
4. Returns jobs with URL-derived data only (no Playwright fetches)

Full detail (jd_full) is backfilled later by enrichment for jobs that pass
the title gate. This avoids expensive Playwright fetches for jobs that would
be filtered out anyway. The slug is the Phenom tenant's careers host
(e.g. ``"careers.conduent.com"``).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from job_finder.web.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from job_finder.web.careers_crawler._title_filters import clean_title

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "Phenom"

# Phenom job URL pattern: /{locale}/job/{numeric_id}/{title_slug}
_JOB_URL_RE = re.compile(r"/[a-z]{2}/[a-z]{2}/job/(\d+)/", re.IGNORECASE)


def _sitemap_index_url(slug: str, careers_url: str | None = None) -> str:
    """Build the sitemap index URL for a Phenom tenant.

    ``slug`` is the careers host (e.g. ``"careers.conduent.com"``).
    ``careers_url`` is the full careers URL if known (e.g. ``"https://careers.bmo.com/ca/en"``).

    Strategy:
    1. If careers_url is provided, extract the locale path from it
    2. Otherwise, try common sitemap paths (us/en first, then others)
    3. Fall back to robots.txt discovery

    Returns the sitemap index URL.
    """
    import requests

    host = slug.strip().replace("https://", "").replace("http://", "").split("/")[0]
    base_url = f"https://{host}"

    # Strategy 1: Extract locale from careers_url if provided
    if careers_url:
        # Parse the URL to extract the locale path (e.g., /ca/en, /global/en, /en-us)
        from urllib.parse import urlparse

        parsed = urlparse(careers_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        # Check if the path looks like a locale (2-3 parts like ca/en, global/en, en-us)
        if len(path_parts) >= 2:
            # Try the locale from careers_url
            locale_path = "/".join(path_parts[:2])
            candidate = f"{base_url}/{locale_path}/sitemap_index.xml"
            try:
                resp = requests.head(candidate, timeout=5, allow_redirects=True)
                if resp.status_code == 200:
                    return candidate
            except Exception:
                pass

    # Strategy 2: Try common sitemap paths (us/en first for US companies)
    common_paths = [
        "/us/en/sitemap_index.xml",
        "/sitemap_index.xml",
        "/global/en/sitemap_index.xml",
        "/en-us/sitemap_index.xml",
        "/en/sitemap_index.xml",
    ]

    for path in common_paths:
        candidate = f"{base_url}{path}"
        try:
            resp = requests.head(candidate, timeout=5, allow_redirects=True)
            if resp.status_code == 200:
                return candidate
        except Exception:
            continue

    # Strategy 3: Try robots.txt for Sitemap directive
    try:
        robots_url = f"{base_url}/robots.txt"
        resp = requests.get(robots_url, timeout=5)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if "sitemap" in sitemap_url.lower():
                        return sitemap_url
    except Exception:
        pass

    # Fall back to default
    return f"{base_url}/us/en/sitemap_index.xml"


def _extract_sitemap_urls(html: str) -> list[str]:
    """Extract sitemap URLs from a sitemap index."""
    soup = BeautifulSoup(html, "xml")
    sitemaps = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)
        if url and "sitemap" in url.lower():
            sitemaps.append(url)
    return sitemaps


def _extract_job_urls(html: str) -> list[str]:
    """Extract job URLs from a sitemap."""
    soup = BeautifulSoup(html, "xml")
    job_urls = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)
        if url and "/job/" in url:
            job_urls.append(url)
    return job_urls


def _extract_job_id(url: str) -> str | None:
    """Extract job ID from a Phenom job URL."""
    match = _JOB_URL_RE.search(url)
    return match.group(1) if match else None


def _extract_title_from_url(url: str) -> str:
    """Extract a candidate title from the Phenom job URL slug.

    Phenom URLs follow the pattern: /{locale}/job/{job_id}/{title_slug}
    The slug contains a hyphen-separated title (e.g., "Human-Resources-Business-Partner").
    This is a cheap pre-filter before the expensive Playwright detail fetch.
    """
    # Extract the slug part after the job ID
    parts = url.rstrip("/").split("/")
    # URL pattern: /{locale}/job/{job_id}/{title_slug}
    # We need at least 4 parts: ['', locale, 'job', job_id, title_slug]
    if len(parts) >= 5:
        slug = parts[-1]
        # If the slug is just a number (job ID with no title slug), return empty
        if slug.isdigit():
            return ""
        # Convert hyphens to spaces and capitalize words
        title = " ".join(word.capitalize() for word in slug.split("-"))
        return title
    return ""


def fetch_postings(
    browser, slug: str, *, careers_url: str | None = None, max_load_more: int = 0
) -> list[dict]:
    """Fetch Phenom job postings via sitemap (no Playwright detail fetches).

    The scanner:
    1. Fetches all sitemaps (no cap) via cheap requests
    2. Extracts all job URLs from all sitemaps (no cap)
    3. Derives candidate titles from URL slugs (cheap)
    4. Returns jobs with URL-derived data only (no Playwright fetches)

    Full detail (jd_full) is backfilled later by enrichment for jobs that pass
    the title gate. This avoids expensive Playwright fetches for jobs that would
    be filtered out anyway.

    Args:
        browser: Playwright Browser instance (unused, kept for contract compatibility).
        slug: Phenom careers host (e.g. "careers.conduent.com").
        careers_url: Optional full careers URL for locale discovery.
        max_load_more: Ignored (Phenom uses sitemap pagination, not load-more clicks).

    Returns:
        Raw posting dicts with URL-derived titles. Empty on fetch error or no postings.
    """
    import requests

    try:
        # Step 1: Fetch sitemap index via requests (faster than Playwright)
        sitemap_index_url = _sitemap_index_url(slug, careers_url)
        try:
            resp = requests.get(sitemap_index_url, timeout=10)
            if resp.status_code != 200:
                logger.debug(
                    "scan_phenom('%s'): sitemap index returned HTTP %d", slug, resp.status_code
                )
                return []
            sitemap_urls = _extract_sitemap_urls(resp.text)
        except Exception as exc:
            logger.debug("scan_phenom('%s'): sitemap index fetch failed: %s", slug, exc)
            return []

        if not sitemap_urls:
            logger.debug("scan_phenom('%s'): no sitemaps found", slug)
            return []

        # Step 2: Extract job URLs from ALL sitemaps (no cap)
        job_urls = []
        for sitemap_url in sitemap_urls:
            try:
                resp = requests.get(sitemap_url, timeout=10)
                if resp.status_code == 200:
                    urls = _extract_job_urls(resp.text)
                    job_urls.extend(urls)
            except Exception as exc:
                logger.debug(
                    "scan_phenom('%s'): sitemap fetch failed for %s: %s", slug, sitemap_url, exc
                )
                continue

        if not job_urls:
            logger.debug("scan_phenom('%s'): no job URLs found in sitemaps", slug)
            return []

        # Step 3: Build postings from URL-derived data (no Playwright fetches)
        # The title gate is applied by the orchestrator, so we return all jobs.
        # Enrichment will backfill jd_full for jobs that pass the title gate.
        postings = []
        for job_url in job_urls:
            job_id = _extract_job_id(job_url)
            if not job_id:
                continue

            # Derive title from URL slug (cheap pre-filter)
            title = _extract_title_from_url(job_url)

            # Apply title isolation to remove location/date glued to title (PR #539)
            if title:
                title = clean_title(title)

            if not title:
                continue

            postings.append(
                {
                    "title": title,
                    "source_url": job_url,
                    "source_id": job_id,
                    "location": "",  # Not available from URL slug
                    "description": "",  # Will be backfilled by enrichment
                }
            )

        logger.debug(
            "scan_phenom('%s'): extracted %d job URLs from sitemaps",
            slug,
            len(postings),
        )
        return postings
    except Exception as exc:
        logger.debug("scan_phenom('%s') failed: %s", slug, exc)
        return []


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Map one raw Phenom posting to the canonical job dict."""
    return {
        "title": posting.get("title", ""),
        "company_source": _COMPANY_SOURCE,
        "location": posting.get("location") or "",
        "locations_structured": [],
        "description": posting.get("description", ""),
        "jd_full": posting.get("description", ""),
        "source_url": posting.get("source_url") or "",
        "source_id": posting.get("source_id") or None,
        "posted_date": None,
        "posted_date_precision": None,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlaywrightPlatformScanner(
    name="phenom",
    company_source=_COMPANY_SOURCE,
    fetch_postings=fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
