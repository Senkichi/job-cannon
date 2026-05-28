"""Static HTML extraction for the careers crawler.

This module owns:
- The shared HTML extractor (`_extract_jobs_from_soup`) used by both the
  static fetch path and the Playwright tiers — it consumes a parsed
  BeautifulSoup tree and returns matched job dicts.
- The recursive JSON-LD walker (`_extract_jsonld_postings`).
- The static-tier entry point (`_try_static_extract`) that performs the
  pure-`requests` fetch, parses, and decides whether the result is
  conclusive (jobs found OR genuinely empty static page) or whether the
  caller should escalate to Playwright (page looks JS-heavy).

`_extract_jobs_from_soup` is part of the public surface — it's imported
lazily by `careers_page_interactions.py` and `ai_career_navigator.py` —
so the parent package re-exports it from `__init__.py`.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from job_finder.web._http_constants import _HEADERS, _TIMEOUT
from job_finder.web.ats_platforms import _title_matches
from job_finder.web.careers_crawler._title_filters import (
    _clean_title,
    _is_metadata_blob,
    _is_nav_path,
)

logger = logging.getLogger(__name__)

# Minimum text/html ratio to consider a page statically rendered.
# Below this, the page is likely JS-heavy and needs Playwright.
_STATIC_TEXT_RATIO = 0.02
_STATIC_MIN_TEXT_LEN = 500

# Tile-container element names that bound the context-title search when an
# <a> has empty inner text. Once we hit one of these we stop walking up.
_TILE_CONTAINER_TAGS = {"li", "article", "section"}
_MAX_CONTEXT_HOPS = 3


def _find_title_via_context(tag) -> str:
    """Find a title in the DOM context of an empty/short-text <a> tag.

    Oracle-style listings render each tile as an <a> wrapping no visible
    text — the title lives in a sibling/parent <h*> instead. Walk up at
    most ``_MAX_CONTEXT_HOPS`` ancestors (or stop early at a tile-like
    container), searching each ancestor's subtree for a heading.

    Returns the heading text, or "" when no usable title is found.
    """
    current = tag
    for _ in range(_MAX_CONTEXT_HOPS):
        parent = current.parent
        if parent is None or parent.name in (None, "body", "html", "[document]"):
            break
        heading = parent.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading is not None:
            text = heading.get_text(strip=True)
            if text and len(text) >= 4:
                return text
        if parent.name in _TILE_CONTAINER_TAGS:
            break
        current = parent
    return ""


def _extract_jobs_from_soup(
    soup: BeautifulSoup,
    base_url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Extract job listings from parsed HTML using JSON-LD and link matching.

    Returns list of dicts with 'title', 'url', 'description' keys.
    Description is always empty — the enrichment pipeline handles JD fetching.

    Args:
        soup: Parsed HTML.
        base_url: Base URL for resolving relative hrefs.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of matched job dicts. May be empty.
    """
    results = []
    seen_urls: set[str] = set()

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
            if not _title_matches(title, target_titles, exclusions):
                continue
            if url and url.startswith("/"):
                url = urljoin(base_url, url)
            if url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            results.append({"title": title, "url": url, "description": ""})

    # --- Pass 2: Link text matching ---
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        raw_text = tag.get_text(strip=True)
        # Oracle-style listings: the <a> wrapping a job tile has empty inner
        # text because the title lives in a sibling <h*>. Look up the DOM
        # before discarding. See FOLLOWUPS.md round-15 Gap #2.
        context_title = ""
        if not raw_text or len(raw_text) < 4:
            context_title = _find_title_via_context(tag)
            if not context_title:
                continue

        # Resolve URL
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)

        # Filter out navigation links (with /search subpath allowance — see
        # `_is_nav_path` docstring and FOLLOWUPS round-15 Gap #3).
        if _is_nav_path(parsed.path):
            continue

        # Deduplicate by URL
        if absolute_url in seen_urls:
            continue

        # Clean title and apply keyword filter. A context-resolved title
        # already comes from a heading element and skips _clean_title's
        # suffix-stripping (the heading text is clean by construction).
        title = context_title or _clean_title(tag, raw_text)
        # Reject obvious metadata blobs (titles that are actually concatenated
        # description+location+req-ID text from aggregator-style pages). These
        # would otherwise leak through with junk titles that pollute the UI
        # and waste scoring spend. See FOLLOWUPS.md 2026-05-27 audit.
        if _is_metadata_blob(title):
            continue
        if not _title_matches(title, target_titles, exclusions):
            continue

        seen_urls.add(absolute_url)
        results.append({"title": title, "url": absolute_url, "description": ""})

    return results


def _extract_jsonld_postings(data) -> list[dict]:
    """Recursively extract JobPosting entries from JSON-LD data.

    Handles single objects, arrays, ItemList wrappers, and @graph arrays.

    Args:
        data: Parsed JSON-LD data (dict or list).

    Returns:
        List of dicts with at least 'title' key.
    """
    postings = []
    if isinstance(data, list):
        for item in data:
            postings.extend(_extract_jsonld_postings(item))
    elif isinstance(data, dict):
        dtype = data.get("@type", "")
        if dtype == "JobPosting":
            postings.append(data)
        elif dtype == "ItemList":
            for item in data.get("itemListElement", []):
                postings.extend(_extract_jsonld_postings(item))
        elif "@graph" in data:
            postings.extend(_extract_jsonld_postings(data["@graph"]))
    return postings


def _try_static_extract(
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Try extracting jobs from static HTML (no JS rendering).

    Returns:
        list[dict] — extracted jobs (may be empty if page is static but has no matches)
        None — page appears JS-heavy, caller should try Playwright
    """
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Static fetch failed for '%s': %s", url, e)
        return None  # Can't tell if JS or down — let Playwright try

    html = resp.text
    text_len = len(resp.text.strip())
    if text_len == 0:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Check if page is JS-heavy (low text content relative to HTML size)
    plain_text = soup.get_text(strip=True)
    ratio = len(plain_text) / max(len(html), 1)

    # Extract jobs regardless — JSON-LD works even on JS-heavy pages
    # if the structured data is embedded in the initial HTML
    jobs = _extract_jobs_from_soup(soup, url, target_titles, exclusions)

    if jobs:
        # Found jobs statically — no need for Playwright
        return jobs

    # No jobs found. Determine if Playwright might help.
    if ratio < _STATIC_TEXT_RATIO or len(plain_text) < _STATIC_MIN_TEXT_LEN:
        # Page looks JS-heavy — signal Playwright
        return None

    # Page has plenty of static text but no matching jobs — genuinely empty
    return []
