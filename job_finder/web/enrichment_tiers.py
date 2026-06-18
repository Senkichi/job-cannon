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

from job_finder.config import JD_STORAGE_MAX_CHARS
from job_finder.web.direct_link import resolve_primary_posting
from job_finder.web.domain_policy import domain_priority, is_blocked_domain
from job_finder.web.html_extract import html_to_clean_text
from job_finder.web.model_provider import call_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DuckDuckGo Instant Answer API endpoint
_DDG_API_URL = "https://api.duckduckgo.com/"

# SerpAPI Google Jobs endpoint
_SERPAPI_URL = "https://serpapi.com/search.json"

# HTTP request headers + timeout — shared with careers_crawler.py and
# careers_page_interactions.py via the central _http_constants module.
from job_finder.web._http_constants import _HEADERS, _TIMEOUT

# Maximum characters to return from direct JD fetch
_MAX_JD_CHARS = JD_STORAGE_MAX_CHARS

# Auth-wall signatures: if page text contains any of these (case-insensitive),
# the fetched page is a login/CAPTCHA wall, not a real JD. Return None.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# Minimum jd_full length before parse_structured_fields will spend a quick-tier call.
# Matches data_enricher.MIN_FETCH_JD_CHARS — anything shorter is residual
# auth-wall noise that wouldn't yield reliable salary/location signal.
_MIN_STRUCTURED_PARSE_JD_CHARS = 200

# Minimum text length for fetch_direct_jd to consider a fetch result a real JD.
# JS-rendered SPA shells (e.g., Workday at malformed URLs) leave only the page
# <title> after stripping <script>/<style>, producing single-token results like
# "Workday" that get persisted as fake JDs. Real JDs are far longer than this.
_MIN_VALID_JD_CHARS = 200

# JSON schema for the post-fetch structured-field extraction call.
# DELIBERATELY EXCLUDES jd_full so the model cannot summarize the description
# back into the description field (the bug the deleted Haiku/Sonnet synthesis
# tiers had — they fabricated short pseudo-JDs from search snippets).
# P1.2: salary_period added so the LLM can signal the posting's pay period;
# the value is routed through normalize_observation for unit math (D-2/D-3).
_STRUCTURED_FIELDS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "salary_min": {"type": "integer"},
        "salary_max": {"type": "integer"},
        "salary_period": {"type": "string", "enum": ["annual", "hourly", "monthly"]},
        "location": {"type": "string"},
    },
}

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

        # Structure-aware extraction (JD Layer 2): strips boilerplate, keeps
        # headings/lists, collapses duplicate blocks. Falls back to a plain
        # noise-tag strip internally when the page has no article structure.
        text = html_to_clean_text(response.text)
        if not text:
            logger.debug("fetch_direct_jd('%s'): no extractable text", url)
            return None

        # Reject auth-wall / CAPTCHA pages that return login HTML instead of JD
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for '%s', rejecting", url)
            return None

        stripped = text.strip()
        if len(stripped) < _MIN_VALID_JD_CHARS:
            logger.debug(
                "fetch_direct_jd('%s'): result %d chars (< %d), rejecting "
                "as SPA-shell or other empty page",
                url,
                len(stripped),
                _MIN_VALID_JD_CHARS,
            )
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
        Dict with direct_url + direct_url_confidence when any posting links,
        plus jd_full / salary_min / salary_max / _primary_posting ONLY on a
        strict (unambiguous) title match. Empty if not found.
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

        # Strict-gated data merge: posting DATA (jd_full, salary) is taken only
        # from an unambiguous title match — a loose match yields the LINK only,
        # so a wrong job's description can never reach jd_full and the scorer.
        resolved = resolve_primary_posting(postings, title, job_row.get("location") or "")
        if resolved is None:
            return {}
        posting, url, confidence = resolved

        result: dict = {"direct_url": url, "direct_url_confidence": confidence}
        if posting is not None:
            if posting.get("description"):
                result["jd_full"] = posting["description"][:_MAX_JD_CHARS]
            if posting.get("salary_min"):
                result["salary_min"] = posting["salary_min"]
            if posting.get("salary_max"):
                result["salary_max"] = posting["salary_max"]
            # Full posting for the wider authoritative-field merge (currency,
            # period, posted_date, locations, source_id). Underscore key: not
            # a DB column; _resolve_from_fragments only lifts missing fields,
            # so it never reaches _persist.
            result["_primary_posting"] = posting

        return result

    except Exception as e:
        logger.warning("ATS API query failed: %s", e)
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
        Dict with direct_url + direct_url_confidence when any posting links,
        plus jd_full / _primary_posting ONLY on a strict title match.
        Empty if not found.
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

        # Strict-gated data merge — same contamination guard as query_ats_api.
        resolved = resolve_primary_posting(postings, title, job_row.get("location") or "")
        if resolved is None:
            return {}
        posting, url, confidence = resolved

        result: dict = {"direct_url": url, "direct_url_confidence": confidence}
        if posting is not None:
            if posting.get("description"):
                result["jd_full"] = posting["description"][:_MAX_JD_CHARS]
            result["_primary_posting"] = posting

        return result

    except Exception as e:
        logger.warning("Careers scrape failed: %s", e)
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
# Post-fetch structured-field extraction (Phase 2c)
# ---------------------------------------------------------------------------


def parse_structured_fields(
    jd_full: str,
    job_row: dict,
    conn: Any,
    config: dict,
) -> dict:
    """Extract salary and location from a fully-fetched jd_full.

    Runs ONCE post-cascade, on the actual fetched description (no
    fragment truncation). Schema deliberately excludes jd_full so the
    model cannot summarize the description back into itself — that was
    the bug the deleted Haiku/Sonnet synthesis tiers had.

    Args:
        jd_full: The full job description text (post-fetch).
        job_row: Job record dict; uses 'dedup_key', 'title', 'company'.
        conn: Open SQLite connection (for cost recording in call_model).
        config: Application config dict (for provider routing).

    Returns:
        Dict containing only fields the model populated. None values
        are omitted. Returns {} on short jd_full, missing data, or any
        exception.
    """
    if not jd_full or len(jd_full) < _MIN_STRUCTURED_PARSE_JD_CHARS:
        return {}

    title = job_row.get("title", "")
    company = job_row.get("company", "")
    job_id = job_row.get("dedup_key")

    system_prompt = (
        "You extract structured fields from a job description. "
        "Return ONLY a JSON object with optional fields: "
        "salary_min (integer, in the unit stated by the posting), "
        "salary_max (integer, in the unit stated by the posting), "
        "salary_period (string, one of: annual|hourly|monthly — include only "
        "if the description explicitly states a pay period), "
        "location (string). Omit fields that cannot be determined. "
        "Do not invent data."
    )
    user_prompt = (
        f"Job: {title} at {company}\n\n"
        f"Description:\n{jd_full}\n\n"
        f"Extract structured fields as JSON. Include only fields explicitly mentioned."
    )

    try:
        result = call_model(
            tier="quick",  # cheap; structured-extraction task
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=_STRUCTURED_FIELDS_SCHEMA,
            job_id=job_id,
            purpose="parse_structured_fields",
            max_tokens=256,
        )
    except Exception as exc:
        logger.warning("parse_structured_fields: error for %s: %s", job_id, exc)
        return {}

    if not result.data or not result.schema_valid:
        return {}

    # P1.2 (D-2/D-3): route LLM-reported salary through normalize_observation
    # so the single normalizer applies the salvage ladder (hourly → annualize,
    # implausible → drop both, etc.) instead of a bespoke inline bounds check.
    # The old inline check dropped BOTH salary fields when EITHER was out of
    # bounds but couldn't salvage hourly values — normalize_observation does
    # both correctly. Both-or-neither semantics are preserved by the normalizer's
    # pair discipline.
    from job_finder.salary_normalizer import (
        SalaryObservation,
        normalize_observation,
    )

    raw_min = result.data.get("salary_min")
    raw_max = result.data.get("salary_max")
    raw_period = result.data.get("salary_period") or "unknown"

    out: dict = {}

    if raw_min is not None or raw_max is not None:
        obs = SalaryObservation(
            min_value=float(raw_min) if raw_min is not None else None,
            max_value=float(raw_max) if raw_max is not None else None,
            period=raw_period,
            currency="USD",
            provenance="llm_extract",
            raw_text=f"llm: min={raw_min} max={raw_max} period={raw_period}",
        )
        normalized = normalize_observation(obs)
        if normalized.resolution in (
            "ok",
            "salvaged_hourly",
            "salvaged_daily",
            "salvaged_weekly",
            "salvaged_monthly",
        ):
            if normalized.salary_min is not None:
                out["salary_min"] = normalized.salary_min
            if normalized.salary_max is not None:
                out["salary_max"] = normalized.salary_max
            if normalized.period != "unknown":
                out["salary_period"] = normalized.period
        else:
            logger.warning(
                "parse_structured_fields: dropping implausible salary for %s "
                "(min=%s max=%s period=%s, resolution=%s)",
                job_id,
                raw_min,
                raw_max,
                raw_period,
                normalized.resolution,
            )

    location = result.data.get("location")
    if location is not None:
        out["location"] = location

    return out


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _parse_salary_string(salary_str: str) -> dict | None:
    """Parse a salary string like '$140K-$180K/yr' into min/max integers.

    P1.2 (D-2): thin wrapper — delegates to ``salary_normalizer.parse_salary_text``
    (single parser) + ``normalize_observation`` (single normalizer) instead of
    duplicating bespoke regex + K/M logic. Hourly/period cues are now captured
    and annualized via the salvage ladder (D-3) rather than silently ignored.
    Implausible values return None (existing behavior).

    Args:
        salary_str: Salary string from SerpAPI detected_extensions.

    Returns:
        Dict with salary_min and/or salary_max as integers, or None if parsing fails.
    """
    from job_finder.salary_normalizer import normalize_observation, parse_salary_text

    try:
        obs = parse_salary_text(salary_str, provenance="feed_string")
        if obs is None:
            return None
        normalized = normalize_observation(obs)
        if normalized.resolution not in (
            "ok",
            "salvaged_hourly",
            "salvaged_daily",
            "salvaged_weekly",
            "salvaged_monthly",
        ):
            return None
        result: dict = {}
        if normalized.salary_min is not None:
            result["salary_min"] = normalized.salary_min
        if normalized.salary_max is not None:
            result["salary_max"] = normalized.salary_max
        return result if result else None
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

    Structure-aware extraction via ``html_to_clean_text`` (JD Layer 2): strips
    boilerplate, preserves headings/lists, collapses duplicate blocks, and
    falls back to a plain noise-tag strip when trafilatura can't parse the page.
    Consumed by ``agentic_enricher`` to convert fetched HTML into ``jd_full``.

    Args:
        html: Raw HTML string.

    Returns:
        Cleaned text content, or None if empty.
    """
    if not html:
        return None
    return html_to_clean_text(html)


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
        # INFO not WARNING — the pipeline degrades gracefully to other search
        # backends, so this is not actionable for the operator.
        if not results:
            logger.info("DDGS: all engines returned empty for query '%s'", query[:80])

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
