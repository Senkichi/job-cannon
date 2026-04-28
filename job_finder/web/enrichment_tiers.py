"""Individual enrichment tier implementations for job data extraction.

Each function implements a single data source: direct URL fetch, ATS API,
careers page scraping, DuckDuckGo search, SerpAPI search.

These are called by data_enricher.enrich_job() in cost order.

Phase 2b sub-fix RC4 removed the LLM-synthesis tiers (extract_with_haiku,
extract_with_sonnet) — they fabricated short pseudo-JDs from search-result
fragments and blocked escalation to fetch tiers that actually retrieved
the real JD. Structured fields (salary, location) are now extracted
post-fetch from jd_full via parse_structured_fields() (Phase 2c).
"""

import logging
import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from job_finder.web.domain_policy import domain_priority, is_blocked_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DuckDuckGo Instant Answer API endpoint
_DDG_API_URL = "https://api.duckduckgo.com/"

# SerpAPI Google Jobs endpoint
_SERPAPI_URL = "https://serpapi.com/search.json"

# HTTP headers for external requests
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)")
}

# Tags to strip from HTML before extracting text
_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "aside"]

# Maximum characters to return from direct JD fetch
_MAX_JD_CHARS = 8000

# Auth-wall signatures: if page text contains any of these (case-insensitive),
# the fetched page is a login/CAPTCHA wall, not a real JD. Return None.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# Timeout for external API calls (seconds)
_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Tier implementations
# ---------------------------------------------------------------------------


def is_short_auth_page(text: str) -> bool:
    """Return True if text looks like a short auth-wall or CAPTCHA page.

    Detection: page is under 2000 chars AND the first 500 chars contain
    an auth/bot signal keyword.
    """
    if not text or len(text) >= 2000:
        return False
    prefix = text[:500].lower()
    signals = [
        "sign in",
        "log in",
        "login",
        "captcha",
        "just a moment",
        "access denied",
        "verify you are human",
        "verify you are a human",
    ]
    return any(s in prefix for s in signals)


def fetch_direct_jd(url: str) -> str | None:
    """Attempt a direct HTTP GET and return cleaned job description text.

    Strips noisy HTML tags and returns cleaned text capped at 8000 chars.

    Args:
        url: The job URL to fetch.

    Returns:
        Cleaned text up to 8000 chars, or None on any error.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Strip noisy tags
        for tag in soup.find_all(_NOISE_TAGS):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Reject auth-wall / CAPTCHA pages that return login HTML instead of JD
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for '%s', rejecting", url)
            return None

        return text[:_MAX_JD_CHARS] if text.strip() else None

    except Exception as e:
        logger.debug("Direct fetch failed for '%s': %s", url, e)
        return None


def query_ats_api(job_row: dict, conn: Any, config: dict) -> dict:
    """Query ATS API (Lever/Greenhouse/Ashby) for job data if company has a slug.

    Looks up the company record from the DB. If ats_probe_status='hit',
    calls the appropriate ATS scan function with a loose title match derived
    from significant words in the job title.

    Args:
        job_row: Job row dict with company_id field.
        conn: Open SQLite connection.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max. Empty if not found.
    """
    try:
        company_id = job_row.get("company_id")
        if not company_id:
            return {}

        company_row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

        if not company_row:
            return {}

        if dict(company_row).get("ats_probe_status") != "hit":
            return {}

        platform = dict(company_row).get("ats_platform")
        slug = dict(company_row).get("ats_slug")
        if not platform or not slug:
            return {}

        # Derive loose target titles from significant words in job title
        title = job_row.get("title", "")
        target_titles = [w for w in title.split() if len(w) > 3]

        exclusions = config.get("scoring", {}).get("exclusions", [])

        # Lazy import with ImportError guard
        try:
            from job_finder.web.ats_scanner import scan_ashby, scan_greenhouse, scan_lever
        except ImportError:
            return {}

        postings = []
        if platform == "lever":
            postings = scan_lever(slug, target_titles, exclusions)
        elif platform == "greenhouse":
            postings = scan_greenhouse(slug, target_titles, exclusions)
        elif platform == "ashby":
            postings = scan_ashby(slug, target_titles, exclusions)

        if not postings:
            return {}

        # Take the first matching posting
        posting = postings[0]
        result = {}
        if posting.get("description"):
            result["jd_full"] = posting["description"][:_MAX_JD_CHARS]
        if posting.get("salary_min"):
            result["salary_min"] = posting["salary_min"]
        if posting.get("salary_max"):
            result["salary_max"] = posting["salary_max"]

        return result

    except Exception as e:
        logger.debug("ATS API query failed: %s", e)
        return {}


def scrape_careers(job_row: dict, conn: Any, config: dict) -> dict:
    """Scrape company careers page for matching job listing.

    Looks up company homepage_url from DB. If found, uses find_careers_url
    and scrape_careers_page to extract JD from the HTML careers page.

    Args:
        job_row: Job row dict with company_id field.
        conn: Open SQLite connection.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full. Empty if not found.
    """
    try:
        company_id = job_row.get("company_id")
        if not company_id:
            return {}

        company_row = conn.execute(
            "SELECT homepage_url, careers_url FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

        if not company_row:
            return {}

        homepage_url = dict(company_row).get("homepage_url")
        if not homepage_url:
            return {}

        # Lazy import with ImportError guard
        try:
            from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
        except ImportError:
            return {}

        # Use cached careers_url or discover from homepage
        careers_url = dict(company_row).get("careers_url")
        if not careers_url:
            careers_url = find_careers_url(homepage_url)
            if careers_url:
                conn.execute(
                    "UPDATE companies SET careers_url = ? WHERE id = ?",
                    (careers_url, company_id),
                )
                conn.commit()
        if not careers_url:
            return {}

        title = job_row.get("title", "")
        target_titles = [w for w in title.split() if len(w) > 3]
        exclusions = config.get("scoring", {}).get("exclusions", [])

        postings = scrape_careers_page(careers_url, target_titles, exclusions)
        if not postings:
            return {}

        posting = postings[0]
        result = {}
        if posting.get("description"):
            result["jd_full"] = posting["description"][:_MAX_JD_CHARS]

        return result

    except Exception as e:
        logger.debug("Careers scrape failed: %s", e)
        return {}


def search_serpapi(query: str, api_key: str) -> tuple[dict | None, list[str]]:
    """Search Google Jobs via SerpAPI for job details.

    Args:
        query: Search query string (e.g., "Data Scientist Acme Corp").
        api_key: SerpAPI API key.

    Returns:
        2-tuple of (result_dict, apply_urls):
        - result_dict: Dict with job data or None if no results.
        - apply_urls: Filtered and priority-sorted apply option URLs.
    """
    try:
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
            "num": 1,
        }
        response = requests.get(_SERPAPI_URL, params=params, timeout=_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        jobs = data.get("jobs_results", [])
        if not jobs:
            return None, []

        job = jobs[0]
        result = {}

        # Extract job description
        description = job.get("description")
        if description:
            result["jd_full"] = description[:_MAX_JD_CHARS]

        # Extract location
        location = job.get("location")
        if location:
            result["location"] = location

        # Extract salary from detected_extensions
        extensions = job.get("detected_extensions", {})
        salary_str = extensions.get("salary", "")
        if salary_str:
            salary_range = _parse_salary_string(salary_str)
            if salary_range:
                result.update(salary_range)

        # Extract, filter, and sort apply_options URLs
        apply_options = job.get("apply_options", [])
        apply_urls = [
            opt["link"]
            for opt in apply_options
            if opt.get("link") and not is_blocked_domain(opt["link"])
        ]
        apply_urls.sort(key=domain_priority)

        # Try to fetch JD from ATS apply URLs
        for url in apply_urls:
            try:
                url_jd = fetch_direct_jd(url)
                if url_jd:
                    result["url_jd"] = url_jd
                    break
            except Exception:
                pass

        return (result if result else None), apply_urls

    except Exception as e:
        logger.debug("SerpAPI search failed for '%s': %s", query, e)
        return None, []


def search_duckduckgo(query: str) -> str | None:
    """Query DuckDuckGo Instant Answer API for job/company info.

    Args:
        query: Search query string.

    Returns:
        AbstractText content string, or None if no useful content found.
    """
    try:
        params = {
            "q": query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "1",
        }
        response = requests.get(_DDG_API_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        data = response.json()

        # Try AbstractText first (most informative)
        abstract = data.get("AbstractText", "")
        if abstract:
            return abstract

        # Fall back to first RelatedTopic text
        topics = data.get("RelatedTopics", [])
        for topic in topics:
            if isinstance(topic, dict) and topic.get("Text"):
                return topic["Text"]

        return None

    except Exception as e:
        logger.debug("DuckDuckGo search failed for '%s': %s", query, e)
        return None


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _parse_salary_string(salary_str: str) -> dict | None:
    """Parse a salary string like '$140K-$180K/yr' into min/max integers.

    Args:
        salary_str: Salary string from SerpAPI detected_extensions.

    Returns:
        Dict with salary_min and/or salary_max as integers, or None if parsing fails.
    """
    try:
        # Remove currency symbols and whitespace
        cleaned = salary_str.upper().replace("$", "").replace(",", "").strip()

        # Handle K (thousands) and M (millions)
        def parse_amount(s: str) -> int | None:
            s = s.strip()
            if s.endswith("K"):
                return int(float(s[:-1]) * 1000)
            elif s.endswith("M"):
                return int(float(s[:-1]) * 1_000_000)
            else:
                try:
                    return int(float(s))
                except ValueError:
                    return None

        result = {}

        # Range pattern: "140K-180K" or "140,000-180,000"
        range_match = re.search(r"([\d.]+[KM]?)\s*[-–]\s*([\d.]+[KM]?)", cleaned)
        if range_match:
            low = parse_amount(range_match.group(1))
            high = parse_amount(range_match.group(2))
            if low:
                result["salary_min"] = low
            if high:
                result["salary_max"] = high
            return result if result else None

        # Single value: "$140K"
        single_match = re.search(r"([\d.]+[KM]?)", cleaned)
        if single_match:
            val = parse_amount(single_match.group(1))
            if val:
                return {"salary_min": val}

        return None

    except Exception:
        logger.debug("_parse_salary_string failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Page content helpers
# ---------------------------------------------------------------------------

# Browser-like headers for sites that block bot UAs
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Chrome/login page detection signals
_CHROME_SIGNALS = [
    "download google chrome",
    "update your browser",
    "browser not supported",
    "enable cookies",
    "cookies are disabled",
    "accept cookies to continue",
]

_LOGIN_PAGE_SIGNALS = [
    "create your free account",
    "sign up for free",
    "start your free trial",
    "register to view",
    "join now to view",
]

# Delay between DDG web search queries (rate limiting)
_DDG_SEARCH_DELAY_S = 1.0


_COMPANY_STOP_WORDS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "co",
        "the",
        "and",
        "group",
        "holdings",
        "international",
        "services",
        "solutions",
        "technologies",
    }
)


def company_tokens(company_name: str) -> list[str]:
    """Extract meaningful tokens from a company name, filtering stop words.

    Returns lowercase tokens that are >= 2 chars and not in the stop list.
    """
    if not company_name:
        return []
    raw_tokens = re.split(r"[\s.,;:!?&/|()]+", company_name.lower())
    return [t for t in raw_tokens if len(t) >= 2 and t not in _COMPANY_STOP_WORDS]


def company_name_in_text(company_name: str, text: str) -> bool:
    """Check whether any meaningful company token appears in the text."""
    tokens = company_tokens(company_name)
    if not tokens:
        return False
    text_lower = text.lower()
    return any(t in text_lower for t in tokens)


def extract_content_from_html(html: str) -> str | None:
    """Extract cleaned text content from raw HTML.

    Strips noise tags (script, style, nav, etc.) and returns cleaned text.

    Args:
        html: Raw HTML string.

    Returns:
        Cleaned text content, or None if empty.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    return text if text.strip() else None


def is_chrome_or_login_page(text: str) -> bool:
    """Return True if text looks like a browser upgrade or login/signup page.

    Checks for Chrome download prompts, browser upgrade notices, cookie
    consent walls, and generic signup gates.

    Args:
        text: Cleaned page text to check.

    Returns:
        True if the page is a Chrome/browser page or login gate.
    """
    if not text:
        return False

    text_lower = text[:2000].lower()
    if any(sig in text_lower for sig in _CHROME_SIGNALS):
        return True
    return bool(any(sig in text_lower for sig in _LOGIN_PAGE_SIGNALS))


# ---------------------------------------------------------------------------
# LinkedIn JD extraction
# ---------------------------------------------------------------------------


def fetch_linkedin_jd(url: str) -> str | None:
    """Extract job description from a LinkedIn guest job page.

    LinkedIn guest pages serve full JD content inside a specific container
    even though the surrounding page chrome contains login prompts that
    trip the generic auth-wall detector.

    Args:
        url: A LinkedIn job URL (e.g. linkedin.com/jobs/view/...).

    Returns:
        Cleaned JD text up to _MAX_JD_CHARS, or None if extraction fails.
    """
    try:
        response = requests.get(url, headers=_BROWSER_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        jd_el = soup.select_one("div.show-more-less-html__markup")
        if jd_el is None:
            jd_el = soup.select_one("div.description__text")

        if jd_el is None:
            logger.debug("LinkedIn JD container not found for '%s'", url)
            return None

        text = jd_el.get_text(separator="\n", strip=True)
        if not text.strip():
            return None

        return text[:_MAX_JD_CHARS]

    except Exception as e:
        logger.debug("LinkedIn JD fetch failed for '%s': %s", url, e)
        return None


# ---------------------------------------------------------------------------
# DDG web search tier
# ---------------------------------------------------------------------------


def search_ddg_web(title: str, company: str) -> dict:
    """Search DuckDuckGo web search for job description URLs and snippets.

    Generates two search queries, collects up to 8 candidate URLs, filters
    blocked domains, and sorts by domain priority.

    Args:
        title: Job title.
        company: Company name.

    Returns:
        Dict with keys:
        - "ddg_urls": list[str] of discovered URLs (up to 8)
        - "ddg_snippet": str concatenation of result body text
    """
    queries = [
        f'"{company}" "{title}" job description',
        f"{company} careers {title}",
    ]

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for i, query in enumerate(queries):
        results: list[dict] = []
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
        except Exception as exc:
            logger.debug("DDG web search failed for '%s': %s", query[:60], exc)

        # Empty result without exception = all engines exhausted for this query.
        # Surface as WARNING for operator visibility (was silent before).
        if not results:
            logger.warning("DDGS: all engines returned empty for query '%s'", query[:80])

        for r in results:
            href = r.get("href", "")
            if href and href not in seen_urls:
                seen_urls.add(href)
                all_results.append(r)

        if i < len(queries) - 1:
            time.sleep(_DDG_SEARCH_DELAY_S)

    filtered_urls: list[str] = []
    for r in all_results:
        href = r.get("href", "")
        if href and not is_blocked_domain(href):
            filtered_urls.append(href)

    filtered_urls.sort(key=domain_priority)
    filtered_urls = filtered_urls[:8]

    snippets = [r.get("body", "") for r in all_results if r.get("body")]
    ddg_snippet = "\n\n".join(snippets) if snippets else ""

    return {
        "ddg_urls": filtered_urls,
        "ddg_snippet": ddg_snippet,
    }


def fetch_ddg_jds(urls: list[str]) -> tuple[str | None, str | None]:
    """Fetch job descriptions from DDG search result URLs.

    Tries each URL (up to 4 attempts), routing LinkedIn URLs through the
    specialized extractor and others through the generic fetcher.

    Args:
        urls: List of candidate URLs from DDG web search.

    Returns:
        2-tuple of (jd_text, source_url):
        - jd_text: First successful JD text (>= 200 chars), or None
        - source_url: The URL that yielded the JD, or None
    """
    for url in urls[:4]:
        try:
            if "linkedin.com/jobs/" in url:
                jd_text = fetch_linkedin_jd(url)
            elif is_blocked_domain(url):
                continue
            else:
                jd_text = fetch_direct_jd(url)

            if jd_text and len(jd_text) >= 200 and not is_chrome_or_login_page(jd_text):
                logger.debug("DDG URL fetch success: %s (%d chars)", url[:80], len(jd_text))
                return jd_text, url
        except Exception as exc:
            logger.debug("DDG URL fetch failed for %s: %s", url[:80], exc)

    return None, None
