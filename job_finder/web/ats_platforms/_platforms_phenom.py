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
from collections.abc import Callable
from dataclasses import dataclass

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "Phenom"

# Page-render timing, mirrored from careers_crawler/_playwright_tier.py
_PLAYWRIGHT_TIMEOUT_MS = 15000
_JS_SETTLE_MS = 2000

# Phenom job URL pattern: /{locale}/job/{numeric_id}/{title_slug}
_JOB_URL_RE = re.compile(r"/[a-z]{2}/[a-z]{2}/job/(\d+)/", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PlaywrightPlatformScanner:
    """Per-platform contract for the Playwright-class scan driver.

    Parallel architecture to ``_registry.PlatformScanner`` — the distinction
    is ``fetch_postings`` takes the Playwright ``Browser`` as an explicit
    first parameter (no requests-only ``slug -> list`` contract). The driver
    (``ats_scanner/_run_playwright.run_playwright_platform_scan``) owns the
    title-match gate and the result-count log line; the orchestrator owns the
    ``sync_playwright()`` lifecycle.

    Attributes:
        name: Lowercase platform key matching ``companies.ats_platform``
            (``"phenom"``). Used in log messages.
        company_source: Display-cased platform name written into the
            ``company_source`` field of each job dict (``"Phenom"``).
        fetch_postings: ``(browser, slug) -> list[dict]``.
            Owns the sitemap fetch + job detail page render + DOM extraction.
            Must catch its own exceptions and return ``[]`` on any error so
            one tenant's render failure cannot crash a whole batch.
        title_of: ``posting -> str``. Pulls the title out of one raw posting
            for the title-match gate.
        posting_to_job: ``(posting, slug) -> dict | None``. Builds the
            canonical job dict for one posting; ``None`` skips it.
    """

    name: str
    company_source: str
    fetch_postings: Callable[..., list[dict]]
    title_of: Callable[[dict], str]
    posting_to_job: Callable[[dict, str], dict | None]


def _sitemap_index_url(slug: str) -> str:
    """Build the sitemap index URL for a Phenom tenant.

    ``slug`` is the careers host (e.g. ``"careers.conduent.com"``).
    Returns the sitemap index URL (defaults to US English locale).
    """
    host = slug.strip().replace("https://", "").replace("http://", "").split("/")[0]
    return f"https://{host}/us/en/sitemap_index.xml"


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
    
    # Try to find title in meta tags first
    title = ""
    meta_title = soup.find("meta", property="og:title")
    if meta_title:
        title = meta_title.get("content", "").split("|")[0].strip()
    
    if not title:
        # Fallback to h1
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    
    # Apply title isolation to remove location/date glued to title (PR #539)
    # Phenom often glues location to the title in meta tags
    if " in " in title:
        title = title.split(" in ")[0].strip()
    if "," in title:
        # Remove trailing location-like comma-separated parts
        parts = [p.strip() for p in title.split(",")]
        # Keep only the first part if it looks like a title (not a location)
        if len(parts) > 1 and any(loc.lower() in parts[-1].lower() for loc in ["united states", "india", "uk", "usa", "remote"]):
            title = parts[0]
    
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


def _fetch_postings(browser, slug: str) -> list[dict]:
    """Fetch Phenom job postings via sitemap + Playwright job detail pages.

    Args:
        browser: Playwright Browser instance.
        slug: Phenom careers host (e.g. "careers.conduent.com").

    Returns:
        Raw posting dicts. Empty on fetch error or no postings.
    """
    import requests
    
    page = None
    try:
        # Step 1: Fetch sitemap index via requests (faster than Playwright)
        sitemap_index_url = _sitemap_index_url(slug)
        try:
            resp = requests.get(sitemap_index_url, timeout=10)
            if resp.status_code != 200:
                logger.debug("scan_phenom('%s'): sitemap index returned HTTP %d", slug, resp.status_code)
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
        for sitemap_url in sitemap_urls[:10]:  # Limit to first 10 sitemaps
            try:
                resp = requests.get(sitemap_url, timeout=10)
                if resp.status_code == 200:
                    urls = _extract_job_urls(resp.text)
                    job_urls.extend(urls)
            except Exception as exc:
                logger.debug("scan_phenom('%s'): sitemap fetch failed for %s: %s", slug, sitemap_url, exc)
                continue
        
        if not job_urls:
            logger.debug("scan_phenom('%s'): no job URLs found in sitemaps", slug)
            return []
        
        # Step 3: Fetch job detail pages via Playwright (for full descriptions)
        page = browser.new_page()
        postings = []
        
        # Limit to first 50 jobs to avoid excessive fetch time
        for job_url in job_urls[:50]:
            try:
                page.goto(job_url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(_JS_SETTLE_MS)
                
                posting = _extract_posting_from_html(page.content(), job_url)
                if posting:
                    postings.append(posting)
            except Exception as exc:
                logger.debug("scan_phenom('%s'): job detail fetch failed for %s: %s", slug, job_url, exc)
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
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
