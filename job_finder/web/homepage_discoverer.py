"""Homepage auto-discovery for companies without a known homepage URL.

Two-tier lookup:
1. Slug heuristic — try https://{ats_slug}.com via HEAD request, validate HTML
   content-type and guard against parked domains.
2. DuckDuckGo HTML search fallback — query "{company_name} official website" on
   https://html.duckduckgo.com/html/ and parse the first organic result.

Used by discover_homepages_batch() which processes up to _BATCH_CAP companies
per run, respecting a _DDG_DELAY between DDG queries to avoid rate limiting.
"""

import logging
import sqlite3
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0)"}
_TIMEOUT = 10

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

_PARKED_DOMAIN_SIGNATURES = [
    "domain is for sale",
    "buy this domain",
    "parked domain",
    "this domain is available",
]

_BATCH_CAP = 50  # max companies per batch run
_DDG_DELAY = 1.0  # seconds between DDG queries to respect rate limits

# Domains that should be skipped as DDG results (not real company sites)
_SKIP_DOMAINS = ["wikipedia.org", "linkedin.com"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_homepage(
    company_name: str,
    ats_platform: str | None,
    ats_slug: str | None,
    source_urls: list[str],
) -> str | None:
    """Auto-discover company homepage URL via two-tier lookup.

    Tier 1: If ats_slug is provided, try https://{ats_slug}.com via HEAD
    request. Validates HTML content-type and guards against parked domains.

    Tier 2: DuckDuckGo HTML search fallback — queries
    "{company_name} official website" and parses first organic result.

    Args:
        company_name: Human-readable company name (for DDG query).
        ats_platform: ATS platform string (e.g. "ashby", "greenhouse"). Unused
            here but kept for caller convenience.
        ats_slug: ATS slug to try as domain prefix (e.g. "ramp" -> ramp.com).
        source_urls: List of source URLs from jobs table (reserved for future
            domain extraction from apply_options URLs).

    Returns:
        Validated homepage URL string, or None if neither tier succeeds.
    """
    # Tier 1: Slug heuristic
    if ats_slug is not None:
        result = _try_slug_heuristic(ats_slug)
        if result is not None:
            return result

    # Tier 2: DuckDuckGo HTML search fallback
    return _search_ddg(company_name)


def discover_homepages_batch(db_path: str, config: dict | None = None) -> dict:
    """Process up to _BATCH_CAP companies with homepage_url IS NULL.

    Creates its own sqlite3 connection (thread-safe, same pattern as
    stale_detector.py). Queries the companies table for rows missing
    homepage_url, calls discover_homepage per company, and updates the DB
    on success.

    Sleeps _DDG_DELAY between companies (conservative — always sleeps even
    when the slug heuristic succeeds, to avoid hammering in batch scenarios).

    Args:
        db_path: Path to the SQLite database file.
        config: Optional config dict (unused, reserved for future use).

    Returns:
        Summary dict:
            companies_checked (int): Number of companies processed.
            homepages_found (int): Number of homepage_url values written.
            errors (list): List of error strings encountered.
    """
    companies_checked = 0
    homepages_found = 0
    errors: list[str] = []

    conn = sqlite3.connect(db_path)
    try:
        companies = conn.execute(
            f"SELECT id, name_raw, ats_platform, ats_slug "
            f"FROM companies WHERE homepage_url IS NULL LIMIT {_BATCH_CAP}"
        ).fetchall()

        for row in companies:
            company_id, name_raw, ats_platform, ats_slug = row
            companies_checked += 1

            # Fetch source_urls for this company from jobs table
            try:
                source_url_rows = conn.execute(
                    "SELECT DISTINCT source_url FROM jobs WHERE company = ? AND source_url != ''",
                    (name_raw,)
                ).fetchall()
                source_urls = [r[0] for r in source_url_rows]
            except Exception as e:
                logger.debug("Could not fetch source_urls for %s: %s", name_raw, e)
                source_urls = []

            try:
                url = discover_homepage(name_raw, ats_platform, ats_slug, source_urls)
            except Exception as e:
                error_msg = f"{name_raw}: {e}"
                logger.debug("discover_homepage failed for %s: %s", name_raw, e)
                errors.append(error_msg)
                time.sleep(_DDG_DELAY)
                continue

            if url:
                try:
                    conn.execute(
                        "UPDATE companies SET homepage_url = ? WHERE id = ?",
                        (url, company_id)
                    )
                    conn.commit()
                    homepages_found += 1
                    logger.debug("Found homepage for %s: %s", name_raw, url)
                except Exception as e:
                    error_msg = f"{name_raw} DB update: {e}"
                    logger.debug("DB update failed for %s: %s", name_raw, e)
                    errors.append(error_msg)

            time.sleep(_DDG_DELAY)

    finally:
        conn.close()

    return {
        "companies_checked": companies_checked,
        "homepages_found": homepages_found,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _try_slug_heuristic(ats_slug: str) -> str | None:
    """Try https://{ats_slug}.com via HEAD + body validation.

    Returns the final URL (after redirects) if the page is HTML and not a
    parked domain, otherwise None.
    """
    url = f"https://{ats_slug}.com"
    try:
        head_resp = requests.head(url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS)
        if head_resp.status_code != 200:
            logger.debug("Slug heuristic: %s returned %d", url, head_resp.status_code)
            return None

        content_type = head_resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            logger.debug("Slug heuristic: %s has non-HTML content-type: %s", url, content_type)
            return None

        # Fetch body to check for parked domain signatures (first 5000 chars)
        get_resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        body_sample = get_resp.text[:5000].lower()

        for signature in _PARKED_DOMAIN_SIGNATURES:
            if signature in body_sample:
                logger.debug("Slug heuristic: %s appears to be a parked domain", url)
                return None

        # Return final URL after redirects
        return head_resp.url

    except Exception as e:
        logger.debug("Slug heuristic failed for %s: %s", url, e)
        return None


def _search_ddg(company_name: str) -> str | None:
    """Query DuckDuckGo HTML search for company homepage.

    Parses organic result links (class="result__a") and returns the first
    non-Wikipedia, non-LinkedIn URL that validates via HEAD request.

    Args:
        company_name: Company name to search for.

    Returns:
        Validated homepage URL or None.
    """
    query = f'"{company_name}" official website'
    try:
        response = requests.get(
            _DDG_HTML_URL,
            params={"q": query},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        soup = BeautifulSoup(response.text, "html.parser")
        result_links = soup.select("a.result__a")

        for link in result_links:
            href = link.get("href", "")
            if not href.startswith("http"):
                continue

            # Skip known non-company domains
            skip = False
            for domain in _SKIP_DOMAINS:
                if domain in href:
                    skip = True
                    break
            if skip:
                continue

            # Validate URL resolves to HTML
            validated = _validate_url(href)
            if validated:
                return validated

        logger.debug("DDG search found no valid result for: %s", company_name)
        return None

    except Exception as e:
        logger.debug("DDG search failed for '%s': %s", company_name, e)
        return None


def _validate_url(url: str) -> str | None:
    """HEAD request to validate URL resolves with 200 and HTML content-type.

    Args:
        url: URL to validate.

    Returns:
        The URL if valid, None otherwise.
    """
    try:
        resp = requests.head(url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return None
        return url
    except Exception as e:
        logger.debug("URL validation failed for %s: %s", url, e)
        return None
