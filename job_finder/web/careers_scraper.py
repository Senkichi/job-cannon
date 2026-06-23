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

from job_finder.config import JD_STORAGE_MAX_CHARS
from job_finder.web.careers_crawler._title_filters import clean_title
from job_finder.web.claude_client import call_claude
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
from job_finder.web.platform_extractor import extract_clean_jd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CAREERS_PATTERNS = ["/careers", "/jobs", "/join", "/join-us", "/work-with-us", "/openings"]

# Aggregator / blog-repost registrable domains that masquerade as employer
# careers pages. Jobs scraped from these carry the BLOG's brand as the company
# ("Jobflarely" <- jobflarely.liveblog365.com) and recycle reposts whose cards
# glue title + date + "View Job ->" CTA together. We never scrape them — a code
# blocklist is the durable backstop (config-yaml denylists rot + drift, per the
# dual-copy CI test). Suffix-matched against the URL host, so any subdomain is
# covered. Extend this list, not config, when a new repost host surfaces.
_BLOCKLISTED_SCRAPE_HOSTS: frozenset[str] = frozenset(
    {
        "liveblog365.com",
        "nerdleveltech.com",
    }
)


def _is_blocklisted_scrape_host(url: str) -> bool:
    """True if *url*'s host is (a subdomain of) a blocklisted aggregator domain."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in _BLOCKLISTED_SCRAPE_HOSTS)


_LOW_TIER_HTML_CHARS = 3000  # Truncate HTML sent to low tier (~1000 tokens)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0)"}

_TIMEOUT = 10

_JD_DELAY = 1.0  # seconds between job page fetches (rate limiting)
_MAX_JD_CHARS = JD_STORAGE_MAX_CHARS  # cap extracted JD text

# Class names that suggest a child element contains a location (city/region)
_LOCATION_CLASSES = {"location", "city", "geo", "place", "region", "department-location"}

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

# Structured output schemas for the two quick-tier call sites below. Both
# the CLI (Anthropic) and Ollama return the same dict shape when a schema is
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

    try:
        try:
            model_result = call_model(
                tier="quick",
                system=system,
                messages=messages,
                conn=conn,
                config=config,
                output_schema=_CAREERS_URL_SCHEMA,
                job_id=None,
                purpose="find_careers_url",
                max_tokens=256,
            )
            result = model_result.data
        except ProviderCascadeExhaustedError:
            logger.warning(
                "careers_scrape: cascade exhausted for URL discovery of '%s', retrying via CLI",
                homepage_url,
            )
            result, _cost, _schema_valid = call_claude(
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
        logger.debug("quick-tier found careers URL for '%s': %s", homepage_url, url_text)
        return url_text

    return None


def _fetch_job_description(url: str) -> str:
    """Fetch a job page and extract cleaned description text.

    Delegates structure-aware extraction to ``platform_extractor.extract_clean_jd``
    (platform-scoped container → trafilatura markdown + block dedup + page-chrome
    strip), checks for auth-wall signatures, and caps output at _MAX_JD_CHARS.
    Returns empty string on any failure (never None).

    Args:
        url: Job page URL to fetch.

    Returns:
        Cleaned description text, or empty string on failure.
    """
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        # Route through the single chokepoint so any platform-scoped pages and
        # trailing page chrome are handled identically to the other fetch tiers.
        text = extract_clean_jd(url, resp.text) or ""
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

    try:
        try:
            model_result = call_model(
                tier="quick",
                system=system,
                messages=messages,
                conn=conn,
                config=config,
                output_schema=_CAREERS_JOBS_SCHEMA,
                job_id=None,
                purpose="extract_jobs",
                max_tokens=1024,
            )
            result = model_result.data
        except ProviderCascadeExhaustedError:
            logger.warning(
                "careers_scrape: cascade exhausted for job extraction at '%s', retrying via CLI",
                careers_url,
            )
            result, _cost, _schema_valid = call_claude(
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

    When heuristic link-finding returns nothing AND conn/config are
    provided, falls back to a quick-tier model analysis of the truncated
    homepage HTML.

    Args:
        homepage_url: Company homepage URL to scan.
        conn: Optional SQLite connection for cost recording (quick-tier fallback).
        config: Optional application config dict (quick-tier fallback).

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

    When HTML parsing finds 0 matching jobs AND conn/config are provided,
    falls back to a quick-tier model extraction via _extract_jobs_with_low_tier.

    Args:
        careers_url: URL of the careers page to scrape.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.
        conn: Optional SQLite connection for cost recording (quick-tier fallback).
        config: Optional application config dict (quick-tier fallback).

    Returns:
        List of dicts with keys 'title', 'url', and 'description'. Empty list on
        error or if no matching jobs found (including JS-rendered pages).
    """
    # Aggregator/blog repost hosts produce brand-as-company junk — never scrape
    # them. Checked on the requested URL up front (cheap, no fetch).
    if _is_blocklisted_scrape_host(careers_url):
        logger.debug("scrape_careers_page: skipping blocklisted aggregator host %s", careers_url)
        return []

    try:
        resp = requests.get(careers_url, timeout=_TIMEOUT, headers=_HEADERS)
    except Exception as e:
        logger.debug("scrape_careers_page('%s') request failed: %s", careers_url, e)
        return []

    # Re-check after redirects: a benign-looking URL may 30x to a repost host.
    if _is_blocklisted_scrape_host(resp.url):
        logger.debug(
            "scrape_careers_page: host %s redirected to blocklisted aggregator", careers_url
        )
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

        # Whitespace-normalize the card text (join adjacent text nodes with a
        # space so the Blue State shape "Principal Analyst (Evergreen)NY, DC" is
        # split, and the sibling <span class="location"> stays extractable below),
        # THEN run the string-level title repair. clean_title strips the trailing
        # date/CTA card chrome ("Data Scientist / IA Engineer Jun 15, 2026 View
        # Job ->" -> "Data Scientist / IA Engineer") so both the relevance match
        # and the persisted value are the clean title. We deliberately use the
        # string variant, NOT the tag-aware _clean_title: its heading/first-child
        # strategies would grab the location <span> as the title on this markup.
        # ParsedJob.from_job re-runs the same contract at the universal chokepoint.
        raw_title = " ".join(tag.stripped_strings)

        # Skip empty links, navigation-only links without text
        if not href or not raw_title:
            continue

        # Apply the keyword/exclusion filter on the RAW card text: an exclusion
        # keyword (e.g. "Intern") must match the original title even if cleaning
        # would later strip it as a trailing qualifier. (Running the filter on the
        # cleaned title let excluded jobs leak through once "- Intern" was removed.)
        if not _title_matches(raw_title, target_titles, exclusions):
            continue

        # Persist the CLEANED title — strips the trailing date/CTA card chrome.
        title = clean_title(raw_title)
        if not title:
            continue

        # Resolve relative URL
        absolute_url = urljoin(careers_url, href)

        # Deduplicate by URL
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        # Extract location from the same DOM area as the title.
        # Priority 1: child element with a location-indicative class or <small>.
        # Priority 2: sibling text/elements in the parent container.
        location = ""
        loc_tag = tag.find(
            lambda t: (
                t.name in ("span", "small", "div", "p", "em", "strong")
                and bool(set(t.get("class") or []) & _LOCATION_CLASSES)
            )
        )
        if loc_tag:
            location = loc_tag.get_text(strip=True)
        else:
            parent = tag.parent
            if parent is not None:
                sibling_texts = []
                for child in parent.children:
                    if child is tag:
                        continue
                    if hasattr(child, "get_text"):
                        text = child.get_text(strip=True)
                        if text:
                            sibling_texts.append(text)
                    else:
                        text = str(child).strip()
                        if text:
                            sibling_texts.append(text)
                location = " ".join(sibling_texts)

        results.append(
            {
                "title": title,
                "url": absolute_url,
                "location": location,
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
