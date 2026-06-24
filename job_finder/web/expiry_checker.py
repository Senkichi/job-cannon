"""Job expiry detection and unified staleness orchestrator.

Provides:
    _extract_posting_id   -- Extract individual posting ID from ATS URL
    _check_ats_api        -- Per-posting ATS API liveness check (Lever/GH/Ashby)
    _check_careers_page   -- Company careers page title-search signal
    _check_job_expiry     -- Signal cascade orchestrator for a single job
    quick_liveness_check  -- Lightweight HTTP GET check for a single URL
    check_job_liveness    -- Scoring preflight wrapper around quick_liveness_check
    run_staleness_check   -- Nightly unified orchestrator (B → A → C)

Architecture:
- Thread-safe: creates own sqlite3 connection (same pattern as stale_detector).
- Signal cascade (per job): URL GET → per-posting ATS API → careers-page search.
  SerpAPI was removed: absence from its index is a weak signal that caused false
  positives, and the per-job 30-second timeout dominated runtime.
- Unified orchestrator (run_staleness_check) runs three phases in order:
    Phase B: batch ATS reconciliation (ats_reconciler.reconcile_all_companies)
    Phase C: parallel HTTP cascade over jobs not yet resolved by Phase B
    Phase A: time-based stale marking (stale_detector.run_stale_detection)
  Order matters: B and C both refresh last_seen for verified-live jobs
  (B inline, C via persist_job_expiry_state's live path), so A — the only
  phase that acts on the clock instead of direct evidence — must run last,
  judging against the freshest evidence available.
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

import requests

from job_finder.db import persist_job_expiry_state, update_pipeline_status
from job_finder.json_utils import utc_now_iso
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEOUT = 10  # seconds for HTTP requests inside the cascade

# Default python-requests UA gets bot-walled (challenge pages, 403/999) by
# LinkedIn, Workday, and most aggregators, inflating INCONCLUSIVE and
# false-LIVE counts. A browser UA keeps the checks on the normal page path.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Signal result constants
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# Default concurrency for Phase C (configurable)
_DEFAULT_PARALLEL_WORKERS = 10

# Greenhouse redirects to the board root with ?error=true when a posting is gone.
# (Merged from liveness_checker._GREENHOUSE_ERROR_RE.)
_GREENHOUSE_ERROR_RE = re.compile(r"[?&]error=true")

# ---------------------------------------------------------------------------
# Posting ID extraction
# ---------------------------------------------------------------------------

_LEVER_POSTING_RE = re.compile(r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE)
_GREENHOUSE_POSTING_RE = re.compile(r"boards\.greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE)
_ASHBY_POSTING_RE = re.compile(
    r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"
    # No IGNORECASE — Ashby slugs are case-sensitive
)

_POSTING_PATTERNS = {
    "lever": _LEVER_POSTING_RE,
    "greenhouse": _GREENHOUSE_POSTING_RE,
    "ashby": _ASHBY_POSTING_RE,
}


def _extract_posting_id(url: str, ats_platform: str) -> str | None:
    """Extract the individual posting ID from an ATS URL.

    Used by Signal 1 (per-posting ATS API). Covers the three platforms
    whose APIs accept a posting-id lookup. Workday and SmartRecruiters
    don't expose equivalent single-posting endpoints; they rely on Phase B
    batch reconciliation via job_finder.web.ats_reconciler.
    """
    pattern = _POSTING_PATTERNS.get(ats_platform)
    if pattern is None:
        return None
    match = pattern.search(url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Signal 1: ATS API Check (per-posting)
# ---------------------------------------------------------------------------


def _check_ats_api(slug: str, posting_id: str, ats_platform: str, timeout: int = _TIMEOUT) -> str:
    """Check if a specific job posting is still live via the ATS API."""
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
        resp = fetch_with_deadline(url, getter=requests.get, timeout=timeout, headers=_HEADERS)
        if resp.status_code in (404, 410):
            return EXPIRED
        if resp.status_code == 200:
            return LIVE
        return INCONCLUSIVE
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return INCONCLUSIVE
    except Exception as e:
        logger.warning("_check_ats_api: unexpected error for %s/%s: %s", slug, posting_id, e)
        return INCONCLUSIVE


# ---------------------------------------------------------------------------
# Signal 2: Company Careers Page Check
# ---------------------------------------------------------------------------

# Lazy imports (careers scraper may not be available in all test configurations)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

try:
    from job_finder.web.ats_platforms import _title_matches
except ImportError:
    _title_matches = None  # type: ignore[assignment]


def _check_careers_page(
    homepage_url: str | None,
    job_title: str,
    target_titles: list[str],
    exclusions: list[str],
) -> str:
    """Check if a job title appears on the company's careers page."""
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

        for item in results:
            result_title = item.get("title", "")
            if _title_matches is not None:
                if _title_matches(result_title, [job_title], []):
                    return LIVE
            else:
                if (
                    job_title.lower() in result_title.lower()
                    or result_title.lower() in job_title.lower()
                ):
                    return LIVE

        return INCONCLUSIVE

    except Exception as e:
        logger.debug("_check_careers_page: error checking %s: %s", homepage_url, e)
        return INCONCLUSIVE


# ---------------------------------------------------------------------------
# In-memory careers-page failure tracker (Signal 2 backoff)
# ---------------------------------------------------------------------------

_careers_lock = threading.Lock()
_careers_failure_counts: dict[int, int] = {}
_careers_skip_until: dict[int, datetime] = {}

_MAX_CAREERS_FAILURES = 3
_CAREERS_SKIP_DAYS = 7


def _record_careers_outcome(company_id: int | None, success: bool) -> None:
    """Track careers-page check outcome for backoff logic."""
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
                _careers_skip_until[company_id] = datetime.now(UTC) + timedelta(
                    days=_CAREERS_SKIP_DAYS
                )
                logger.info(
                    "_record_careers_outcome: company %d hit %d failures, skipping for %d days",
                    company_id,
                    count,
                    _CAREERS_SKIP_DAYS,
                )


# ---------------------------------------------------------------------------
# Signal 0: Direct URL liveness check
# ---------------------------------------------------------------------------

# Expired-page body markers. Lowercase — matched case-insensitively via body.lower().
# Merged from liveness_checker._EXPIRED_PATTERNS (the unique strings that
# weren't already here).
_EXPIRED_BODY_MARKERS = (
    "position filled",
    "position has been filled",
    "no longer accepting",
    "this job is no longer available",
    "job has been removed",
    "this job posting has been removed",
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
    "sorry, this job has already been filled",
    "this opportunity is no longer available",
    # Merged from liveness_checker
    "the position you are looking for is no longer available",
    "this requisition is no longer active",
    "job not found",
    "posting not found",
    "this opening has been closed",
    # Greenhouse search-result page when board is empty
    "there are no jobs matching your search",
    # German
    "diese stelle ist nicht mehr verfügbar",
    "diese stelle ist nicht mehr verfugbar",
    "diese position wurde bereits besetzt",
    # French
    "cette offre n'est plus disponible",
    "cette offre n\u2019est plus disponible",
)

_EXPIRED_BODY_REGEXES = tuple(
    re.compile(p)
    for p in (
        r"this job\b.{0,50}\bis no longer available",
        r"this job\b.{0,30}\bis no longer accepting",
        r"no longer active",
        r"expired\s+on\s+\w+",
    )
)


def quick_liveness_check(url: str, timeout: int = 8) -> str:
    """Lightweight HTTP GET check for a single job URL.

    Used by the scoring preflight to gate score-tier evaluation AND by
    Phase C's cascade. Independent from the ATS-specific signals.
    """
    # Greenhouse error-redirect URL — expired boards redirect to ?error=true
    if _GREENHOUSE_ERROR_RE.search(url):
        return EXPIRED

    try:
        resp = fetch_with_deadline(
            url, getter=requests.get, timeout=timeout, allow_redirects=True, headers=_HEADERS
        )
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
    """Check if a job posting is still live by testing its first source URL."""
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
# Signal cascade orchestrator (per job)
# ---------------------------------------------------------------------------


def _check_job_expiry(
    job: dict,
    company: dict | None,
    config: dict,
    skip_careers: bool = False,
) -> tuple[str, str]:
    """Run the signal cascade for a single job.

    Signals in order: direct URL → per-posting ATS API → careers-page search.
    Short-circuits on first definitive answer. Returns (result, evidence).
    """
    title = job.get("title", "")
    timeout = config.get("staleness", {}).get("cascade_request_timeout_seconds", 8)

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
        url_result = quick_liveness_check(source_urls[0], timeout=timeout)
        if url_result == EXPIRED:
            return EXPIRED, "url_check expired_markers"
        if url_result == LIVE:
            return LIVE, "url_check 200_ok"
        # INCONCLUSIVE falls through to Signal 1

    # --- Signal 1: Per-posting ATS API Check ---
    if company and company.get("ats_platform") and company.get("ats_slug"):
        platform = company["ats_platform"]
        slug = company["ats_slug"]
        posting_id = None
        for url in source_urls:
            posting_id = _extract_posting_id(url, platform)
            if posting_id:
                break

        if posting_id:
            result = _check_ats_api(slug, posting_id, platform, timeout=timeout)
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

    return INCONCLUSIVE, ""


# ---------------------------------------------------------------------------
# Parallel cascade worker
# ---------------------------------------------------------------------------


def _cascade_worker(job: dict, company: dict | None, config: dict) -> tuple[str, str, str]:
    """Execute the cascade for one job in a ThreadPoolExecutor worker.

    Returns (dedup_key, result, evidence). Worker path is fully read-only
    against the DB; all writes happen on the orchestrator thread.
    """
    dedup_key = job["dedup_key"]
    company_id = company.get("id") if company else None

    # Check careers-page failure backoff (Signal 2 only)
    skip_careers = False
    with _careers_lock:
        skip_until = _careers_skip_until.get(company_id) if company_id else None
    if skip_until and datetime.now(UTC) < skip_until:
        skip_careers = True

    try:
        result, evidence = _check_job_expiry(job, company, config, skip_careers=skip_careers)
    except Exception as e:
        logger.warning("cascade_worker: error checking %s: %s", dedup_key, e)
        return (dedup_key, INCONCLUSIVE, f"worker_error:{type(e).__name__}")

    # Track careers-page outcome for backoff (thread-safe via _careers_lock)
    if not skip_careers and company_id:
        if "careers_page" in evidence:
            _record_careers_outcome(company_id, success=True)
        elif result == INCONCLUSIVE and company and company.get("homepage_url"):
            _record_careers_outcome(company_id, success=False)

    return (dedup_key, result, evidence)


# ---------------------------------------------------------------------------
# Phase C: parallel HTTP cascade
# ---------------------------------------------------------------------------


def _run_phase_c_cascade(db_path: str, config: dict) -> dict:
    """Parallel cascade for jobs not yet resolved by Phase B.

    Workers run HTTP + regex only; writes happen on the main thread.
    """
    staleness_cfg = config.get("staleness", {})
    legacy_expiry_cfg = config.get("expiry", {})

    recheck_days = staleness_cfg.get(
        "cascade_recheck_days",
        legacy_expiry_cfg.get("recheck_days", 3),
    )
    max_workers = staleness_cfg.get(
        "cascade_parallel_workers",
        _DEFAULT_PARALLEL_WORKERS,
    )

    summary = {"checked": 0, "archived": 0, "live": 0, "inconclusive": 0}

    with standalone_connection(db_path) as conn:
        recheck_cutoff = (datetime.now(UTC) - timedelta(days=recheck_days)).isoformat()
        rows = conn.execute(
            """
            SELECT j.*, c.ats_platform, c.ats_slug, c.homepage_url, c.id AS company_row_id
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.pipeline_status IN ('discovered', 'reviewing')
              AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
              AND (j.expiry_checked_at IS NULL OR j.expiry_checked_at < ?)
            ORDER BY j.expiry_checked_at IS NULL DESC, j.expiry_checked_at ASC
            """,
            (recheck_cutoff,),
        ).fetchall()

        summary["checked"] = len(rows)
        if not rows:
            logger.info("_run_phase_c_cascade: no jobs to check")
            return summary

        # Build work items: (job_dict, company_dict_or_none)
        work_items: list[tuple[dict, dict | None]] = []
        for row in rows:
            job = dict(row)
            company: dict | None = None
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
                    "id": job.get("company_row_id"),
                }
            work_items.append((job, company))

        logger.info(
            "_run_phase_c_cascade: %d jobs, %d workers",
            len(work_items),
            max_workers,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_job = {
                executor.submit(_cascade_worker, job, company, config): job
                for job, company in work_items
            }

            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    dedup_key, result, evidence = future.result()
                except Exception as e:
                    logger.warning(
                        "_run_phase_c_cascade: worker future failed for %s: %s",
                        job.get("dedup_key"),
                        e,
                    )
                    summary["inconclusive"] += 1
                    continue

                now = utc_now_iso()

                if result == EXPIRED:
                    persist_job_expiry_state(conn, dedup_key, EXPIRED, now)
                    update_pipeline_status(
                        conn,
                        dedup_key,
                        "archived",
                        source="expiry_check",
                        evidence=evidence,
                    )
                    summary["archived"] += 1
                    logger.info(
                        "_run_phase_c_cascade: archived %s (%s)",
                        dedup_key,
                        evidence,
                    )
                elif result == LIVE:
                    persist_job_expiry_state(conn, dedup_key, LIVE, now)
                    summary["live"] += 1
                else:
                    persist_job_expiry_state(conn, dedup_key, INCONCLUSIVE, now)
                    summary["inconclusive"] += 1

    logger.info("_run_phase_c_cascade complete: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Public API: unified staleness orchestrator
# ---------------------------------------------------------------------------


def run_staleness_check(db_path: str, config: dict) -> dict:
    """Unified nightly staleness orchestrator.

    Runs three phases in order:
        Phase B: batch ATS reconciliation (one HTTP call per company)
        Phase C: parallel HTTP cascade for jobs not resolved by Phase B
        Phase A: time-based stale marking + passive-stage archive

    Order matters: Phases B and C both refresh last_seen for verified-live
    jobs (B inline, C via persist_job_expiry_state). Phase A is the only
    phase that infers from the clock rather than direct evidence, so it
    runs last — a job HTTP-verified live tonight must not be stale-marked
    or clock-archived tonight.
    """
    staleness_cfg = config.get("staleness", {})
    if not staleness_cfg.get("enabled", True):
        legacy_expiry_cfg = config.get("expiry", {})
        if not legacy_expiry_cfg.get("enabled", True):
            logger.info("run_staleness_check: disabled via config")
            return {
                "phase_b": {},
                "phase_a": {},
                "phase_c": {},
                "disabled": True,
            }

    summary: dict = {"phase_b": {}, "phase_a": {}, "phase_c": {}}

    # --- Phase B: batch ATS reconciliation ---
    if staleness_cfg.get("batch_ats_enabled", True):
        try:
            from job_finder.web.ats_reconciler import reconcile_all_companies

            summary["phase_b"] = reconcile_all_companies(db_path, config)
        except Exception:
            logger.exception("run_staleness_check: Phase B failed")
            summary["phase_b"] = {"error": True}
    else:
        logger.info("run_staleness_check: Phase B disabled via config")

    # --- Phase C: parallel HTTP cascade ---
    try:
        summary["phase_c"] = _run_phase_c_cascade(db_path, config)
    except Exception:
        logger.exception("run_staleness_check: Phase C failed")
        summary["phase_c"] = {"error": True}

    # --- Phase A: time-based stale / archive (last: judges on the evidence
    # B and C just refreshed) ---
    try:
        from job_finder.web.stale_detector import run_stale_detection

        summary["phase_a"] = run_stale_detection(db_path, config)
    except Exception:
        logger.exception("run_staleness_check: Phase A failed")
        summary["phase_a"] = {"error": True}

    logger.info("run_staleness_check complete: %s", summary)
    return summary
