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
import sqlite3
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from job_finder.config import DEFAULT_MODEL_LOW
from job_finder.web.claude_client import call_claude
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model

logger = logging.getLogger(__name__)


# Satisfies _make_adapter's api_key guard without pulling in the Anthropic
# SDK. AnthropicProvider forwards this to call_claude(), which ignores
# client and routes through the CLI — OAuth/subscription billing is preserved.
class _CLIClientStub:
    api_key = "cli-managed"


_CLI_CLIENT_STUB = _CLIClientStub()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CAREERS_PATTERNS = ["/careers", "/jobs", "/join", "/join-us", "/work-with-us", "/openings"]

_LOW_TIER_HTML_CHARS = 3000  # Truncate HTML sent to low tier (~1000 tokens)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0)"}

_TIMEOUT = 10

_JD_DELAY = 1.0  # seconds between job page fetches (rate limiting)
_MAX_JD_CHARS = 8000  # cap extracted JD text

_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "aside"]

_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# ATS domain patterns to detect redirects (Research Pitfall 6)
_ATS_DOMAINS = [
    "jobs.lever.co",
    "api.lever.co",
    "boards.greenhouse.io",
    "boards-api.greenhouse.io",
    "jobs.ashbyhq.com",
]

# Subdomains that indicate a careers site (checked after ATS exclusion)
_CAREERS_SUBDOMAINS = ("careers.", "jobs.", "work.", "apply.")

# Structured output schemas for the two Haiku call sites below. Both providers
# (Anthropic CLI + Ollama) return the same dict shape when a schema is
# supplied — without it, Ollama's forced "format":"json" yields arbitrary keys
# while the CLI wraps freeform text in {"text": ...}, which would silently
# produce empty results once a cascade routes the call through Ollama.
_CAREERS_URL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Absolute URL to the careers/jobs page, or the word 'none' if not found",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_CAREERS_JOBS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


def _extract_base_domain(url: str) -> str | None:
    """Extract registrable domain from URL, stripping www. prefix.

    Returns e.g. 'google.com' from 'https://www.google.com/'.
    """
    netloc = urlparse(url).netloc
    if not netloc:
        return None
    return netloc.removeprefix("www.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_careers_url_with_low_tier(
    homepage_url: str,
    homepage_html: str,
    conn: sqlite3.Connection,
    config: dict,
) -> str | None:
    """Use low-tier model to identify careers page URL from homepage HTML.

    Only called when heuristic link-finding fails. Truncates HTML to
    _LOW_TIER_HTML_CHARS (~1000 tokens) to minimize cost.

    Args:
        homepage_url: The homepage URL (for resolving relative URLs).
        homepage_html: Raw HTML of the homepage.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Absolute URL to the careers page, or None if not found.
    """
    truncated_html = homepage_html[:_LOW_TIER_HTML_CHARS]

    system = (
        "You identify careers/jobs page URLs from company website HTML. "
        "Return the absolute URL in the 'url' field, or the string 'none' in "
        "the 'url' field when no careers page is found."
    )
    messages = [
        {
            "role": "user",
            "content": f"Given this company homepage HTML from {homepage_url}, identify the URL for their careers or jobs page.\n\nHTML:\n{truncated_html}",
        }
    ]

    use_dispatcher = bool(config.get("providers", {}).get("low"))

    try:
        if use_dispatcher:
            try:
                model_result = call_model(
                    tier="low",
                    system=system,
                    messages=messages,
                    conn=conn,
                    config=config,
                    output_schema=_CAREERS_URL_SCHEMA,
                    job_id=None,
                    purpose="find_careers_url",
                    max_tokens=256,
                    client=_CLI_CLIENT_STUB,
                )
                result = model_result.data
            except ProviderCascadeExhaustedError:
                logger.warning(
                    "careers_scrape: cascade exhausted for URL discovery of '%s', retrying via CLI",
                    homepage_url,
                )
                result, _cost, _schema_valid = call_claude(
                    model=DEFAULT_MODEL_LOW,
                    system=system,
                    messages=messages,
                    output_schema=_CAREERS_URL_SCHEMA,
                    conn=conn,
                    job_id=None,
                    purpose="find_careers_url",
                    config=config,
                    max_tokens=256,
                )
        else:
            result, _cost, _schema_valid = call_claude(
                model=DEFAULT_MODEL_LOW,
                system=system,
                messages=messages,
                output_schema=_CAREERS_URL_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="find_careers_url",
                config=config,
                max_tokens=256,
            )
    except Exception as e:
        logger.debug("Low-tier careers URL fallback failed for '%s': %s", homepage_url, e)
        return None

    url_text = (result.get("url", "") if isinstance(result, dict) else "").strip()
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
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for job page '%s'", url)
            return ""
        return text[:_MAX_JD_CHARS] if text.strip() else ""
    except Exception as e:
        logger.debug("Failed to fetch job description from '%s': %s", url, e)
        return ""


def _extract_jobs_with_low_tier(
    careers_url: str,
    careers_html: str,
    target_titles: list[str],
    exclusions: list[str],
    conn: sqlite3.Connection,
    config: dict,
) -> list[dict]:
    """Extract job listings from unstructured careers page HTML using low-tier model.

    Called when HTML link-parsing finds 0 results. Sends truncated HTML
    to low tier for structured extraction.

    Args:
        careers_url: URL of the careers page (for resolving relative URLs).
        careers_html: Raw HTML of the careers page.
        target_titles: Target title keywords for post-extraction filtering.
        exclusions: Exclusion keywords for post-extraction filtering.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        List of dicts with title, url, description keys. May be empty.
    """
    truncated_html = careers_html[:_LOW_TIER_HTML_CHARS]

    system = (
        "You extract job listings from careers page HTML. Populate the 'jobs' "
        "array with objects containing 'title' (string, required), 'url' "
        "(string, optional), and 'location' (string, optional). If no jobs are "
        "found, return an empty 'jobs' array."
    )
    messages = [
        {
            "role": "user",
            "content": f"Extract job listings from this careers page ({careers_url}):\n\n{truncated_html}",
        }
    ]

    use_dispatcher = bool(config.get("providers", {}).get("low"))

    try:
        if use_dispatcher:
            try:
                model_result = call_model(
                    tier="low",
                    system=system,
                    messages=messages,
                    conn=conn,
                    config=config,
                    output_schema=_CAREERS_JOBS_SCHEMA,
                    job_id=None,
                    purpose="extract_jobs",
                    max_tokens=1024,
                    client=_CLI_CLIENT_STUB,
                )
                result = model_result.data
            except ProviderCascadeExhaustedError:
                logger.warning(
                    "careers_scrape: cascade exhausted for job extraction at '%s', retrying via CLI",
                    careers_url,
                )
                result, _cost, _schema_valid = call_claude(
                    model=DEFAULT_MODEL_LOW,
                    system=system,
                    messages=messages,
                    output_schema=_CAREERS_JOBS_SCHEMA,
                    conn=conn,
                    job_id=None,
                    purpose="extract_jobs",
                    config=config,
                    max_tokens=1024,
                )
        else:
            result, _cost, _schema_valid = call_claude(
                model=DEFAULT_MODEL_LOW,
                system=system,
                messages=messages,
                output_schema=_CAREERS_JOBS_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="extract_jobs",
                config=config,
                max_tokens=1024,
            )

        jobs = result.get("jobs", []) if isinstance(result, dict) else []
        if not isinstance(jobs, list):
            return []

        # Apply keyword filter and resolve URLs
        try:
            from job_finder.web.ats_scanner import _title_matches
        except ImportError:

            def _title_matches(title, target_titles, exclusions):
                title_lower = title.lower()
                if target_titles and not any(t.lower() in title_lower for t in target_titles):
                    return False
                return not any(ex.lower() in title_lower for ex in exclusions)

        filtered = []
        for job in jobs:
            title = job.get("title", "")
            if not title or not _title_matches(title, target_titles, exclusions):
                continue
            url = job.get("url") or ""
            if url.startswith("/"):
                url = urljoin(careers_url, url)
            filtered.append(
                {
                    "title": title,
                    "url": url,
                    "description": "",  # No JD fetch for low-tier-extracted jobs (too costly)
                }
            )

        logger.debug(
            "_extract_jobs_with_low_tier('%s'): %d jobs extracted, %d after filter",
            careers_url,
            len(jobs),
            len(filtered),
        )
        return filtered

    except Exception as e:
        logger.debug("Low-tier job extraction failed for '%s': %s", careers_url, e)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_careers_url(
    homepage_url: str,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
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

    # Check if HTTP redirect landed on a careers/jobs/work subdomain
    if any(parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS):
        logger.debug(
            "find_careers_url('%s'): redirected to careers subdomain '%s'",
            homepage_url,
            final_url,
        )
        return final_url

    # Parse homepage HTML for careers links
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("find_careers_url('%s') HTML parse error: %s", homepage_url, e)
        return None

    # Detect <meta http-equiv="refresh"> redirects to careers URLs
    import re

    meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
    if meta_refresh:
        content = meta_refresh.get("content", "")
        # Extract URL from content like "0; url=https://..." or "0;url=/careers"
        match = re.search(r"url\s*=\s*(.+)", content, re.I)
        if match:
            refresh_url = match.group(1).strip().strip("'\"")
            # Resolve relative URL
            refresh_url = urljoin(homepage_url, refresh_url)
            refresh_parsed = urlparse(refresh_url)
            # Check for ATS domain — don't follow
            if any(ats in refresh_parsed.netloc for ats in _ATS_DOMAINS):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to ATS domain '%s' — returning None",
                    homepage_url,
                    refresh_parsed.netloc,
                )
                return None
            # Check if refresh target is a careers subdomain or careers path
            if any(refresh_parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to careers subdomain '%s'",
                    homepage_url,
                    refresh_url,
                )
                return refresh_url
            if any(pattern in refresh_parsed.path for pattern in _CAREERS_PATTERNS):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to careers path '%s'",
                    homepage_url,
                    refresh_url,
                )
                return refresh_url

    # Search all <a href="..."> for careers-pattern matches
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue

        # Check absolute URLs pointing to careers subdomains
        if href.lower().startswith("http"):
            href_parsed = urlparse(href)
            if any(href_parsed.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS):
                # Verify it's not an ATS domain
                if not any(ats in href_parsed.netloc for ats in _ATS_DOMAINS):
                    logger.debug(
                        "find_careers_url('%s'): found link to careers subdomain '%s'",
                        homepage_url,
                        href,
                    )
                    return href

        # Check if href matches any careers pattern
        href_lower = href.lower()
        for pattern in _CAREERS_PATTERNS:
            # Match: href starts with pattern OR contains the pattern as a path segment
            if (
                href_lower == pattern
                or href_lower.startswith(pattern + "/")
                or href_lower.startswith(pattern + "?")
            ):
                # Resolve relative URL to absolute
                absolute_url = urljoin(homepage_url, href)
                logger.debug(
                    "find_careers_url('%s'): found careers link '%s'",
                    homepage_url,
                    absolute_url,
                )
                return absolute_url

            # Also match absolute URLs that contain the pattern in path
            if href_lower.startswith("http") and pattern in urlparse(href_lower).path:
                logger.debug(
                    "find_careers_url('%s'): found absolute careers link '%s'",
                    homepage_url,
                    href,
                )
                return href

    logger.debug("find_careers_url('%s'): no careers link found in HTML", homepage_url)

    # Proactive subdomain probe: try careers.{domain}, jobs.{domain}, etc.
    base_domain = _extract_base_domain(homepage_url)
    if base_domain:
        for prefix in _CAREERS_SUBDOMAINS:
            candidate = f"https://{prefix}{base_domain}/"
            try:
                probe = requests.head(
                    candidate,
                    timeout=3,
                    headers=_HEADERS,
                    allow_redirects=True,
                )
                if probe.status_code >= 400:
                    continue
                final = urlparse(probe.url)
                if any(ats in final.netloc for ats in _ATS_DOMAINS):
                    continue
                # Validate final URL still looks like a careers page —
                # reject if redirect bounced back to main site
                if any(final.netloc.startswith(sub) for sub in _CAREERS_SUBDOMAINS):
                    logger.debug(
                        "find_careers_url('%s'): subdomain probe hit '%s'",
                        homepage_url,
                        probe.url,
                    )
                    return probe.url
                if any(p in final.path for p in _CAREERS_PATTERNS):
                    logger.debug(
                        "find_careers_url('%s'): subdomain probe hit '%s' (path match)",
                        homepage_url,
                        probe.url,
                    )
                    return probe.url
            except Exception:
                continue

    # low-tier fallback: if heuristic found nothing and client is available
    if conn is not None and config is not None:
        logger.debug("find_careers_url('%s'): trying low-tier fallback", homepage_url)
        return _find_careers_url_with_low_tier(homepage_url, resp.text, conn, config)

    return None


def scrape_careers_page(
    careers_url: str,
    target_titles: list[str],
    exclusions: list[str],
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
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
    falls back to low-tier AI extraction via _extract_jobs_with_low_tier.

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
            return not any(ex.lower() in title_lower for ex in exclusions)

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

        results.append(
            {
                "title": title,
                "url": absolute_url,
            }
        )

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

    # low-tier fallback when HTML parsing found no matching jobs
    if not results and conn is not None and config is not None:
        logger.debug("scrape_careers_page('%s'): trying low-tier fallback", careers_url)
        results = _extract_jobs_with_low_tier(
            careers_url, resp.text, target_titles, exclusions, conn, config
        )

    return results
