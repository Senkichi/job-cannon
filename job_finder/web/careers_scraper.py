"""HTML careers page scraper for companies without recognized ATS platforms.

Provides:
- find_careers_url: detect careers page URL from company homepage HTML
- scrape_careers_page: extract keyword-matched job listings from static HTML

Used by run_ats_scan (ats_scanner.py) as HTML fallback loop for miss companies.

Architecture:
- Static HTML only -- JS-rendered pages return empty list (expected limitation)
- Uses _title_matches from ats_scanner for keyword filtering (shared utility)
- Research Pitfall 6: After fetching, check r.url for ATS domain redirect before scraping

ATS URL redirect detection (Research Pitfall 6):
- If homepage redirects to jobs.lever.co, boards.greenhouse.io, or jobs.ashbyhq.com,
  return None and let caller extract slug from r.url instead of scraping HTML.
"""

import logging
import re
import sqlite3
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# _HEADERS, _TIMEOUT, _NOISE_TAGS imported from enrichment_tiers — these are
# genuinely shared infrastructure constants. Importing underscore-prefixed names
# from a sibling module is an intentional pragmatic choice for this single-codebase
# local app, consistent with the existing pattern in agentic_enricher.py.
# Auth-wall detection delegates to the canonical helpers in enrichment_tiers.
from job_finder.web.enrichment_tiers import (
    _HEADERS,
    _TIMEOUT,
    _NOISE_TAGS,
    _FULL_TEXT_AUTH_SIGNATURES,
    is_short_auth_page,
    is_chrome_or_login_page,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CAREERS_PATTERNS = ["/careers", "/jobs", "/join", "/join-us", "/work-with-us", "/openings"]

# Subdomains that indicate a careers/jobs page directly
_CAREERS_SUBDOMAINS = ("careers.", "jobs.", "work.")

# Meta-refresh content pattern: "0;url=https://..." or "0; URL=..."
_META_REFRESH_RE = re.compile(r"url\s*=\s*([^\s\"'>]+)", re.IGNORECASE)

_HAIKU_HTML_CHARS = 3000  # Truncate HTML sent to Haiku (~1000 tokens)

# _HEADERS, _TIMEOUT, _NOISE_TAGS are imported from enrichment_tiers above.
# Definitions removed to eliminate duplication (was copy-pasted across 3 modules).

_JD_DELAY = 1.0  # seconds between job page fetches (rate limiting)
_MAX_JD_CHARS = 8000  # cap extracted JD text

# _AUTH_WALL_SIGNATURES removed — auth detection now uses is_short_auth_page()
# and is_chrome_or_login_page() from enrichment_tiers (the canonical source).

# ATS domain patterns to detect redirects (Research Pitfall 6)
_ATS_DOMAINS = [
    "jobs.lever.co",
    "api.lever.co",
    "boards.greenhouse.io",
    "boards-api.greenhouse.io",
    "jobs.ashbyhq.com",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_careers_url_with_haiku(
    homepage_url: str,
    homepage_html: str,
    client: Any,
    conn: sqlite3.Connection,
    config: dict,
) -> str | None:
    """Use Haiku to identify careers page URL from homepage HTML.

    Only called when heuristic link-finding fails. Truncates HTML to
    _HAIKU_HTML_CHARS (~1000 tokens) to minimize cost.

    Args:
        homepage_url: The homepage URL (for resolving relative URLs).
        homepage_html: Raw HTML of the homepage.
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Absolute URL to the careers page, or None if not found.
    """
    from job_finder.web.model_provider import call_model

    truncated_html = homepage_html[:_HAIKU_HTML_CHARS]

    system = "You identify careers/jobs page URLs from company website HTML. Return ONLY the URL, or the word 'none' if no careers page is found. Do not explain."
    messages = [
        {
            "role": "user",
            "content": f"Given this company homepage HTML from {homepage_url}, identify the URL for their careers or jobs page.\n\nHTML:\n{truncated_html}",
        }
    ]

    try:
        result_obj = call_model(
            tier="haiku",
            system=system,
            messages=messages,
            conn=conn,
            config=config,
            output_schema=None,
            job_id=None,
            purpose="careers_scrape",
            max_tokens=256,
            client=client,
        )

        # call_model returns ModelResult — when no output_schema, result_obj.data has "text" key
        url_text = result_obj.data.get("text", "").strip()
        if not url_text or url_text.lower() == "none":
            return None

        # Resolve relative URL
        if url_text.startswith("/"):
            url_text = urljoin(homepage_url, url_text)

        # Basic validation: must start with http
        if url_text.startswith("http"):
            logger.debug("Haiku found careers URL for '%s': %s", homepage_url, url_text)
            return url_text

        return None
    except Exception as e:
        logger.debug("Haiku careers URL fallback failed for '%s': %s", homepage_url, e)
        return None


def _fetch_job_description(url: str) -> str:
    """Fetch a job page and extract cleaned description text.

    Strips noise HTML tags, checks for auth-wall signatures, and caps
    output at _MAX_JD_CHARS. Returns empty string on any failure.

    Args:
        url: Job page URL to fetch.

    Returns:
        Cleaned description text, or empty string on failure.
    """
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all(_NOISE_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Delegate auth-wall detection to canonical helpers from enrichment_tiers.
        # is_short_auth_page covers short login/CAPTCHA pages; is_chrome_or_login_page
        # covers cookie banners, LinkedIn walls, and wrong page types.
        # _FULL_TEXT_AUTH_SIGNATURES covers specific multi-word auth phrases
        # (e.g. "access denied") safe for full-text scan on any page length.
        if is_short_auth_page(text) or is_chrome_or_login_page(text):
            logger.debug("Auth-wall or chrome page detected for job page '%s'", url)
            return ""
        if any(sig in text.lower() for sig in _FULL_TEXT_AUTH_SIGNATURES):
            logger.debug("Auth-wall signature detected for job page '%s'", url)
            return ""
        return text[:_MAX_JD_CHARS] if text.strip() else ""
    except Exception as e:
        logger.debug("Failed to fetch job description from '%s': %s", url, e)
        return ""


def _extract_jobs_with_haiku(
    careers_url: str,
    careers_html: str,
    target_titles: list[str],
    exclusions: list[str],
    client: Any,
    conn: sqlite3.Connection,
    config: dict,
) -> list[dict]:
    """Extract job listings from unstructured careers page HTML using Haiku.

    Called when HTML link-parsing finds 0 results. Sends truncated HTML
    to Haiku for structured extraction.

    Args:
        careers_url: URL of the careers page (for resolving relative URLs).
        careers_html: Raw HTML of the careers page.
        target_titles: Target title keywords for post-extraction filtering.
        exclusions: Exclusion keywords for post-extraction filtering.
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        List of dicts with title, url, description keys. May be empty.
    """
    import json as _json
    from job_finder.web.model_provider import call_model

    truncated_html = careers_html[:_HAIKU_HTML_CHARS]

    system = "You extract job listings from careers page HTML. Return a JSON array of objects, each with 'title' (string), 'url' (string or null), and 'location' (string or null) fields. If no jobs are found, return an empty array []."
    messages = [
        {
            "role": "user",
            "content": f"Extract job listings from this careers page ({careers_url}):\n\n{truncated_html}",
        }
    ]

    try:
        result_obj = call_model(
            tier="haiku",
            system=system,
            messages=messages,
            conn=conn,
            config=config,
            output_schema=None,
            job_id=None,
            purpose="careers_scrape",
            max_tokens=1024,
            client=client,
        )

        # Parse Haiku response — expect JSON array
        text = result_obj.data.get("text", "").strip()
        # Handle markdown code blocks
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        jobs = _json.loads(text)
        if not isinstance(jobs, list):
            return []

        # Apply keyword filter and resolve URLs
        try:
            from job_finder.web.ats_platforms import _title_matches
        except ImportError:
            def _title_matches(title, target_titles, exclusions):
                title_lower = title.lower()
                if target_titles and not any(t.lower() in title_lower for t in target_titles):
                    return False
                if any(ex.lower() in title_lower for ex in exclusions):
                    return False
                return True

        filtered = []
        for job in jobs:
            title = job.get("title", "")
            if not title or not _title_matches(title, target_titles, exclusions):
                continue
            url = job.get("url") or ""
            if url.startswith("/"):
                url = urljoin(careers_url, url)
            filtered.append({
                "title": title,
                "url": url,
                "description": "",  # No JD fetch for Haiku-extracted jobs (too costly)
            })

        logger.debug(
            "_extract_jobs_with_haiku('%s'): %d jobs extracted, %d after filter",
            careers_url, len(jobs), len(filtered),
        )
        return filtered

    except Exception as e:
        logger.debug("Haiku job extraction failed for '%s': %s", careers_url, e)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_careers_url(
    homepage_url: str,
    client: Any = None,
    conn: Optional[sqlite3.Connection] = None,
    config: Optional[dict] = None,
) -> str | None:
    """Detect careers page URL from company homepage.

    Fetches homepage with requests.get and searches for links matching
    known careers URL patterns (/careers, /jobs, /join, etc.).

    IMPORTANT (Research Pitfall 6): Checks the final URL after redirect.
    If the homepage redirects to an ATS domain (Lever, Greenhouse, Ashby),
    returns None so caller can extract slug from the redirect URL instead.

    When heuristic link-finding returns nothing AND client/conn/config are
    provided, falls back to Haiku AI analysis of the truncated homepage HTML.

    Args:
        homepage_url: Company homepage URL to scan.
        client: Optional Anthropic client for Haiku fallback.
        conn: Optional SQLite connection for cost recording (Haiku fallback).
        config: Optional application config dict (Haiku fallback).

    Returns:
        Absolute URL to the careers page, or None if not found / ATS redirect.
    """
    try:
        resp = requests.get(homepage_url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("find_careers_url('%s') request failed: %s", homepage_url, e)
        return None

    # Research Pitfall 6: check final URL for ATS redirect
    final_url = resp.url
    parsed = urlparse(final_url)
    if any(ats_domain in parsed.netloc for ats_domain in _ATS_DOMAINS):
        logger.debug(
            "find_careers_url('%s'): redirected to ATS domain '%s' — returning None",
            homepage_url,
            parsed.netloc,
        )
        return None

    # If the final URL already lands on a careers/jobs subdomain (non-ATS),
    # return it directly — no need to scrape for a link.
    if any(parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS):
        logger.debug(
            "find_careers_url('%s'): final URL is careers subdomain '%s'",
            homepage_url, final_url,
        )
        return final_url

    # Parse homepage HTML for careers links
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("find_careers_url('%s') HTML parse error: %s", homepage_url, e)
        return None

    # Check <meta http-equiv="refresh" content="0; url=..."> redirects
    meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.IGNORECASE)})
    if meta_refresh:
        content = meta_refresh.get("content", "")
        m = _META_REFRESH_RE.search(str(content))
        if m:
            refresh_url = m.group(1).strip().strip("'\"")
            refresh_url = urljoin(homepage_url, refresh_url)
            refresh_parsed = urlparse(refresh_url)
            # Only follow if it's not an ATS redirect and looks like a careers destination
            if not any(ats_domain in refresh_parsed.netloc for ats_domain in _ATS_DOMAINS):
                if any(refresh_parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS) or \
                   any(pattern in refresh_parsed.path.lower() for pattern in _CAREERS_PATTERNS):
                    logger.debug(
                        "find_careers_url('%s'): meta-refresh to careers URL '%s'",
                        homepage_url, refresh_url,
                    )
                    return refresh_url

    # Search all <a href="..."> for careers-pattern matches
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue

        href_lower = href.lower()

        # Check for absolute links pointing to careers subdomains (non-ATS)
        if href_lower.startswith("http"):
            href_parsed = urlparse(href_lower)
            if any(href_parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS) and \
               not any(ats_domain in href_parsed.netloc for ats_domain in _ATS_DOMAINS):
                logger.debug(
                    "find_careers_url('%s'): found careers subdomain link '%s'",
                    homepage_url, href,
                )
                return href

        # Match path-based careers patterns
        for pattern in _CAREERS_PATTERNS:
            if href_lower == pattern or href_lower.startswith(pattern + "/") or href_lower.startswith(pattern + "?"):
                absolute_url = urljoin(homepage_url, href)
                logger.debug(
                    "find_careers_url('%s'): found careers link '%s'",
                    homepage_url, absolute_url,
                )
                return absolute_url

            if href_lower.startswith("http") and pattern in urlparse(href_lower).path:
                logger.debug(
                    "find_careers_url('%s'): found absolute careers link '%s'",
                    homepage_url, href,
                )
                return href

    logger.debug("find_careers_url('%s'): no careers link found", homepage_url)

    # Haiku fallback: if heuristic found nothing and client is available
    if client is not None and conn is not None and config is not None:
        logger.debug("find_careers_url('%s'): trying Haiku fallback", homepage_url)
        return _find_careers_url_with_haiku(homepage_url, resp.text, client, conn, config)

    return None


def scrape_careers_page(
    careers_url: str,
    target_titles: list[str],
    exclusions: list[str],
    client: Any = None,
    conn: Optional[sqlite3.Connection] = None,
    config: Optional[dict] = None,
) -> list[dict]:
    """Extract keyword-matched job listings from a static careers page.

    Fetches the careers page and looks for <a> tags whose text matches
    target_titles (using _title_matches from ats_scanner). This approach
    only works on static HTML pages — JavaScript-rendered pages will return
    an empty list (expected limitation documented in Research).

    For each matched job, follows the job URL to fetch the full job description
    text (rate-limited at _JD_DELAY seconds between fetches). Auth-wall pages
    return empty description. Descriptions capped at _MAX_JD_CHARS.

    When HTML parsing finds 0 matching jobs AND client/conn/config are provided,
    falls back to Haiku AI extraction via _extract_jobs_with_haiku.

    Args:
        careers_url: URL of the careers page to scrape.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.
        client: Optional Anthropic client for Haiku fallback.
        conn: Optional SQLite connection for cost recording (Haiku fallback).
        config: Optional application config dict (Haiku fallback).

    Returns:
        List of dicts with keys 'title', 'url', and 'description'. Empty list on
        error or if no matching jobs found (including JS-rendered pages).
    """
    try:
        resp = requests.get(careers_url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("scrape_careers_page('%s') request failed: %s", careers_url, e)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("scrape_careers_page('%s') HTML parse error: %s", careers_url, e)
        return []

    # Import shared keyword filter from ats_scanner (Plan 01 utility)
    try:
        from job_finder.web.ats_scanner import _title_matches
    except ImportError:
        # Fallback: simple case-insensitive match
        def _title_matches(title, target_titles, exclusions):
            title_lower = title.lower()
            if target_titles and not any(t.lower() in title_lower for t in target_titles):
                return False
            if any(ex.lower() in title_lower for ex in exclusions):
                return False
            return True

    results = []
    seen_urls = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        title = tag.get_text(strip=True)

        # Skip empty links, navigation-only links without text
        if not href or not title:
            continue

        # Apply keyword filter
        if not _title_matches(title, target_titles, exclusions):
            continue

        # Resolve relative URL
        absolute_url = urljoin(careers_url, href)

        # Deduplicate by URL
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        results.append({
            "title": title,
            "url": absolute_url,
        })

    logger.debug(
        "scrape_careers_page('%s'): %d matching jobs found",
        careers_url,
        len(results),
    )

    # Fetch full JD for each matched job (rate-limited)
    for i, job in enumerate(results):
        if job.get("url"):
            job["description"] = _fetch_job_description(job["url"])
            if i < len(results) - 1:  # No delay after last job
                time.sleep(_JD_DELAY)
        else:
            job["description"] = ""

    # Haiku fallback when HTML parsing found no matching jobs
    if not results and client is not None and conn is not None and config is not None:
        logger.debug("scrape_careers_page('%s'): trying Haiku fallback", careers_url)
        results = _extract_jobs_with_haiku(
            careers_url, resp.text, target_titles, exclusions, client, conn, config
        )

    return results
