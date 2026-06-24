"""Homepage auto-discovery for companies without a known homepage URL.

Four-tier lookup:
1. Domain guess — strip suffixes from name_raw, try single-token as domain.
   Zero API cost.
2. Slug heuristic — try ats_slug first, then name-derived slug as domain.
   Zero API cost.
3. Claude CLI lookup — invoke ``claude -p`` with WebSearch/WebFetch to
   resolve hard-case homepages (multi-word names, abbreviations, parent
   companies) that the mechanical tiers miss. $0 via Claude.ai subscription.
4. SerpAPI web search — query SerpAPI engine=google for company homepage.
   Paid; used as last resort when Claude CLI declines.

Used by run_homepage_discovery() which processes up to _BATCH_CAP companies
per run. Stamps homepage_probe_attempted_at on every company processed
(success or failure) for retry-avoidance.
"""

import logging
import re

import requests

from job_finder.secrets import get_secret
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.http_fetch import fetch_with_deadline

# Lazy import of careers-page resolver (ImportError guard — mirrors
# _run_html.py and enrichment_tiers.py). Keeps homepage_discoverer
# importable when careers_scraper is unavailable.
try:
    from job_finder.web.careers_scraper import find_careers_url
except ImportError:
    find_careers_url = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0)"}
_TIMEOUT = 10

_PARKED_DOMAIN_SIGNATURES = [
    "domain is for sale",
    "buy this domain",
    "parked domain",
    "this domain is available",
    "hugedomains.com",
    "domain_profile.cfm",
    "gen.xyz/cart",
    "this domain may be for sale",
    "make an offer on this domain",
]

_BATCH_CAP = 10  # Conservative SerpAPI quota (100-250/month depending on plan)

# Domains to skip as SerpAPI results (not real company sites)
_SKIP_DOMAINS = [
    "glassdoor.com",
    "crunchbase.com",
    "bloomberg.com",
    "zoominfo.com",
    "pitchbook.com",
    "linkedin.com",
    "wikipedia.org",
]

_SERPAPI_BASE_URL = "https://serpapi.com/search.json"

_COMPANY_SUFFIXES = frozenset(
    [
        "inc",
        "llc",
        "corp",
        "co",
        "ltd",
        "group",
        "inc.",
        "llc.",
        "corp.",
        "co.",
        "ltd.",
    ]
)

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
    """Auto-discover company homepage URL via four-tier lookup.

    Tier 1 (domain guess): Strip suffixes from name_raw, try single-token
    as domain. Zero API cost.

    Tier 2 (slug heuristic): Try ats_slug first, then name-derived slug
    as domain. Zero API cost.

    Tier 3 (Claude CLI lookup): Invoke ``claude -p`` with WebSearch/WebFetch
    for hard-case companies (multi-word, abbreviations, parent companies).
    $0 via Claude.ai subscription. Skipped if the Claude CLI is unavailable.

    Tier 4 (SerpAPI web search): Query SerpAPI engine=google. Paid; last
    resort. Skipped when api_key is None.

    Args:
        company_name: Human-readable company name.
        ats_platform: ATS platform string (e.g. "ashby", "greenhouse"). Unused
            here but kept for caller convenience.
        ats_slug: ATS slug to try as domain prefix (e.g. "ramp" -> ramp.com).
        source_urls: List of source URLs from jobs table (reserved for future
            domain extraction from apply_options URLs).
        api_key: SerpAPI key. When None, Tier 4 is skipped.

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

    # Tier 3: Claude CLI lookup ($0 via subscription)
    result = _try_claude_enricher(company_name)
    if result is not None:
        return result

    # Tier 4: SerpAPI web search (skipped when no API key)
    if api_key is not None:
        return _search_serpapi(company_name, api_key)

    return None


def run_homepage_discovery(db_path: str, config: dict | None = None) -> dict:
    """Process up to _BATCH_CAP companies with no homepage_url and no prior probe attempt.

    Creates its own sqlite3 connection (thread-safe for APScheduler).
    Stamps homepage_probe_attempted_at on every company processed (success or failure).
    Short-circuits on SerpAPI quota errors.

    Args:
        db_path: Path to the SQLite database file.
        config: Optional config dict. SerpAPI key for Tier 4 is resolved via
            get_secret("sources.serpapi.api_key", config=config) — env var,
            keyring, or config.yaml plaintext fallback.

    Returns:
        Summary dict:
            companies_checked (int): Number of companies processed.
            homepages_found (int): Number of homepage_url values written.
            errors (list): List of error strings encountered.
    """
    companies_checked = 0
    homepages_found = 0
    errors: list[str] = []

    api_key = get_secret("sources.serpapi.api_key", config=config) if config else None

    with standalone_connection(db_path) as conn:
        companies = conn.execute(
            f"SELECT id, name_raw, ats_platform, ats_slug, ats_probe_status "
            f"FROM companies "
            f"WHERE homepage_url IS NULL AND homepage_probe_attempted_at IS NULL "
            f"LIMIT {_BATCH_CAP}"
        ).fetchall()

        for row in companies:
            company_id, name_raw, ats_platform, ats_slug, ats_probe_status = row
            companies_checked += 1

            # Fetch source_urls for this company from jobs table (FK join)
            try:
                source_url_rows = conn.execute(
                    "SELECT source_urls FROM jobs WHERE company_id = ? AND source_urls IS NOT NULL",
                    (company_id,),
                ).fetchall()
                import json as _json

                source_urls = []
                for r in source_url_rows:
                    try:
                        source_urls.extend(_json.loads(r[0]))
                    except (ValueError, TypeError):
                        pass
            except Exception as e:
                logger.debug("Could not fetch source_urls for %s: %s", name_raw, e)
                source_urls = []

            try:
                url = discover_homepage(
                    name_raw, ats_platform, ats_slug, source_urls, api_key=api_key
                )
            except SerpAPIQuotaError as e:
                logger.error("SerpAPI quota error -- stopping batch: %s", e)
                errors.append(f"QUOTA_ERROR: {e}")
                # Stamp this company before breaking
                conn.execute(
                    "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                    (company_id,),
                )
                conn.commit()
                break
            except Exception as e:
                error_msg = f"{name_raw}: {e}"
                logger.debug("discover_homepage failed for %s: %s", name_raw, e)
                errors.append(error_msg)
                # Still stamp probe attempted
                conn.execute(
                    "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                    (company_id,),
                )
                conn.commit()
                continue

            if url:
                # Close the discovery loop (Issue #221): in the same UPDATE
                # that writes homepage_url, also derive careers_url
                # (mechanical, $0 — no conn/config passed) and reset
                # ats_probe_status to 'pending' unless already 'hit' so the
                # next probe_ats_slugs run picks this row up. Never downgrade
                # a confirmed hit.
                careers_url = None
                if find_careers_url is not None:
                    try:
                        careers_url = find_careers_url(url)
                    except Exception as e:
                        logger.debug("find_careers_url failed for %s: %s", url, e)

                set_clauses = [
                    "homepage_url = ?",
                    "homepage_probe_attempted_at = datetime('now')",
                ]
                params: list = [url]
                if careers_url:
                    set_clauses.append("careers_url = ?")
                    params.append(careers_url)
                if ats_probe_status != "hit":
                    set_clauses.append("ats_probe_status = 'pending'")
                params.append(company_id)

                try:
                    conn.execute(
                        f"UPDATE companies SET {', '.join(set_clauses)} WHERE id = ?",
                        tuple(params),
                    )
                    conn.commit()
                    homepages_found += 1
                    logger.debug("Found homepage for %s: %s", name_raw, url)
                except Exception as e:
                    error_msg = f"{name_raw} DB update: {e}"
                    logger.debug("DB update failed for %s: %s", name_raw, e)
                    errors.append(error_msg)
            else:
                # No homepage found — still stamp probe attempted
                conn.execute(
                    "UPDATE companies SET homepage_probe_attempted_at = datetime('now') WHERE id = ?",
                    (company_id,),
                )
                conn.commit()

    return {
        "companies_checked": companies_checked,
        "homepages_found": homepages_found,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _try_domain_guess(name_raw: str) -> str | None:
    """Tier 1: single-token companies only (e.g., 'Stripe' -> stripe.com).

    Strips company suffixes, checks if result is a single token.
    Multi-word names return None immediately (let Tier 2 handle).
    Reuses _try_slug_heuristic for HEAD probe + parked-domain guard.
    """
    stripped = _strip_company_suffixes(name_raw)
    tokens = stripped.split()
    if len(tokens) != 1:
        return None
    return _try_slug_heuristic(tokens[0])


def _try_slug_heuristic(ats_slug: str) -> str | None:
    """Try https://{ats_slug}.com via HEAD + body validation.

    Returns the final URL (after redirects) if the page is HTML and not a
    parked domain, otherwise None.
    """
    url = f"https://{ats_slug}.com"
    try:
        # Many modern sites block HEAD requests (return 403/405/406/502).
        # Try HEAD first for efficiency, fall back to GET if non-200.
        head_resp = requests.head(url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS)
        if head_resp.status_code == 200:
            content_type = head_resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.debug("Slug heuristic: %s has non-HTML content-type: %s", url, content_type)
                return None
            final_url = head_resp.url
        else:
            # HEAD failed — fall back to GET (many sites only accept GET)
            head_resp = None
            final_url = None

        # Fetch body to check for parked domain signatures (first 5000 chars)
        get_resp = fetch_with_deadline(
            url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
        )

        # Bot-blocking codes (403, 405, 406) prove the domain is active —
        # parked domains never return these. Accept the URL directly.
        # But first check the redirect chain didn't land on a domain squatter.
        resolved = final_url or get_resp.url
        if get_resp.status_code in (403, 405, 406):
            if any(sig in resolved.lower() for sig in _PARKED_DOMAIN_SIGNATURES):
                logger.debug("Slug heuristic: %s redirected to parked domain: %s", url, resolved)
                return None
            return resolved

        if get_resp.status_code != 200:
            logger.debug("Slug heuristic: %s GET returned %d", url, get_resp.status_code)
            return None

        body_sample = get_resp.text[:5000].lower()
        for signature in _PARKED_DOMAIN_SIGNATURES:
            if signature in body_sample:
                logger.debug("Slug heuristic: %s appears to be a parked domain", url)
                return None

        # Return final URL after redirects (prefer HEAD redirect chain, fall back to GET)
        return final_url or get_resp.url

    except Exception as e:
        logger.debug("Slug heuristic failed for %s: %s", url, e)
        return None


def _try_claude_enricher(company_name: str) -> str | None:
    """Tier 3: Claude CLI lookup for hard-case homepages.

    Invokes ``claude -p`` with WebSearch/WebFetch tooling to resolve
    companies whose mechanical name→domain mapping fails. The Claude CLI
    runs against the user's Claude.ai subscription ($0 per call).

    Imports ``enrich_companies_via_claude`` lazily so this module doesn't
    fail to import on machines without the Claude CLI installed — the call
    itself returns [] when the CLI binary is missing.

    Args:
        company_name: Company name to look up.

    Returns:
        Validated homepage URL string, or None if no result or invalid URL.
    """
    try:
        from job_finder.web.claude_enricher import enrich_companies_via_claude

        results = enrich_companies_via_claude([{"name": company_name}])
    except Exception as e:
        logger.debug("claude_enricher failed for '%s': %s", company_name, e)
        return None

    if not results:
        return None

    homepage_url = results[0].get("homepage_url")
    if not homepage_url or not homepage_url.startswith("http"):
        return None

    # Validate before returning — Claude can occasionally produce dead URLs.
    return _validate_url(homepage_url)


def _search_serpapi(company_name: str, api_key: str) -> str | None:
    """Tier 4: SerpAPI Google web search for company homepage.

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
