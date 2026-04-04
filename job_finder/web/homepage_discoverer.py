"""Homepage auto-discovery for companies without a known homepage URL.

Three-tier lookup:
1. Domain guess — strip suffixes from name_raw, try single-token as domain.
   Zero API cost.
2. Slug heuristic — try ats_slug first, then name-derived slug as domain.
   Zero API cost.
3. SerpAPI web search — query SerpAPI engine=google for company homepage.
   Replaces broken DDG HTML search.

Used by run_homepage_discovery() which processes up to _BATCH_CAP companies
per run. Stamps homepage_probe_attempted_at on every company processed
(success or failure) for retry-avoidance.
"""

import json
import logging
import re
from typing import Optional

import requests

from job_finder.web.db_helpers import standalone_connection
# _HEADERS and _TIMEOUT imported from enrichment_tiers — eliminates copy-paste
# duplication across careers_scraper.py and homepage_discoverer.py.
# Intentional pragmatic underscore-prefixed import for a single-codebase local app.
from job_finder.web.enrichment_tiers import _HEADERS, _TIMEOUT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# _HEADERS and _TIMEOUT removed — now imported from enrichment_tiers above.

_PARKED_DOMAIN_SIGNATURES = [
    "domain is for sale",
    "buy this domain",
    "parked domain",
    "this domain is available",
]

_BATCH_CAP = 10   # Conservative SerpAPI quota (100-250/month depending on plan)
_FAST_BATCH_CAP = 50  # Free-tier (Tier 1+2) batch cap — no API cost

# Domains to skip as SerpAPI results (not real company sites)
_SKIP_DOMAINS = [
    "glassdoor.com", "crunchbase.com", "bloomberg.com",
    "zoominfo.com", "pitchbook.com", "linkedin.com", "wikipedia.org",
]

_SERPAPI_BASE_URL = "https://serpapi.com/search.json"

_COMPANY_SUFFIXES = frozenset([
    "inc", "llc", "corp", "co", "ltd", "group",
    "inc.", "llc.", "corp.", "co.", "ltd.",
])


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SerpAPIQuotaError(Exception):
    """Raised when SerpAPI returns a quota/error response."""
    pass


# ---------------------------------------------------------------------------
# Name normalization helpers
# ---------------------------------------------------------------------------


def _strip_company_suffixes(name: str) -> str:
    """Lowercase name, strip trailing suffix tokens (Inc, LLC, Corp, etc.)."""
    tokens = name.lower().split()
    while tokens and tokens[-1].rstrip(".") in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _name_to_slug(name: str) -> str:
    """Convert name_raw to hyphenated slug for Tier 2 fallback."""
    stripped = _strip_company_suffixes(name)
    slug = re.sub(r"[^a-z0-9]+", "-", stripped).strip("-")
    return slug


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_homepage(
    company_name: str,
    ats_platform: str | None,
    ats_slug: str | None,
    source_urls: list[str],
    api_key: str | None = None,
) -> str | None:
    """Auto-discover company homepage URL via three-tier lookup.

    Tier 1 (domain guess): Strip suffixes from name_raw, try single-token
    as domain. Zero API cost.

    Tier 2 (slug heuristic): Try ats_slug first, then name-derived slug
    as domain. Zero API cost.

    Tier 3 (SerpAPI web search): Query SerpAPI engine=google. Skipped
    when api_key is None.

    Args:
        company_name: Human-readable company name.
        ats_platform: ATS platform string (e.g. "ashby", "greenhouse"). Unused
            here but kept for caller convenience.
        ats_slug: ATS slug to try as domain prefix (e.g. "ramp" -> ramp.com).
        source_urls: List of source URLs from jobs table (reserved for future
            domain extraction from apply_options URLs).
        api_key: SerpAPI key. When None, Tier 3 is skipped.

    Returns:
        Validated homepage URL string, or None if no tier succeeds.
    """
    # Tier 1: Domain guess (single-token names only)
    result = _try_domain_guess(company_name)
    if result is not None:
        return result

    # Tier 2: Slug heuristic — try ats_slug first, then name-derived slug
    if ats_slug is not None:
        result = _try_slug_heuristic(ats_slug)
        if result is not None:
            return result

    # Tier 2b: Name-derived slug fallback (when ats_slug absent or failed)
    name_slug = _name_to_slug(company_name)
    if name_slug and name_slug != (ats_slug or ""):
        result = _try_slug_heuristic(name_slug)
        if result is not None:
            return result

    # Tier 3: SerpAPI web search (skipped when no API key)
    if api_key is not None:
        return _search_serpapi(company_name, api_key)

    return None


def _process_homepage_batch(
    conn,
    companies: list,
    api_key: str | None,
) -> tuple[int, int, list[str]]:
    """Process a batch of companies for homepage discovery.

    Args:
        conn: Open SQLite connection.
        companies: List of company rows (id, name_raw, ats_platform, ats_slug).
        api_key: SerpAPI key. When None, Tier 3 is skipped.

    Returns:
        Tuple of (companies_checked, homepages_found, errors).
    """
    checked = 0
    found = 0
    errors: list[str] = []

    for row in companies:
        company_id, name_raw, ats_platform, ats_slug = row
        checked += 1

        try:
            source_url_rows = conn.execute(
                "SELECT source_urls FROM jobs WHERE company_id = ? AND source_urls IS NOT NULL AND source_urls != '[]'",
                (company_id,)
            ).fetchall()
            source_urls = []
            for (raw,) in source_url_rows:
                try:
                    import json as _json
                    parsed = _json.loads(raw)
                    if isinstance(parsed, list):
                        source_urls.extend(u for u in parsed if u)
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.debug("Could not fetch source_urls for %s: %s", name_raw, e)
            source_urls = []

        try:
            url = discover_homepage(name_raw, ats_platform, ats_slug, source_urls, api_key=api_key)
        except SerpAPIQuotaError as e:
            logger.error("SerpAPI quota error -- stopping batch: %s", e)
            errors.append(f"QUOTA_ERROR: {e}")
            conn.execute(
                "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                (company_id,)
            )
            conn.commit()
            break
        except Exception as e:
            error_msg = f"{name_raw}: {e}"
            logger.debug("discover_homepage failed for %s: %s", name_raw, e)
            errors.append(error_msg)
            conn.execute(
                "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                (company_id,)
            )
            conn.commit()
            continue

        if url:
            try:
                conn.execute(
                    "UPDATE companies SET homepage_url = ?, homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                    (url, company_id)
                )
                conn.commit()
                found += 1
                logger.debug("Found homepage for %s: %s", name_raw, url)
            except Exception as e:
                error_msg = f"{name_raw} DB update: {e}"
                logger.debug("DB update failed for %s: %s", name_raw, e)
                errors.append(error_msg)
        else:
            conn.execute(
                "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                (company_id,)
            )
            conn.commit()

    return checked, found, errors


def run_homepage_discovery(db_path: str, config: dict | None = None) -> dict:
    """Process companies with no homepage_url and no prior probe attempt.

    Phase A (free tiers): Up to _FAST_BATCH_CAP companies, no SerpAPI key passed.
    Phase B (SerpAPI): Up to _BATCH_CAP additional companies, with api_key.
    Short-circuits on SerpAPI quota errors.

    Args:
        db_path: Path to the SQLite database file.
        config: Optional config dict. Uses config['serpapi']['api_key'] for Phase B.

    Returns:
        Summary dict:
            companies_checked (int): Number of companies processed.
            homepages_found (int): Number of homepage_url values written.
            errors (list): List of error strings encountered.
    """
    api_key = None
    if config:
        api_key = config.get("serpapi", {}).get("api_key")

    companies_checked = 0
    homepages_found = 0
    errors: list[str] = []

    with standalone_connection(db_path) as conn:
        # Phase A: free tiers only (no SerpAPI key) — larger batch
        fast_companies = conn.execute(
            f"SELECT id, name_raw, ats_platform, ats_slug "
            f"FROM companies "
            f"WHERE homepage_url IS NULL AND homepage_probe_attempted_at IS NULL "
            f"LIMIT {_FAST_BATCH_CAP}"
        ).fetchall()

        a_checked, a_found, a_errors = _process_homepage_batch(conn, fast_companies, api_key=None)
        companies_checked += a_checked
        homepages_found += a_found
        errors.extend(a_errors)

        # Phase B: SerpAPI for remaining companies (different set — already stamped)
        if api_key and not any("QUOTA_ERROR" in e for e in a_errors):
            serp_companies = conn.execute(
                f"SELECT id, name_raw, ats_platform, ats_slug "
                f"FROM companies "
                f"WHERE homepage_url IS NULL AND homepage_probe_attempted_at IS NULL "
                f"LIMIT {_BATCH_CAP}"
            ).fetchall()

            b_checked, b_found, b_errors = _process_homepage_batch(conn, serp_companies, api_key=api_key)
            companies_checked += b_checked
            homepages_found += b_found
            errors.extend(b_errors)

    return {
        "companies_checked": companies_checked,
        "homepages_found": homepages_found,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _try_domain_guess(name_raw: str) -> str | None:
    """Tier 1: domain guess for single- or two-token companies.

    Strips company suffixes, then:
    - Single token: try token.com (e.g. 'Stripe' -> stripe.com)
    - Two tokens: try concatenated.com (e.g. 'Palo Alto' -> paloalto.com)
    - Three or more tokens: return None (let Tier 2 handle)

    Reuses _try_slug_heuristic for HEAD probe + parked-domain guard.
    """
    stripped = _strip_company_suffixes(name_raw)
    tokens = stripped.split()
    if len(tokens) == 1:
        return _try_slug_heuristic(tokens[0])
    if len(tokens) == 2:
        return _try_slug_heuristic("".join(tokens))
    return None


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


def _search_serpapi(company_name: str, api_key: str) -> str | None:
    """Tier 3: SerpAPI Google web search for company homepage.

    Queries '{company_name} homepage' via SerpAPI engine=google.
    Iterates organic_results, skips _SKIP_DOMAINS, validates first
    non-skipped URL via HEAD request.

    Raises SerpAPIQuotaError if SerpAPI returns an error response.
    """
    params = {
        "engine": "google",
        "q": f'"{company_name}" homepage',
        "api_key": api_key,
        "num": 5,
    }
    try:
        resp = requests.get(_SERPAPI_BASE_URL, params=params, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("SerpAPI search request failed for '%s': %s", company_name, e)
        return None

    if data.get("error"):
        raise SerpAPIQuotaError(data["error"])

    for result in data.get("organic_results", []):
        link = result.get("link", "")
        if not link.startswith("http"):
            continue
        if any(domain in link for domain in _SKIP_DOMAINS):
            continue
        validated = _validate_url(link)
        if validated:
            return validated

    logger.debug("SerpAPI search found no valid result for: %s", company_name)
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
