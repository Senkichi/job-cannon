"""Individual enrichment tier implementations for job data extraction.

Each function implements a single data source: direct URL fetch, ATS API,
careers page scraping, DuckDuckGo search, SerpAPI search, Haiku extraction,
and Sonnet deep extraction.

These are called by data_enricher.enrich_job() in cost order.
"""

import json
import logging
import re
from typing import Optional, Any

import requests
from bs4 import BeautifulSoup

from job_finder.web.model_provider import call_model

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

# Auth-wall signatures: if page text contains any of these (case-insensitive),
# the fetched page is a login/CAPTCHA wall, not a real JD. Return None.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
    # LinkedIn variants — the most common source of auth-wall false negatives
    "agree & join linkedin",
    "join to apply",
    "join or sign in to find your next job",
    "sign in with email",
    # Generic login/CAPTCHA walls
    "create your free account",
    "verify you're not a robot",
    "enable javascript to view this page",
]

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
]


# ---------------------------------------------------------------------------
# Content quality validation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tier implementations
# ---------------------------------------------------------------------------


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

        # Reject website chrome, login pages, and wrong page types
        if is_chrome_or_login_page(text):
            logger.debug("Chrome/login page detected for '%s', rejecting", url)
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
            from job_finder.web.ats_scanner import scan_lever, scan_greenhouse, scan_ashby
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


def search_serpapi(query: str, api_key: str) -> Optional[dict]:
    """Search Google Jobs via SerpAPI for job details.

    Args:
        query: Search query string (e.g., "Data Scientist Acme Corp").
        api_key: SerpAPI API key.

    Returns:
        Dict with job data (jd_full, salary_min, salary_max, location) or None
        if no results found.

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
            return None

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

        return result if result else None

    except TransientEnrichmentError:
        raise  # Let caller handle transient errors
    except requests.exceptions.Timeout as e:
        raise TransientEnrichmentError(f"SerpAPI timeout for '{query}'") from e
    except Exception as e:
        logger.debug("SerpAPI search failed for '%s': %s", query, e)
        return None


def search_duckduckgo(query: str) -> Optional[str]:
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
