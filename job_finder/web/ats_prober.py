"""Single-company ATS probing with retry, backoff, and error handling."""

import logging
import sqlite3
import time  # noqa: F401 — available for callers that may need it
from datetime import UTC, datetime, timedelta

import requests

from job_finder.web.ats_detection import derive_slug_candidates

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8  # seconds

# Probe status precedence for upsert conflict resolution (higher = more advanced)
_PROBE_STATUS_PRECEDENCE = {
    "hit": 2,
    "pending": 1,
    "miss": 0,
}

# ---------------------------------------------------------------------------
# Retry state machine constants (DEBT-01 / Phase 14)
# ---------------------------------------------------------------------------

# Backoff schedule: [1hr, 4hr, 24hr] — index = current retry_count before increment
_BACKOFF_HOURS = [1, 4, 24]
_MAX_RETRIES = 3  # After 3 consecutive failures → permanent unreachable miss

# HTTP status codes that indicate transient failures (retry eligible)
_TRANSIENT_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# HTTP status codes that indicate permanent miss (no retry)
_PERMANENT_MISS_CODES: frozenset[int] = frozenset({404, 410})

# ---------------------------------------------------------------------------
# Retry state machine helpers (DEBT-01 / Phase 14)
# ---------------------------------------------------------------------------


def _compute_retry_after(retry_count: int) -> str:
    """Compute UTC ISO timestamp for next retry based on current retry_count.

    Uses _BACKOFF_HOURS schedule: [1hr, 4hr, 24hr].
    retry_count is the count BEFORE the current failure (before incrementing).

    Returns timestamps in SQLite datetime() format ("YYYY-MM-DD HH:MM:SS") so that
    comparisons like retry_after < datetime('now') work correctly in SQL queries.

    Args:
        retry_count: Current retry_count value (0-based index into backoff schedule).

    Returns:
        UTC timestamp string in SQLite-compatible format for SQL datetime comparisons.
    """
    index = min(retry_count, len(_BACKOFF_HOURS) - 1)
    hours = _BACKOFF_HOURS[index]
    dt = datetime.now(UTC) + timedelta(hours=hours)
    # Return in SQLite-compatible UTC format (no timezone offset suffix) for
    # correct comparison with datetime('now') in SQL WHERE clauses
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_transient_error(exc_or_status) -> bool:
    """Return True if the given exception or status code indicates a transient error.

    Args:
        exc_or_status: Either an exception instance or an integer HTTP status code.

    Returns:
        True if the error is transient (should retry), False if permanent.
    """
    if isinstance(exc_or_status, int):
        return exc_or_status in _TRANSIENT_CODES
    # Check for requests exception types indicating transient network issues
    return isinstance(
        exc_or_status,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    )


def _handle_scan_error(
    conn: sqlite3.Connection,
    company_id: int,
    company_name: str,
    error_detail: str,
    now: str,
) -> None:
    """Handle a transient ATS scan/probe error for a company.

    Reads current retry_count from companies table. If retry_count >= _MAX_RETRIES - 1
    (i.e. already had max retries), promotes to permanent miss with miss_reason='unreachable'.
    Otherwise, increments retry_count and sets retry_after using exponential backoff.

    Args:
        conn: Open SQLite connection.
        company_id: Company row ID.
        company_name: Company name (for logging).
        error_detail: Description of the error.
        now: Current UTC ISO timestamp string.
    """
    row = conn.execute("SELECT retry_count FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        logger.warning("_handle_scan_error: company %d not found", company_id)
        return

    current_retry_count = row[0] or 0

    if current_retry_count >= _MAX_RETRIES - 1:
        # 3rd consecutive failure → promote to permanent unreachable miss
        new_retry_count = _MAX_RETRIES
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'miss',
                   miss_reason = 'unreachable',
                   retry_count = ?,
                   updated_at = ?
               WHERE id = ?""",
            (new_retry_count, now, company_id),
        )
        conn.commit()
        logger.info(
            "_handle_scan_error: %s promoted to unreachable after %d failures",
            company_name,
            new_retry_count,
        )
    else:
        # Transient error — increment retry_count, set backoff retry_after
        new_retry_count = current_retry_count + 1
        retry_after = _compute_retry_after(current_retry_count)
        conn.execute(
            """UPDATE companies
               SET ats_probe_status = 'error',
                   retry_count = ?,
                   retry_after = ?,
                   updated_at = ?
               WHERE id = ?""",
            (new_retry_count, retry_after, now, company_id),
        )
        conn.commit()
        logger.info(
            "_handle_scan_error: %s set to error (retry %d/%d), retry_after=%s. Error: %s",
            company_name,
            new_retry_count,
            _MAX_RETRIES,
            retry_after,
            error_detail,
        )


def _reset_retry_state(
    conn: sqlite3.Connection,
    company_id: int,
    now: str,
) -> None:
    """Reset retry state after a successful probe/scan.

    Sets retry_count=0, retry_after=NULL, miss_reason=NULL on the company row.
    Does NOT change ats_probe_status — caller is responsible for setting that.

    Args:
        conn: Open SQLite connection.
        company_id: Company row ID.
        now: Current UTC ISO timestamp string.
    """
    conn.execute(
        """UPDATE companies
           SET retry_count = 0,
               retry_after = NULL,
               miss_reason = NULL,
               updated_at = ?
           WHERE id = ?""",
        (now, company_id),
    )
    conn.commit()


def probe_single_company(
    company_id: int,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Probe a single company's ATS platform and update its state.

    Used by the manual retry route (POST /companies/<id>/retry) to immediately
    re-probe a company in error or unreachable state.

    Uses the caller's conn (Flask request thread g.db) — NOT its own connection.
    This differs from probe_ats_slugs/run_ats_scan which create their own connections.

    Args:
        company_id: The companies table row ID.
        conn: Open SQLite connection (caller's — Flask g.db or test conn).
        config: Application config dict (reads TESTING flag).

    Returns:
        Dict with at minimum a "status" key: "hit", "error", or "miss".
        "hit" also includes "jobs_found". "error" includes "detail".
    """
    now = datetime.now(UTC).isoformat()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company is None:
        return {"status": "miss", "detail": "company not found"}

    platform = company["ats_platform"]
    slug = company["ats_slug"]
    company_name = company["name_raw"]

    # If company has a known platform and slug, probe directly via HTTP
    # (not via scan_lever/scan_greenhouse/scan_ashby which swallow exceptions)
    if platform and slug:
        try:
            if platform == "lever":
                url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            elif platform == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            elif platform == "ashby":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
            elif platform == "smartrecruiters":
                url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
            elif platform == "workday":
                # Workday uses POST — delegate to dedicated probe function
                try:
                    if _probe_workday(slug):
                        conn.execute(
                            "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                            (company_id,),
                        )
                        _reset_retry_state(conn, company_id, now)
                        logger.info("probe_single_company: %s -> hit (workday)", company_name)
                        return {"status": "hit", "jobs_found": 0}
                    else:
                        conn.execute(
                            "UPDATE companies SET ats_probe_status = 'miss' WHERE id = ?",
                            (company_id,),
                        )
                        conn.commit()
                        return {"status": "miss"}
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    _handle_scan_error(conn, company_id, company_name, str(e), now)
                    return {"status": "error", "detail": str(e)}
            else:
                return {"status": "miss", "detail": f"unknown platform: {platform}"}

            # Let Timeout/ConnectionError propagate — caught below as transient
            resp = requests.get(url, timeout=_PROBE_TIMEOUT)

            if resp.status_code == 200:
                # Success: update to hit, reset retry state
                conn.execute(
                    "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                    (company_id,),
                )
                _reset_retry_state(conn, company_id, now)
                try:
                    data = resp.json()
                    jobs_count = len(data) if isinstance(data, list) else 0
                except Exception:
                    logger.debug(
                        "probe jobs_count parse failed for %s", company_name, exc_info=True
                    )
                    jobs_count = 0
                logger.info("probe_single_company: %s -> hit (%d jobs)", company_name, jobs_count)
                return {"status": "hit", "jobs_found": jobs_count}
            elif resp.status_code in _PERMANENT_MISS_CODES:
                conn.execute(
                    "UPDATE companies SET ats_probe_status = 'miss' WHERE id = ?",
                    (company_id,),
                )
                conn.commit()
                return {"status": "miss"}
            elif resp.status_code in _TRANSIENT_CODES:
                detail = f"HTTP {resp.status_code}"
                _handle_scan_error(conn, company_id, company_name, detail, now)
                return {"status": "error", "detail": detail}
            else:
                # Other non-200 — treat as permanent miss
                conn.execute(
                    "UPDATE companies SET ats_probe_status = 'miss' WHERE id = ?",
                    (company_id,),
                )
                conn.commit()
                return {"status": "miss"}

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            _handle_scan_error(conn, company_id, company_name, str(e), now)
            return {"status": "error", "detail": str(e)}
        except Exception as e:
            logger.warning("probe_single_company: %s unexpected error: %s", company_name, e)
            _handle_scan_error(conn, company_id, company_name, str(e), now)
            return {"status": "error", "detail": str(e)}

    else:
        # No platform/slug — try speculative probing via derived slug candidates
        candidates = derive_slug_candidates(company_name)
        for slug_candidate in candidates:
            try:
                if _probe_lever_with_result(slug_candidate):
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'hit',
                               ats_platform = 'lever',
                               ats_slug = ?
                           WHERE id = ?""",
                        (slug_candidate, company_id),
                    )
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_greenhouse(slug_candidate):
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'hit',
                               ats_platform = 'greenhouse',
                               ats_slug = ?
                           WHERE id = ?""",
                        (slug_candidate, company_id),
                    )
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_ashby(slug_candidate):
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'hit',
                               ats_platform = 'ashby',
                               ats_slug = ?
                           WHERE id = ?""",
                        (slug_candidate, company_id),
                    )
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                _handle_scan_error(conn, company_id, company_name, str(e), now)
                return {"status": "error", "detail": str(e)}

        # All candidates exhausted — permanent miss
        conn.execute(
            "UPDATE companies SET ats_probe_status = 'miss' WHERE id = ?",
            (company_id,),
        )
        conn.commit()
        return {"status": "miss"}


def _probe_lever_with_result(slug: str) -> bool:
    """Return True if Lever slug has at least one active posting. Let transient exceptions propagate."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(url, timeout=_PROBE_TIMEOUT)
    if r.status_code == 200:
        data = r.json()
        return isinstance(data, list) and len(data) > 0
    return False


def _probe_lever(slug: str) -> bool:
    """Return True if slug has at least one active Lever posting.

    IMPORTANT (Research Pitfall 2): Lever returns HTTP 200 with empty list
    for invalid slugs AND for valid slugs with no current postings. Only
    cache as 'hit' when response is 200 AND list has at least one posting.

    Args:
        slug: Lever company slug to probe.

    Returns:
        True if the slug is confirmed active on Lever (non-empty postings list).
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            # Per Research Pitfall 2: empty list is NOT a confirmed hit
            return isinstance(data, list) and len(data) > 0
        return False
    except Exception as e:
        logger.debug("_probe_lever('%s') failed: %s", slug, e)
        return False


def _probe_greenhouse(slug: str) -> bool:
    """Return True if slug is a valid Greenhouse board token.

    Greenhouse returns 200 for valid board tokens. 404 for invalid ones.

    Args:
        slug: Greenhouse board token to probe.

    Returns:
        True if the slug resolves to a valid Greenhouse job board.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_greenhouse('%s') failed: %s", slug, e)
        return False


def _probe_workday(slug: str) -> bool:
    """Return True if Workday slug has active job postings.

    Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal").
    Parses subdomain to derive tenant (prefix before ".wd"), then POSTs to
    the standardized Workday CXS jobs API.

    Args:
        slug: Workday slug in "subdomain/board" format.

    Returns:
        True if the API returns 200 with jobPostings data.
    """
    parts = slug.split("/", 1)
    if len(parts) != 2:
        return False
    subdomain, board = parts

    # Derive tenant from subdomain: everything before ".wd" (e.g. "walmart" from "walmart.wd5")
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    try:
        r = requests.post(
            url,
            json={"limit": 1, "offset": 0, "searchText": ""},
            headers={"Content-Type": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_workday('%s') failed: %s", slug, e)
        return False


def _probe_smartrecruiters(slug: str) -> bool:
    """Return True if SmartRecruiters company has active job postings.

    SmartRecruiters exposes a public Posting API (no auth required).
    Returns 200 with {"totalFound": N, "content": [...]} for valid companies.

    API: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1

    Args:
        slug: SmartRecruiters company identifier (e.g. 'LinkedIn3', 'AbbVie').

    Returns:
        True if the slug resolves to a company with active postings.
    """
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    try:
        r = requests.get(
            url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("totalFound", 0) > 0
        return False
    except Exception as e:
        logger.debug("_probe_smartrecruiters('%s') failed: %s", slug, e)
        return False


def _probe_ashby(slug: str) -> bool:
    """Return True if slug is a valid Ashby job board name.

    Note: Ashby slugs are case-sensitive (Research Pitfall 3).
    When probing from company name, the slug is lowercased. If this fails,
    the URL-derived slug (with original casing) should be used instead.

    Args:
        slug: Ashby job board name to probe.

    Returns:
        True if the slug resolves to a valid Ashby job board.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_ashby('%s') failed: %s", slug, e)
        return False
