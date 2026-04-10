"""Job expiry detection via tiered signal cascade.

Provides:
    _extract_posting_id   -- Extract individual posting ID from ATS URL
    _check_ats_api        -- Signal 1: ATS API liveness check
    _check_careers_page   -- Signal 2: Company careers page title search
    _check_serpapi        -- Signal 3: SerpAPI re-search fallback
    _check_job_expiry     -- Cascade orchestrator for a single job
    run_expiry_check      -- Nightly batch runner (APScheduler entry point)

Architecture:
- Thread-safe: creates own sqlite3 connection (same pattern as stale_detector.py)
- Signal cascade short-circuits on first definitive answer (expired/live)
- Only targets jobs in discovered/reviewing status
- Consecutive careers page failures tracked in-memory (resets on restart)
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEOUT = 10  # seconds for HTTP requests
_INTER_REQUEST_DELAY = 1  # seconds between jobs in batch

# Signal result constants
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# SerpAPI base URL
_SERPAPI_BASE_URL = "https://serpapi.com/search.json"

# ---------------------------------------------------------------------------
# Posting ID extraction (Signal 1 prerequisite)
# ---------------------------------------------------------------------------

_LEVER_POSTING_RE = re.compile(
    r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE
)
_GREENHOUSE_POSTING_RE = re.compile(
    r"boards\.greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE
)
_ASHBY_POSTING_RE = re.compile(
    r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"
    # No IGNORECASE — Ashby slugs are case-sensitive
)

_POSTING_PATTERNS = {
    "lever": _LEVER_POSTING_RE,
    "greenhouse": _GREENHOUSE_POSTING_RE,
    "ashby": _ASHBY_POSTING_RE,
}

def _extract_posting_id(url: str, ats_platform: str) -> Optional[str]:
    """Extract the individual posting ID from an ATS URL.

    Args:
        url: A job source URL string.
        ats_platform: One of 'lever', 'greenhouse', 'ashby'.

    Returns:
        The posting ID string, or None if the URL doesn't match the platform pattern.
    """
    pattern = _POSTING_PATTERNS.get(ats_platform)
    if pattern is None:
        return None
    match = pattern.search(url)
    return match.group(1) if match else None

# ---------------------------------------------------------------------------
# Signal 1: ATS API Check
# ---------------------------------------------------------------------------

def _check_ats_api(slug: str, posting_id: str, ats_platform: str) -> str:
    """Check if a specific job posting is still live via ATS API.

    Args:
        slug: Company's ATS slug (e.g., 'acme-corp').
        posting_id: Individual posting ID extracted from URL.
        ats_platform: One of 'lever', 'greenhouse', 'ashby'.

    Returns:
        EXPIRED if the posting returns 404/410.
        LIVE if the posting returns 200.
        INCONCLUSIVE on network error or unknown platform.
    """
    if ats_platform == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
    elif ats_platform == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
    elif ats_platform == "ashby":
        # Ashby's GraphQL API is complex; check the public job board URL instead
        url = f"https://jobs.ashbyhq.com/{slug}/{posting_id}"
    else:
        return INCONCLUSIVE

    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code in (404, 410):
            return EXPIRED
        if resp.status_code == 200:
            return LIVE
        # Other status codes (403, 500, etc.) are inconclusive
        return INCONCLUSIVE
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return INCONCLUSIVE
    except Exception as e:
        logger.warning("_check_ats_api: unexpected error for %s/%s: %s", slug, posting_id, e)
        return INCONCLUSIVE

# ---------------------------------------------------------------------------
# Signal 2: Company Careers Page Check
# ---------------------------------------------------------------------------

# Lazy imports for careers scraper (may not be available in tests)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

# Lazy import of _title_matches from ats_scanner (per user decision:
# reuse existing title matching for consistency with ATS scan behavior)
try:
    from job_finder.web.ats_platforms import _title_matches
except ImportError:
    _title_matches = None  # type: ignore[assignment]

def _check_careers_page(
    homepage_url: Optional[str],
    job_title: str,
    target_titles: list[str],
    exclusions: list[str],
) -> str:
    """Check if a job title appears on the company's careers page.

    Uses _title_matches from ats_scanner for consistent title matching behavior.

    Args:
        homepage_url: Company homepage URL (from companies table). None if unknown.
        job_title: The job title to search for.
        target_titles: Title keywords for matching (from config).
        exclusions: Title exclusion keywords (from config).

    Returns:
        LIVE if the job title is found on the careers page.
        INCONCLUSIVE if no careers page, page unreachable, or title not found
        (title absence is a weak signal — the page may not list all roles).
    """
    if not homepage_url:
        return INCONCLUSIVE

    if find_careers_url is None or scrape_careers_page is None:
        logger.debug("_check_careers_page: careers_scraper not available")
        return INCONCLUSIVE

    try:
        careers_url = find_careers_url(homepage_url)
        if not careers_url:
            return INCONCLUSIVE

        results = scrape_careers_page(careers_url, target_titles, exclusions)

        # Check if any result title is a close match to our job title
        # using _title_matches for consistency with ATS scan behavior
        for item in results:
            result_title = item.get("title", "")
            if _title_matches is not None:
                # Use [job_title] as single-element target so any result matching
                # the job title returns True
                if _title_matches(result_title, [job_title], []):
                    return LIVE
            else:
                # Fallback: simple case-insensitive match
                if job_title.lower() in result_title.lower() or result_title.lower() in job_title.lower():
                    return LIVE

        # Title not found — but this is a weak signal (JS-rendered pages, etc.)
        return INCONCLUSIVE

    except Exception as e:
        logger.debug("_check_careers_page: error checking %s: %s", homepage_url, e)
        return INCONCLUSIVE

# ---------------------------------------------------------------------------
# Signal 3: SerpAPI Fallback
# ---------------------------------------------------------------------------

def _check_serpapi(job_title: str, company_name: str, config: dict) -> str:
    """Re-search for a job via SerpAPI google_jobs engine.

    Args:
        job_title: The job title to search for.
        company_name: The company name.
        config: Application config dict (reads sources.serpapi.enabled and api_key).

    Returns:
        LIVE if a matching result is found.
        EXPIRED if no matching result in the first batch.
        INCONCLUSIVE if SerpAPI is disabled, has no key, or network error.
    """
    serpapi_config = config.get("sources", {}).get("serpapi", {})
    if not serpapi_config.get("enabled", False):
        return INCONCLUSIVE
    api_key = serpapi_config.get("api_key", "")
    if not api_key:
        return INCONCLUSIVE

    try:
        from thefuzz import fuzz
        params = {
            "engine": "google_jobs",
            "q": f'"{job_title}" "{company_name}"',
            "api_key": api_key,
            "hl": "en",
        }
        resp = requests.get(_SERPAPI_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        company_lower = company_name.lower()

        for result in data.get("jobs_results", []):
            result_title = result.get("title", "")
            result_company = result.get("company_name", "").lower()
            # Match: company name appears in result AND substantial title overlap
            if company_lower in result_company and fuzz.token_set_ratio(job_title, result_title) >= 60:
                return LIVE

        return EXPIRED

    except Exception as e:
        logger.warning("_check_serpapi: error searching for '%s' at '%s': %s", job_title, company_name, e)
        return INCONCLUSIVE

# ---------------------------------------------------------------------------
# In-memory failure tracker (Signal 2 backoff)
# ---------------------------------------------------------------------------

# Maps company_id -> consecutive failure count. Resets on app restart.
# Thread-safety: protected by _careers_lock; APScheduler runs in a background
# thread while Flask request handlers may trigger manual checks concurrently.
_careers_lock = threading.Lock()
_careers_failure_counts: dict[int, int] = {}
_careers_skip_until: dict[int, datetime] = {}

_MAX_CAREERS_FAILURES = 3
_CAREERS_SKIP_DAYS = 7

def _record_careers_outcome(company_id: Optional[int], success: bool) -> None:
    """Track careers page check outcome for backoff logic.

    On success: reset failure count. On failure: increment count and
    set skip-until timestamp if threshold reached.

    Args:
        company_id: Company row ID (None if no company linked).
        success: True if careers page was reachable and returned results.
    """
    if company_id is None:
        return
    with _careers_lock:
        if success:
            _careers_failure_counts.pop(company_id, None)
            _careers_skip_until.pop(company_id, None)
        else:
            count = _careers_failure_counts.get(company_id, 0) + 1
            _careers_failure_counts[company_id] = count
            if count >= _MAX_CAREERS_FAILURES:
                _careers_skip_until[company_id] = datetime.now(timezone.utc) + timedelta(days=_CAREERS_SKIP_DAYS)
                logger.info(
                    "_record_careers_outcome: company %d hit %d failures, skipping for %d days",
                    company_id, count, _CAREERS_SKIP_DAYS,
                )

# ---------------------------------------------------------------------------
# Lightweight per-job liveness helpers (scoring preflight)
# ---------------------------------------------------------------------------

_EXPIRED_BODY_MARKERS = (
    "position filled",
    "position has been filled",
    "no longer accepting",
    "this job is no longer available",
    "job has been removed",
    "this position has been closed",
    "this job has expired",
    "this job posting has expired",
    "this listing has expired",
    "this job listing has expired",
    "this position is no longer open",
    "this role has been filled",
    "this position is no longer available",
    "job no longer available",
    "the position has been filled",
    "this job is closed",
    "this job has been closed",
    "sorry, this position has been filled",
    "this opportunity is no longer available",
)

_EXPIRED_BODY_REGEXES = tuple(re.compile(p) for p in (
    # Glassdoor: "This job from Jul 9, 2025 is no longer available for applications"
    r"this job\b.{0,50}\bis no longer available",
    # "This job posting is no longer accepting applications"
    r"this job\b.{0,30}\bis no longer accepting",
    # Generic date-interpolated: "Posted on <date> - No longer active"
    r"no longer active",
    # "Expired on <date>"
    r"expired\s+on\s+\w+",
))


def quick_liveness_check(url: str, timeout: int = 8) -> str:
    """Lightweight HTTP GET check for a single job URL.

    Used by the scoring preflight to gate Sonnet evaluation. Independent
    from the nightly ATS-specific signal cascade.

    Args:
        url: Job posting URL.
        timeout: Request timeout in seconds.

    Returns:
        EXPIRED if 404/410 or body contains expired markers.
        LIVE if 200 with no expired markers.
        INCONCLUSIVE on error, timeout, or non-standard status.
    """
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (404, 410):
            return EXPIRED
        if resp.status_code == 200:
            body_lower = resp.text[:5000].lower()
            for marker in _EXPIRED_BODY_MARKERS:
                if marker in body_lower:
                    return EXPIRED
            for pattern in _EXPIRED_BODY_REGEXES:
                if pattern.search(body_lower):
                    return EXPIRED
            return LIVE
        return INCONCLUSIVE
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return INCONCLUSIVE
    except Exception as e:
        logger.debug("quick_liveness_check: error for %s: %s", url, e)
        return INCONCLUSIVE


def check_job_liveness(job_row: dict) -> str:
    """Check if a job posting is still live by testing its first source URL.

    Extracts the first URL from source_urls JSON and runs
    quick_liveness_check. Returns INCONCLUSIVE if no URL is available.

    Args:
        job_row: Job row dict (must include source_urls).

    Returns:
        EXPIRED, LIVE, or INCONCLUSIVE.
    """
    source_urls_raw = job_row.get("source_urls", "[]")
    if isinstance(source_urls_raw, str):
        try:
            source_urls = json.loads(source_urls_raw)
        except (json.JSONDecodeError, TypeError):
            source_urls = []
    else:
        source_urls = source_urls_raw or []

    if not source_urls:
        return INCONCLUSIVE

    return quick_liveness_check(source_urls[0])


# ---------------------------------------------------------------------------
# Signal cascade orchestrator
# ---------------------------------------------------------------------------

def _check_job_expiry(
    job: dict,
    company: Optional[dict],
    config: dict,
    skip_careers: bool = False,
) -> tuple[str, str]:
    """Run the signal cascade for a single job.

    Signals checked in order: ATS API -> careers page -> SerpAPI.
    Short-circuits on first definitive answer (EXPIRED or LIVE).

    Args:
        job: Job row dict (must include dedup_key, title, company, source_urls).
        company: Company row dict or None (from companies table join).
        config: Application config dict.
        skip_careers: If True, skip Signal 2 (careers page) due to backoff.

    Returns:
        Tuple of (result, evidence):
            result: EXPIRED, LIVE, or INCONCLUSIVE.
            evidence: Human-readable string describing which signal fired.
    """
    title = job.get("title", "")
    company_name = job.get("company", "")

    # Parse source_urls JSON
    source_urls_raw = job.get("source_urls", "[]")
    if isinstance(source_urls_raw, str):
        try:
            source_urls = json.loads(source_urls_raw)
        except (json.JSONDecodeError, TypeError):
            source_urls = []
    else:
        source_urls = source_urls_raw or []

    # --- Signal 0: Direct URL liveness check ---
    if source_urls:
        url_result = quick_liveness_check(source_urls[0])
        if url_result == EXPIRED:
            return EXPIRED, "url_check expired_markers"
        if url_result == LIVE:
            return LIVE, "url_check 200_ok"
        # INCONCLUSIVE falls through to ATS API

    # --- Signal 1: ATS API Check ---
    if company and company.get("ats_platform") and company.get("ats_slug"):
        platform = company["ats_platform"]
        slug = company["ats_slug"]
        # Try to extract posting ID from source URLs
        posting_id = None
        for url in source_urls:
            posting_id = _extract_posting_id(url, platform)
            if posting_id:
                break

        if posting_id:
            result = _check_ats_api(slug, posting_id, platform)
            if result == EXPIRED:
                return EXPIRED, f"{platform}_api 404"
            if result == LIVE:
                return LIVE, f"{platform}_api 200"

    # --- Signal 2: Careers Page Check ---
    if not skip_careers:
        homepage_url = company.get("homepage_url") if company else None
        target_titles = config.get("profile", {}).get("target_titles", [])
        exclusions = config.get("profile", {}).get("exclusions", {}).get("title_keywords", [])
        careers_result = _check_careers_page(homepage_url, title, target_titles, exclusions)
        if careers_result == LIVE:
            return LIVE, "careers_page title_found"
        # Note: careers_page returning INCONCLUSIVE falls through to Signal 3

    # --- Signal 3: SerpAPI Fallback ---
    serpapi_result = _check_serpapi(title, company_name, config)
    if serpapi_result == EXPIRED:
        return EXPIRED, "serpapi no_match"
    if serpapi_result == LIVE:
        return LIVE, "serpapi match_found"

    return INCONCLUSIVE, ""

# ---------------------------------------------------------------------------
# Public API: Nightly batch runner
# ---------------------------------------------------------------------------

def run_expiry_check(db_path: str, config: dict) -> dict:
    """Run expiry detection on discovered/reviewing jobs.

    Creates its own SQLite connection (thread-safe for APScheduler).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict.

    Returns:
        Dict with keys: checked (int), archived (int), live (int), inconclusive (int).
    """
    expiry_config = config.get("expiry", {})
    if not expiry_config.get("enabled", True):
        return {"checked": 0, "archived": 0, "live": 0, "inconclusive": 0}

    recheck_days = expiry_config.get("recheck_days", 3)

    with standalone_connection(db_path) as conn:
        try:
            # Query candidate jobs: discovered/reviewing, not recently checked.
            # No LIMIT — recheck_days is the natural throttle. On the first run all
            # unchecked jobs are eligible; subsequent nights only rechek stale ones.
            recheck_cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).isoformat()
            rows = conn.execute(
                """SELECT j.*, c.ats_platform, c.ats_slug, c.homepage_url, c.id as company_row_id
                   FROM jobs j
                   LEFT JOIN companies c ON j.company_id = c.id
                   WHERE j.pipeline_status IN ('discovered', 'reviewing')
                     AND (j.expiry_checked_at IS NULL OR j.expiry_checked_at < ?)
                   ORDER BY j.expiry_checked_at IS NULL DESC, j.expiry_checked_at ASC""",
                (recheck_cutoff,),
            ).fetchall()

            from job_finder.db import update_pipeline_status, persist_job_expiry_state

            archived = 0
            live = 0
            inconclusive = 0

            for row in rows:
                job = dict(row)
                company = None
                if job.get("ats_platform"):
                    company = {
                        "ats_platform": job["ats_platform"],
                        "ats_slug": job["ats_slug"],
                        "homepage_url": job.get("homepage_url"),
                        "id": job.get("company_row_id"),
                    }
                elif job.get("homepage_url"):
                    company = {
                        "homepage_url": job["homepage_url"],
                        "ats_platform": None,
                        "ats_slug": None,
                    }

                # Check careers page failure backoff (Signal 2 only)
                company_id = job.get("company_row_id")
                skip_careers = False
                with _careers_lock:
                    skip_until = _careers_skip_until.get(company_id) if company_id else None
                if skip_until and datetime.now(timezone.utc) < skip_until:
                    skip_careers = True
                    logger.debug(
                        "run_expiry_check: skipping careers check for company %s (backoff)",
                        company_id,
                    )

                try:
                    result, evidence = _check_job_expiry(job, company, config, skip_careers=skip_careers)
                except Exception as e:
                    logger.warning("run_expiry_check: error checking %s: %s", job["dedup_key"], e)
                    inconclusive += 1
                    continue

                now = datetime.now(timezone.utc).isoformat()

                # Track careers page outcome for backoff
                if not skip_careers and company_id:
                    if "careers_page" in evidence:
                        _record_careers_outcome(company_id, success=True)
                    elif result == INCONCLUSIVE and company and company.get("homepage_url"):
                        # Careers page was attempted but inconclusive (possible failure)
                        _record_careers_outcome(company_id, success=False)

                if result == EXPIRED:
                    # Persist expiry state via the sole write path
                    persist_job_expiry_state(conn, job["dedup_key"], EXPIRED, now)
                    # update_pipeline_status commits internally (pipeline_events audit trail)
                    update_pipeline_status(
                        conn, job["dedup_key"], "archived",
                        source="expiry_check", evidence=evidence,
                    )
                    archived += 1
                    logger.info("run_expiry_check: archived %s (%s)", job["dedup_key"], evidence)

                elif result == LIVE:
                    persist_job_expiry_state(conn, job["dedup_key"], LIVE, now)
                    live += 1

                else:
                    persist_job_expiry_state(conn, job["dedup_key"], INCONCLUSIVE, now)
                    inconclusive += 1
                    logger.debug("run_expiry_check: inconclusive for %s", job["dedup_key"])

            result_summary = {
                "checked": len(rows),
                "archived": archived,
                "live": live,
                "inconclusive": inconclusive,
            }
            logger.info("run_expiry_check complete: %s", result_summary)
            return result_summary

        except Exception:
            conn.rollback()
            logger.exception("run_expiry_check failed")
            raise
