"""Phenom platform scanner — Playwright-based (sitemap source, no public JSON API).

Phenom-hosted career portals do not expose a public unauthenticated JSON endpoint
for job listings. Job URLs are exposed through sitemaps at
``https://{host}/{locale}/sitemap_index.xml`` which reference sitemapN.xml files
containing individual job URLs in the pattern:
``https://{host}/{locale}/job/{job_id}/{title_slug}``

The scanner:
1. Fetches the sitemap index to discover sitemapN.xml files
2. Parses each sitemap to extract job URLs
3. Fetches individual job detail pages via Playwright
4. Extracts job data from the rendered HTML

Descriptions are pulled from job detail pages. The slug is the Phenom
tenant's careers host (e.g. ``"careers.conduent.com"``).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from job_finder.web.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from job_finder.web.careers_crawler._title_filters import clean_title

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "Phenom"

# Page-render timing, mirrored from careers_crawler/_playwright_tier.py
_PLAYWRIGHT_TIMEOUT_MS = 15000
_JS_SETTLE_MS = 2000

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


def _extract_posting_from_html(html: str, url: str) -> dict | None:
    """Extract job data from a Phenom job detail page."""
    soup = BeautifulSoup(html, "html.parser")

    # Priority 1: Extract title from JSON-LD JobPosting object (structural source)
    title = ""
    json_ld_scripts = soup.find_all("script", type="application/ld+json")
    for script in json_ld_scripts:
        try:
            import json

            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                title = data.get("title", "")
                if title:
                    break
        except Exception:
            continue

    # Priority 2: Fallback to meta tags (og:title)
    if not title:
        meta_title = soup.find("meta", property="og:title")
        if meta_title:
            title = meta_title.get("content", "").split("|")[0].strip()

    # Priority 3: Fallback to h1
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # Apply title isolation to remove location/date glued to title (PR #539)
    if title:
        title = clean_title(title)

    # Extract location from meta description or keywords
    location = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc:
        desc = meta_desc.get("content", "")
        # Location is often after "in " in the description
        if " in " in desc:
            location = desc.split(" in ")[-1].split(" at ")[0].strip()

    if not location:
        meta_keywords = soup.find("meta", attrs={"name": "keywords"})
        if meta_keywords:
            keywords = meta_keywords.get("content", "")
            # Location is often the last comma-separated value
            parts = [k.strip() for k in keywords.split(",")]
            if len(parts) > 1:
                location = parts[-1]

    # Extract description
    description = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc:
        description = meta_desc.get("content", "")

    job_id = _extract_job_id(url)

    if not title or not job_id:
        return None

    return {
        "title": title,
        "source_url": url,
        "source_id": job_id,
        "location": location,
        "description": description,
    }


def fetch_postings(
    browser, slug: str, *, careers_url: str | None = None, max_load_more: int = 0
) -> list[dict]:
    """Fetch Phenom job postings via sitemap + Playwright job detail pages.

    Args:
        browser: Playwright Browser instance.
        slug: Phenom careers host (e.g. "careers.conduent.com").
        careers_url: Optional full careers URL for locale discovery.

    Returns:
        Raw posting dicts. Empty on fetch error or no postings.
    """
    import requests

    page = None
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

        # Step 2: Extract job URLs from all sitemaps
        job_urls = []
        for sitemap_url in sitemap_urls[:2]:  # Limit to first 2 sitemaps for speed
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

        # Step 3: Fetch job detail pages via Playwright (for full descriptions)
        page = browser.new_page()
        postings = []

        # Limit to first 5 jobs to avoid excessive fetch time during testing
        for job_url in job_urls[:5]:
            try:
                page.goto(job_url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(_JS_SETTLE_MS)

                posting = _extract_posting_from_html(page.content(), job_url)
                if posting:
                    postings.append(posting)
            except Exception as exc:
                logger.debug(
                    "scan_phenom('%s'): job detail fetch failed for %s: %s", slug, job_url, exc
                )
                continue

        return postings
    except Exception as exc:
        logger.debug("scan_phenom('%s') failed: %s", slug, exc)
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


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
