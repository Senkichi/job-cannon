"""TalentBrew (by Radancy) platform scanner — static HTML extraction.

TalentBrew is a recruitment marketing platform that powers career sites for
major companies (Ford, J&J, HP, CVS Health, etc.). The platform does not expose
a public JSON API — jobs are rendered as server-side HTML with embedded
structured data (JSON-LD) where available.

The scanner:
1. Fetches HTML and extracts job listings via JSON-LD and link matching
2. Returns jobs with URL-derived data only (no Playwright fetches)
3. Full detail (jd_full) is backfilled later by enrichment for jobs that pass
   the title gate

The slug is the TalentBrew tenant's careers host (e.g. ``"careers.ford.com"``).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from job_finder.web.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from job_finder.web.ats_platforms._registry import _auth_block_statuses

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "TalentBrew"


def _extract_jsonld_postings(data) -> list[dict]:
    """Recursively extract JobPosting entries from JSON-LD data.

    Handles single objects, arrays, ItemList wrappers, and @graph arrays.

    Each returned dict has at least 'title' key. When ``jobLocation`` is
    present on the schema.org JobPosting, a 'location' key is added.

    Args:
        data: Parsed JSON-LD data (dict or list).

    Returns:
        List of dicts with at least 'title' key and optional 'location' key.
    """
    postings: list[dict] = []
    if isinstance(data, list):
        for item in data:
            postings.extend(_extract_jsonld_postings(item))
    elif isinstance(data, dict):
        dtype = data.get("@type", "")
        if dtype == "JobPosting":
            entry: dict = dict(data)
            # Extract location from jobLocation if present
            job_location = data.get("jobLocation")
            if job_location:
                if isinstance(job_location, str):
                    entry["location"] = job_location.strip()
                elif isinstance(job_location, dict):
                    address = job_location.get("address")
                    if isinstance(address, str):
                        entry["location"] = address.strip()
                    elif isinstance(address, dict):
                        locality = (address.get("addressLocality") or "").strip()
                        if locality:
                            entry["location"] = locality
            postings.append(entry)
        elif dtype == "ItemList":
            for item in data.get("itemListElement", []):
                postings.extend(_extract_jsonld_postings(item))
        elif "@graph" in data:
            postings.extend(_extract_jsonld_postings(data["@graph"]))
    return postings


def _extract_candidates(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Structural candidate extraction: JSON-LD + link passes WITHOUT title filtering.

    Returns every plausible job posting/link in DOM order — nav links,
    metadata blobs, and exact ``(url, title)`` duplicates excluded (those are
    structural junk), but titles NOT matched against the user's targets.
    """
    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    # --- Pass 1: JSON-LD structured data ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        postings = _extract_jsonld_postings(data)
        for posting in postings:
            title = posting.get("title", "")
            url = posting.get("url") or posting.get("sameAs") or ""
            if not title:
                continue
            if url and url.startswith("/"):
                url = urljoin(base_url, url)
            if url:
                if (url, title) in seen_pairs:
                    continue
                seen_pairs.add((url, title))
            entry: dict = {"title": title, "url": url, "description": ""}
            loc = posting.get("location", "")
            if loc:
                entry["location"] = loc
            candidates.append(entry)

    # --- Pass 2: Link text matching ---
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        raw_text = tag.get_text(strip=True)
        if not raw_text or len(raw_text) < 4:
            continue

        # Resolve URL
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)

        # Filter out navigation links
        if "/search" in parsed.path or "/category" in parsed.path:
            continue

        title = raw_text.strip()
        if (absolute_url, title) in seen_pairs:
            continue
        seen_pairs.add((absolute_url, title))
        link_entry: dict = {"title": title, "url": absolute_url, "description": ""}
        candidates.append(link_entry)

    return candidates


def fetch_postings(
    browser, slug: str, *, careers_url: str | None = None, max_load_more: int = 0
) -> list[dict]:
    """Fetch TalentBrew job postings via static HTML extraction.

    The scanner:
    1. Fetches HTML and extracts job listings via JSON-LD and link matching
    2. Returns jobs with URL-derived data only (no Playwright fetches)

    Full detail (jd_full) is backfilled later by enrichment for jobs that pass
    the title gate. This avoids expensive Playwright fetches for jobs that would
    be filtered out anyway.

    Args:
        browser: Playwright Browser instance (unused, kept for contract compatibility).
        slug: TalentBrew careers host (e.g. "careers.ford.com").
        careers_url: Optional full careers URL for locale discovery.
        max_load_more: Ignored (TalentBrew uses static extraction, not load-more clicks).

    Returns:
        Raw posting dicts with URL-derived titles. Empty on fetch error or no postings.
    """
    # Build the base URL from the slug
    base_url = f"https://{slug}" if not slug.startswith("http") else slug
    search_url = f"{base_url}/search-jobs" if not base_url.endswith("/search-jobs") else base_url

    # Use careers_url if provided (more specific than the search URL)
    if careers_url:
        search_url = careers_url

    try:
        resp = requests.get(search_url, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            logger.debug("fetch_talentbrew('%s'): HTTP %d", slug, resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = _extract_candidates(soup, search_url)

        # Convert to the posting format expected by the scanner
        postings = []
        for job in candidates:
            postings.append(
                {
                    "title": job.get("title", ""),
                    "source_url": job.get("url", ""),
                    "source_id": None,  # Will be derived from URL or enrichment
                    "location": job.get("location", ""),
                    "description": "",  # Will be backfilled by enrichment
                }
            )

        logger.debug(
            "fetch_talentbrew('%s'): extracted %d job postings from static HTML",
            slug,
            len(postings),
        )
        return postings

    except Exception as exc:
        logger.debug("fetch_talentbrew('%s') failed: %s", slug, exc)
        return []


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Map one raw TalentBrew posting to the canonical job dict."""
    return {
        "title": posting.get("title", ""),
        "company_source": _COMPANY_SOURCE,
        "location": posting.get("location") or "",
        "locations_structured": [],  # Parsed by location_parser on insert
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
    name="talentbrew",
    company_source=_COMPANY_SOURCE,
    fetch_postings=fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
