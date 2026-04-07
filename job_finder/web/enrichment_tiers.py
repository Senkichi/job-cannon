"""Individual enrichment tier implementations for job data extraction.

Each function implements a single data source: direct URL fetch, ATS API,
careers page scraping, DuckDuckGo search, SerpAPI search, Haiku extraction,
and Sonnet deep extraction.

These are called by data_enricher.enrich_job() in cost order.
"""

import json
import logging
import re
import time
from typing import Optional, Any

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from job_finder.web.model_provider import call_model
from job_finder.web.domain_policy import is_blocked_domain, domain_priority

logger = logging.getLogger(__name__)


class TransientEnrichmentError(Exception):
    """Raised when an enrichment tier fails due to a transient error (429, 5xx, timeout).

    Signals to the caller that this tier should be retried later,
    NOT advanced past.
    """
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DuckDuckGo Instant Answer API endpoint
_DDG_API_URL = "https://api.duckduckgo.com/"

# SerpAPI Google Jobs endpoint
_SERPAPI_URL = "https://serpapi.com/search.json"

# HTTP headers for external requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)"
    )
}

# Tags to strip from HTML before extracting text
_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "aside"]

# Maximum characters to return from direct JD fetch
_MAX_JD_CHARS = 8000

# Auth-wall signatures split into two tiers:
#
# _FULL_TEXT_AUTH_SIGNATURES — specific multi-word phrases safe to check against
# the FULL text of any page regardless of length. These are unlikely to appear
# as incidental text in nav bars, footers, or body copy of real job descriptions.
# Used by fetch_direct_jd() for unrestricted full-text matching.
_FULL_TEXT_AUTH_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
    # LinkedIn variants — the most common source of auth-wall false negatives
    "agree & join linkedin",
    "join to apply",
    "join or sign in to find your next job",
    "sign in with email",
    # Generic login/CAPTCHA walls (specific enough for full-text)
    "create your free account",
    "verify you're not a robot",
    "enable javascript to view this page",
]

# _SHORT_PAGE_AUTH_SIGNALS — generic short substrings that commonly appear in
# nav bars, footers, and sidebars of legitimate long JD pages. ONLY safe to
# check on SHORT pages (< 2000 chars) where their presence strongly signals
# an auth-wall rather than incidental page chrome.
_SHORT_PAGE_AUTH_SIGNALS = [
    "sign in",
    "log in",
    "create account",
    "captcha",
    "please verify",
    "just a moment",
]

# Combined list used by is_short_auth_page() which gates on len < 2000.
_AUTH_WALL_SIGNATURES = _FULL_TEXT_AUTH_SIGNATURES + _SHORT_PAGE_AUTH_SIGNALS

# Timeout for external API calls (seconds)
_TIMEOUT = 10

# Chrome patterns: if the first ~300 chars of scraped text contain these,
# the page is website chrome (cookie banners, nav), not a real JD.
_CHROME_HEADER_SIGNATURES = [
    "cookie",
    "close this dialog",
    "we and our third-party partners",
    "accept all cookies",
    "manage cookie preferences",
]

# LinkedIn-specific: page is a login-gated LinkedIn listing (has some job text
# mixed with login prompts). These appear throughout the text.
_LINKEDIN_WALL_MARKERS = [
    "agree & join linkedin",
    "join or sign in to find your next job",
    "join to apply for the",
    "sign in with email",
    "forgot password?",
]

# Page-type markers: scraped content is a company overview, search results page,
# or job board listing — not an individual JD.
_WRONG_PAGE_SIGNATURES = [
    "view all jobs at",        # Company overview pages (Built In, etc.)
    "recently posted jobs at", # Built In company profiles
    "total employees",         # Built In company overview chrome
    "perks + benefits",        # Built In company overview chrome
    "similar companies hiring",  # Built In company overview chrome
    "jobs at similar companies",  # Built In company overview chrome
    # Eightfold/Phenom PCS ATS SPA shell: JS-rendered careers pages inject CSS
    # theming config as inline JSON. The actual JD requires JS execution to render.
    '"themeoptions"',          # Eightfold/PCS careers page SPA config JSON
    '"vartheme"',              # Eightfold/PCS CSS variable theming config
]


# Stop-words excluded from company-name matching heuristic.
# Single-char tokens and common business suffixes appear on every page.
_COMPANY_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "in", "at", "by",
    "to", "on", "with", "inc", "llc", "ltd", "corp", "co",
})


# ---------------------------------------------------------------------------
# Content quality validation
# ---------------------------------------------------------------------------


def company_tokens(company: str) -> list[str]:
    """Extract meaningful tokens from a company name for text matching.

    Splits on whitespace/punctuation, removes stop-words and single-char tokens.
    Used by fetch_ddg_jds() and the agentic enricher's company-name heuristic.

    Args:
        company: Company name string.

    Returns:
        List of lowercase tokens, empty if all tokens are stop-words.
    """
    import re as _re
    return [
        tok for tok in _re.split(r"[\s\-&,\.]+", company.lower())
        if tok and tok not in _COMPANY_STOP_WORDS and len(tok) > 1
    ]


def company_name_in_text(company: str, text: str) -> bool:
    """Return True if at least one meaningful company-name token appears in text.

    Lightweight validation to reject fetched pages that don't mention the target
    company at all — catching wrong-company JDs from job board mirrors, salary
    aggregators, and career advice articles.

    Returns True (pass) when company_tokens is empty (degenerate company name
    like "AI" or "The") to avoid false rejections. Callers that want fail-closed
    behavior on empty tokens should check company_tokens() separately.

    Args:
        company: Company name to check for.
        text: Page text to search (case-insensitive).

    Returns:
        True if the company is mentioned or tokens are degenerate.
    """
    tokens = company_tokens(company)
    if not tokens:
        return True  # degenerate name — can't validate, let it through
    return any(tok in text.lower() for tok in tokens)


# Phrases that reliably appear in real job descriptions but NOT in Wikipedia
# articles, salary pages, company about pages, or job board listing pages.
# At least one must appear (case-insensitive) for a page to be accepted as a JD.
_JD_CONTENT_MARKERS = [
    "responsibilities",
    "qualifications",
    "requirements",
    "what you'll do",
    "what you will do",
    "about the role",
    "about this role",
    "job description",
    "you will",
    "we are looking for",
    "we're looking for",
    "preferred qualifications",
    "minimum qualifications",
    "basic qualifications",
    "key responsibilities",
    "role summary",
    "position summary",
    "job summary",
    "about the job",
    "about the position",
    "the ideal candidate",
    "your responsibilities",
    "what we're looking for",
    "what we offer",
    "benefits",
    "equal opportunity",
]


def has_jd_content(text: str) -> bool:
    """Return True if text contains job description content markers.

    Rejects Wikipedia articles, salary pages, company about pages, and job board
    listing pages that pass company-name validation but aren't actual JDs.

    Args:
        text: Page text to check (case-insensitive).

    Returns:
        True if at least one JD content marker is present.
    """
    if not text:
        return False
    text_lower = text.lower()
    return any(marker in text_lower for marker in _JD_CONTENT_MARKERS)


def is_chrome_or_login_page(text: str) -> bool:
    """Return True if scraped text is website chrome, a login wall, or a wrong page type.

    Checks for:
    - Cookie banners in the first 300 characters
    - LinkedIn login page markers anywhere in text
    - Company overview / job board listing pages (not individual JDs)

    Args:
        text: Cleaned page text to validate.

    Returns:
        True if the text should be rejected (not a usable JD).
    """
    if not text:
        return True

    text_lower = text.lower()
    header_lower = text_lower[:300]

    # Cookie/consent banners at the top of the page
    if any(sig in header_lower for sig in _CHROME_HEADER_SIGNATURES):
        return True

    # LinkedIn login wall (markers appear throughout the page)
    linkedin_hits = sum(1 for sig in _LINKEDIN_WALL_MARKERS if sig in text_lower)
    if linkedin_hits >= 2:
        return True

    # Wrong page type (company overview, search results, etc.)
    if any(sig in text_lower for sig in _WRONG_PAGE_SIGNATURES):
        return True

    return False


def is_short_auth_page(text: str) -> bool:
    """Return True when the page is a short auth-wall (login/CAPTCHA page).

    A page is classified as a short auth-wall when ALL of the following hold:
    - At least one _AUTH_WALL_SIGNATURES string matches the first 500 chars
      (checked case-insensitive), AND
    - Total page text length is < 2000 characters.

    This heuristic catches the agentic enricher's short-page auth-wall case
    (which previously duplicated this logic inline). The short-page threshold
    filters out real job pages that incidentally mention "sign in" in a header.

    Note: For long pages (len >= 2000), rely on is_chrome_or_login_page() and
    the full _AUTH_WALL_SIGNATURES check in fetch_direct_jd() instead.

    Args:
        text: Cleaned page text to validate.

    Returns:
        True if the page is a short auth-wall; False otherwise.
    """
    if not text:
        return False

    # Only flag short pages — real JDs are rarely under 2000 chars
    if len(text) >= 2000:
        return False

    text_lower = text[:500].lower()
    return any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES)


# ---------------------------------------------------------------------------
# HTML content extraction
# ---------------------------------------------------------------------------


def extract_content_from_html(html: str) -> Optional[str]:
    """Extract main content from HTML, stripping navigation, chrome, and boilerplate.

    Uses trafilatura's density-based extraction as the primary method. trafilatura
    identifies the main content zone by measuring text density and link density
    across DOM subtrees — the same principle used by readability algorithms — so
    it generalises across aggregators, job boards, and ATS platforms without
    requiring site-specific configuration.

    Falls back to BeautifulSoup noise-tag stripping when trafilatura returns
    nothing (e.g. very sparse pages, Cloudflare challenge pages that trafilatura
    can't parse). The fallback preserves existing behaviour for those cases.

    Args:
        html: Raw HTML string. Works with both static HTML and Playwright-rendered
              DOM (post-JS-execution).

    Returns:
        Cleaned text content, or None if both methods fail to produce output.
    """
    # Primary: trafilatura density-based extraction
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            include_tables=True,    # Salary/requirements tables are JD content
            include_comments=False,  # Skip comment sections on job boards
        )
        if text and len(text) >= 300:
            return text
    except Exception:
        pass  # ImportError (not installed) or any extraction error → fall through

    # Fallback: strip known-noisy tags, return full page text
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(_NOISE_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text if text.strip() else None
    except Exception:
        return None


# Browser-like User-Agent for sites that block bot UAs
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Tier implementations
# ---------------------------------------------------------------------------


def fetch_linkedin_jd(url: str) -> Optional[str]:
    """Extract job description from a LinkedIn guest job page.

    LinkedIn guest pages serve full JD content inside a specific container
    even though the surrounding page chrome contains login prompts that
    trip the generic auth-wall detector. This function targets the JD
    container directly.

    Args:
        url: A LinkedIn job URL (e.g. linkedin.com/jobs/view/...).

    Returns:
        Cleaned JD text up to _MAX_JD_CHARS, or None if extraction fails.
    """
    try:
        response = requests.get(url, headers=_BROWSER_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Primary selector: the "show more" JD container
        jd_el = soup.select_one("div.show-more-less-html__markup")
        # Fallback: broader description container
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


def fetch_direct_jd(url: str) -> Optional[str]:
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

        text = extract_content_from_html(response.text)
        if not text:
            return None

        # DEFECT 006 FIX: is_short_auth_page() catches short Cloudflare/CAPTCHA pages
        # (< 2000 chars) before the heavier full-text checks. Short challenge pages
        # (e.g. "Just a moment... Cloudflare") pass is_chrome_or_login_page() because
        # they lack cookie-banner markers, but are correctly caught here.
        if is_short_auth_page(text):
            logger.debug("Short auth-wall detected for '%s', rejecting", url)
            return None

        # Reject auth-wall / CAPTCHA pages that return login HTML instead of JD.
        # Uses _FULL_TEXT_AUTH_SIGNATURES (specific multi-word phrases) — NOT the
        # combined _AUTH_WALL_SIGNATURES which contains generic short substrings
        # ("sign in", "log in") that cause false positives on long pages with
        # nav bars or footers mentioning login. Short pages are already caught
        # by is_short_auth_page() above.
        text_lower = text.lower()
        if any(sig in text_lower for sig in _FULL_TEXT_AUTH_SIGNATURES):
            logger.debug("Auth-wall detected for '%s', rejecting", url)
            return None

        # Reject website chrome, login pages, and wrong page types
        if is_chrome_or_login_page(text):
            logger.debug("Chrome/login page detected for '%s', rejecting", url)
            return None

        return text[:_MAX_JD_CHARS]

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
            from job_finder.web.ats_platforms import scan_lever, scan_greenhouse, scan_ashby
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
            "SELECT homepage_url FROM companies WHERE id = ?",
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

        careers_url = find_careers_url(homepage_url)
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


def extract_with_sonnet(
    fragments: dict,
    job_row: dict,
    client: Any,
    conn: Any,
    config: dict,
) -> dict:
    """Use Sonnet to deep-extract structured data from ALL accumulated fragments.

    Assembles all text fragments from prior tiers into a single context string
    (budget 4000-6000 chars). Prompts Sonnet to extract jd_full and salary
    from sparse/fragmented signals.

    Args:
        fragments: Dict of all text fragments accumulated from prior tiers.
        job_row: Job row dict for context (title, company).
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max. Empty dict on failure.
    """
    try:
        title = job_row.get("title", "")
        company = job_row.get("company", "")

        # Assemble all fragments into context string (cap at 5000 chars total)
        context_parts = []
        for key, text in fragments.items():
            if text and isinstance(text, str):
                label = key.replace("_", " ").upper()
                context_parts.append(f"[{label}]\n{str(text)[:1500]}")
        context_text = "\n\n".join(context_parts)[:5000]

        if not context_text:
            context_text = f"Job posting: {title} at {company}"

        system_prompt = (
            "You are an expert job data extractor. Given fragments of information "
            "about a job posting (from web searches, ATS APIs, and careers pages), "
            "extract structured information as a JSON object. "
            "Extract only what is explicitly stated — do not invent data. "
            "Return ONLY a JSON object with these optional fields: "
            "jd_full (string, full job description), "
            "salary_min (integer, USD annual), "
            "salary_max (integer, USD annual). "
            "Omit fields that cannot be determined from the provided text."
        )

        user_prompt = (
            f"Job: {title} at {company}\n\n"
            f"Information fragments from multiple sources:\n{context_text}\n\n"
            f"Extract job details as JSON. Include only fields that are explicitly mentioned."
        )

        job_id = job_row.get("dedup_key")

        result_obj = call_model(
            tier="sonnet",
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=None,
            job_id=job_id,
            purpose="enrich_job_sonnet",
            max_tokens=1024,
            client=client,
        )
        result = result_obj.data

        if isinstance(result, dict):
            enriched = {}
            for key, value in result.items():
                if value is not None and key in ("jd_full", "salary_min", "salary_max"):
                    if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                        enriched[key] = int(value)
                    elif key == "jd_full" and isinstance(value, str) and value.strip():
                        enriched[key] = value
            return enriched

        return {}

    except Exception as e:
        logger.debug("Sonnet extraction failed: %s", e)
        return {}


def search_serpapi(query: str, api_key: str) -> tuple[Optional[dict], list[str]]:
    """Search Google Jobs via SerpAPI for job details.

    Also surfaces apply_options URLs (canonical ATS links from Google Jobs results)
    at zero additional API cost. These often contain greenhouse.io, lever.co, or
    other ATS URLs that give a direct path to the full JD.

    Args:
        query: Search query string (e.g., "Data Scientist Acme Corp").
        api_key: SerpAPI API key.

    Returns:
        2-tuple of (result_dict, apply_option_urls):
        - result_dict: Dict with job data (jd_full, salary_min, salary_max, location,
          url_jd if an apply_option URL fetched successfully), or None if no results found.
        - apply_option_urls: Sorted list of valid ATS URL strings extracted from
          apply_options (blocked domains filtered, sorted by domain_priority()).
          Always a list (never None), empty if no apply_options found.

    Raises:
        TransientEnrichmentError: On 429 rate-limit or 5xx server errors.
            Callers should NOT advance past this tier.
    """
    try:
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
            "num": 1,
        }
        response = requests.get(_SERPAPI_URL, params=params, timeout=_TIMEOUT)

        # Distinguish transient failures from genuine "no results"
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientEnrichmentError(
                f"SerpAPI {response.status_code} for '{query}'"
            )
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

        # Surface apply_options URLs (ATS canonical links) at zero extra API cost.
        # Filter blocked domains (glassdoor, indeed, etc.) and sort by priority rank
        # so ATS platforms (greenhouse, lever) are tried before generic job boards.
        apply_option_urls: list[str] = []
        for option in job.get("apply_options", []):
            link = option.get("link", "")
            if link and not is_blocked_domain(link):
                apply_option_urls.append(link)

        # Sort by domain_priority: lower index = higher priority (ATS platforms first)
        apply_option_urls.sort(key=domain_priority)

        # DEFECT 014 FIX: Always attempt direct JD fetch from ATS URLs regardless of
        # whether result already contains a "jd_full" from the Google Jobs description.
        # ATS canonical pages (greenhouse.io, lever.co) often carry a longer, formatted
        # JD than the Google Jobs snippet. Stored under "url_jd" key so
        # _resolve_from_fragments() can compare both and pick the better one, while
        # _persist() safely ignores "url_jd" (it's not in _ENRICHABLE_COLUMNS).
        if apply_option_urls:
            for ats_url in apply_option_urls:
                fetched_text = fetch_direct_jd(ats_url)
                if fetched_text:
                    result["url_jd"] = fetched_text
                    logger.debug(
                        "SerpAPI apply_option JD fetched from %s (%d chars)",
                        ats_url[:80],
                        len(fetched_text),
                    )
                    break  # First successful fetch wins

        return (result if result else None), apply_option_urls

    except TransientEnrichmentError:
        raise  # Let caller handle transient errors
    except requests.exceptions.Timeout as e:
        raise TransientEnrichmentError(f"SerpAPI timeout for '{query}'") from e
    except Exception as e:
        logger.debug("SerpAPI search failed for '%s': %s", query, e)
        return None, []


def search_duckduckgo(query: str) -> Optional[str]:
    """Query DuckDuckGo Instant Answer API for job/company info.

    .. deprecated:: Use search_ddg_web() for actual web search results.
       This function queries the Instant Answer API which returns encyclopedia
       snippets, not web search results. Retained for backward compatibility
       (company_enricher still uses it).

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
# DDG Web Search (replaces Instant Answer API for job enrichment)
# ---------------------------------------------------------------------------

# Delay between DDG web search queries to avoid rate limits
_DDG_SEARCH_DELAY_S = 1.0


def search_ddg_web(title: str, company: str) -> dict:
    """Search DuckDuckGo web search for job description URLs and snippets.

    Uses the ddgs library (real web search via ddgs.text()) instead of the
    Instant Answer API which only returns encyclopedia content.

    Generates two search queries with different strategies, collects up to 8
    candidate URLs, filters blocked domains, and sorts by domain priority.

    Args:
        title: Job title.
        company: Company name.

    Returns:
        Dict with keys:
        - "ddg_urls": list[str] of discovered URLs (up to 8), filtered and sorted
        - "ddg_snippet": str concatenation of result body text (for Haiku fallback)
    """
    queries = [
        f'"{company}" "{title}" job description',
        f"{company} careers {title}",
    ]

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for i, query in enumerate(queries):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            for r in results:
                href = r.get("href", "")
                if href and href not in seen_urls:
                    seen_urls.add(href)
                    all_results.append(r)
        except Exception as exc:
            logger.debug("DDG web search failed for '%s': %s", query[:60], exc)

        # Rate-limit delay between queries (skip after last query)
        if i < len(queries) - 1:
            time.sleep(_DDG_SEARCH_DELAY_S)

    # Filter blocked domains and sort by priority
    filtered_urls: list[str] = []
    for r in all_results:
        href = r.get("href", "")
        if href and not is_blocked_domain(href):
            filtered_urls.append(href)

    filtered_urls.sort(key=domain_priority)
    filtered_urls = filtered_urls[:8]

    # Concatenate body text for Haiku extraction fallback
    snippets = [r.get("body", "") for r in all_results if r.get("body")]
    ddg_snippet = "\n\n".join(snippets) if snippets else ""

    return {
        "ddg_urls": filtered_urls,
        "ddg_snippet": ddg_snippet,
    }


def fetch_ddg_jds(
    urls: list[str],
    title: str = "",
    company: str = "",
) -> tuple[Optional[str], Optional[str]]:
    """Fetch job descriptions from DDG search result URLs.

    Tries each URL (up to 4 attempts), routing LinkedIn individual job pages
    through the specialized extractor and others through the generic fetcher.
    LinkedIn search result pages (/jobs/keyword-jobs) are skipped.

    Validates fetched content against company name to reject wrong-company JDs
    from job board mirrors, salary aggregators, and career advice articles.

    Args:
        urls: List of candidate URLs from DDG web search.
        title: Job title (for logging context).
        company: Company name — fetched pages must mention at least one
            meaningful token from this name to be accepted.

    Returns:
        2-tuple of (jd_text, source_url):
        - jd_text: First valid JD text (>= 200 chars, correct company), or None
        - source_url: The URL that yielded the JD, or None
    """
    for url in urls[:4]:
        try:
            # LinkedIn: only individual job pages, not search result pages
            if "linkedin.com/jobs/view/" in url:
                jd_text = fetch_linkedin_jd(url)
            elif "linkedin.com/jobs/" in url:
                # Search result page (e.g. /jobs/google-analyst-jobs) — skip
                logger.debug("Skipping LinkedIn search page: %s", url[:80])
                continue
            elif is_blocked_domain(url):
                continue
            else:
                jd_text = fetch_direct_jd(url)

            if not jd_text or len(jd_text) < 200 or is_chrome_or_login_page(jd_text):
                continue

            # Validate: does the fetched content mention the target company?
            if company and not company_name_in_text(company, jd_text):
                logger.debug(
                    "DDG: rejecting %s (company '%s' not found in text)",
                    url[:80], company[:30],
                )
                continue

            # Validate: does the page contain actual JD content (responsibilities,
            # qualifications, etc.)? Rejects Wikipedia, salary pages, about pages.
            if not has_jd_content(jd_text):
                logger.debug(
                    "DDG: rejecting %s (no JD content markers found)",
                    url[:80],
                )
                continue

            logger.debug("DDG URL fetch success: %s (%d chars)", url[:80], len(jd_text))
            return jd_text, url
        except Exception as exc:
            logger.debug("DDG URL fetch failed for %s: %s", url[:80], exc)

    return None, None


def extract_with_haiku(
    search_text: str,
    job_row: dict,
    client: Any,
    conn: Any,
    config: dict,
) -> dict:
    """Use Haiku to extract structured job data from accumulated fragment text.

    Sends search_text to Haiku with a structured extraction prompt to parse
    out salary range, location, and job description summary.

    Args:
        search_text: Aggregated text from DDG and other free-tier sources.
        job_row: Job row dict for context (title, company).
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max, location.
        Returns empty dict on failure.
    """
    try:
        title = job_row.get("title", "")
        company = job_row.get("company", "")

        system_prompt = (
            "You are a job data extractor. Given text about a job posting, extract "
            "structured information as a JSON object. Extract only what is explicitly "
            "stated — do not invent data. Return ONLY a JSON object with these optional "
            "fields: jd_full (string, job description summary), salary_min (integer, USD), "
            "salary_max (integer, USD), location (string). Omit fields that are not present."
        )

        user_prompt = (
            f"Job: {title} at {company}\n\n"
            f"Text to extract from:\n{search_text[:2000]}\n\n"
            f"Extract job details as JSON. Include only fields that are explicitly mentioned."
        )

        job_id = job_row.get("dedup_key")

        result_obj = call_model(
            tier="haiku",
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=None,
            job_id=job_id,
            purpose="enrich_job",
            max_tokens=512,
            client=client,
        )
        result = result_obj.data

        if isinstance(result, dict):
            # Remove None values and ensure salary fields are integers
            enriched = {}
            for key, value in result.items():
                if value is not None and key in ("jd_full", "salary_min", "salary_max", "location"):
                    if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                        enriched[key] = int(value)
                    elif isinstance(value, str) and value.strip():
                        enriched[key] = value
            return enriched

        return {}

    except Exception as e:
        logger.debug("Haiku extraction failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _parse_salary_string(salary_str: str) -> Optional[dict]:
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
        def parse_amount(s: str) -> Optional[int]:
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
