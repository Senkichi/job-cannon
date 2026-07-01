"""Single-company ATS probing with retry, backoff, and error handling."""

import logging
import sqlite3
import time  # noqa: F401 — available for callers that may need it
from datetime import UTC, datetime, timedelta

import requests

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_detection import derive_slug_candidates
from job_finder.web.brand_blocklist import is_blocked_brand

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
    now = utc_now_iso()

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
                        # B4: Workday probe returns False when tenant/board
                        # combination yields no postings — symptomatically the
                        # same as 404 (slug doesn't resolve to a Workday board).
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   miss_reason = 'platform_slug_404'
                               WHERE id = ?""",
                            (company_id,),
                        )
                        conn.commit()
                        return {"status": "miss"}
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    _handle_scan_error(conn, company_id, company_name, str(e), now)
                    return {"status": "error", "detail": str(e)}
            elif platform == "icims":
                # iCIMS is JS-rendered with no public API (issue #454) —
                # delegate to the requests-light existence probe. A 'hit' here
                # only confirms the board is live; the Playwright scanner phase
                # (run_ats_scan) does the actual job extraction.
                try:
                    if _probe_icims(slug):
                        conn.execute(
                            "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                            (company_id,),
                        )
                        _reset_retry_state(conn, company_id, now)
                        logger.info("probe_single_company: %s -> hit (icims)", company_name)
                        return {"status": "hit", "jobs_found": 0}
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'miss',
                               miss_reason = 'platform_slug_404'
                           WHERE id = ?""",
                        (company_id,),
                    )
                    conn.commit()
                    return {"status": "miss"}
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    _handle_scan_error(conn, company_id, company_name, str(e), now)
                    return {"status": "error", "detail": str(e)}
            elif platform == "successfactors":
                # SuccessFactors XML feed — delegate to the probe function.
                try:
                    if _probe_successfactors(slug):
                        conn.execute(
                            "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                            (company_id,),
                        )
                        _reset_retry_state(conn, company_id, now)
                        logger.info(
                            "probe_single_company: %s -> hit (successfactors)", company_name
                        )
                        return {"status": "hit", "jobs_found": 0}
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'miss',
                               miss_reason = 'platform_slug_404'
                           WHERE id = ?""",
                        (company_id,),
                    )
                    conn.commit()
                    return {"status": "miss"}
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    _handle_scan_error(conn, company_id, company_name, str(e), now)
                    return {"status": "error", "detail": str(e)}
            elif platform == "adp":
                # ADP Workforce Now JSON feed — delegate to the probe function.
                try:
                    if _probe_adp(slug):
                        conn.execute(
                            "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
                            (company_id,),
                        )
                        _reset_retry_state(conn, company_id, now)
                        logger.info("probe_single_company: %s -> hit (adp)", company_name)
                        return {"status": "hit", "jobs_found": 0}
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'miss',
                               miss_reason = 'platform_slug_404'
                           WHERE id = ?""",
                        (company_id,),
                    )
                    conn.commit()
                    return {"status": "miss"}
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    _handle_scan_error(conn, company_id, company_name, str(e), now)
                    return {"status": "error", "detail": str(e)}
            else:
                # B4: platform value is set but not one we know how to probe —
                # ATS catalog drift (probably the company's platform was
                # removed from the supported set after being tagged).
                return {
                    "status": "miss",
                    "detail": f"unknown platform: {platform}",
                    "miss_reason": "unknown_platform",
                }

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
                # B4: 404/410 -> tenant not found on this platform.
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'platform_slug_404'
                       WHERE id = ?""",
                    (company_id,),
                )
                conn.commit()
                return {"status": "miss"}
            elif resp.status_code in _TRANSIENT_CODES:
                detail = f"HTTP {resp.status_code}"
                _handle_scan_error(conn, company_id, company_name, detail, now)
                return {"status": "error", "detail": detail}
            else:
                # Other non-200 (403, 401, etc.) — treat as permanent miss.
                # B4: distinct reason so audits can tell 404 (slug doesn't exist)
                # apart from 403/blocked (slug exists but probe blocked).
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'platform_slug_blocked'
                       WHERE id = ?""",
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
        # No platform/slug — try speculative probing via derived slug candidates.
        # F8: short-circuit famous-brand names (Shopify, Walmart, ...) — the
        # speculative ladder produces ~29% FPs on these because slug-collisions
        # with small-company ATS tenants are common. See brand_blocklist.py.
        if is_blocked_brand(company_name):
            logger.info("probe_single_company: %s blocked by brand blocklist", company_name)
            conn.execute(
                """UPDATE companies
                   SET ats_probe_status='miss', miss_reason='blocked_brand'
                   WHERE id=?""",
                (company_id,),
            )
            conn.commit()
            return {"status": "miss", "detail": "blocked_brand"}
        candidates = derive_slug_candidates(company_name)
        for slug_candidate in candidates:
            try:
                if _probe_lever_with_result(slug_candidate):
                    try:
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'hit',
                                   ats_platform = 'lever',
                                   ats_slug = ?
                               WHERE id = ?""",
                            (slug_candidate, company_id),
                        )
                    except sqlite3.IntegrityError as ie:
                        # m076's UNIQUE(ats_platform, ats_slug) gate. The
                        # slug we just probed is already owned by another
                        # company; leave ats_slug at its current value and
                        # keep walking the candidate list.
                        logger.warning(
                            "probe_single_company: collision lever/%s for %s — "
                            "leaving existing ats_slug. exc=%s",
                            slug_candidate,
                            company_name,
                            ie,
                        )
                        continue
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_greenhouse(slug_candidate):
                    try:
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'hit',
                                   ats_platform = 'greenhouse',
                                   ats_slug = ?
                               WHERE id = ?""",
                            (slug_candidate, company_id),
                        )
                    except sqlite3.IntegrityError as ie:
                        logger.warning(
                            "probe_single_company: collision greenhouse/%s for %s "
                            "— leaving existing ats_slug. exc=%s",
                            slug_candidate,
                            company_name,
                            ie,
                        )
                        continue
                    _reset_retry_state(conn, company_id, now)
                    return {"status": "hit", "jobs_found": 0}
                if _probe_ashby(slug_candidate):
                    try:
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'hit',
                                   ats_platform = 'ashby',
                                   ats_slug = ?
                               WHERE id = ?""",
                            (slug_candidate, company_id),
                        )
                    except sqlite3.IntegrityError as ie:
                        logger.warning(
                            "probe_single_company: collision ashby/%s for %s — "
                            "leaving existing ats_slug. exc=%s",
                            slug_candidate,
                            company_name,
                            ie,
                        )
                        continue
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


def _probe_oracle_cloud(slug: str) -> bool:
    """Return True if slug resolves to a live Oracle Recruiting Cloud (Fusion CE) site.

    Slug format: ``"{host}|{site}"`` (e.g. ``"ehmk.fa.us2.oraclecloud.com|CX_1"``).
    GETs the public Candidate-Experience REST finder for a single requisition; a
    200 means the pod + site resolve. Oracle's finder uses literal ``;,=``
    delimiters, so the query string is built by hand (mirrors the scanner's
    ``_fetch_postings`` — ``params=`` would percent-encode and break the finder).
    """
    host, _, site = (slug or "").partition("|")
    host = host.strip()
    site = site.strip() or "CX_1"
    if not host:
        return False
    url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&finder=findReqs;siteNumber={site},limit=1,offset=0"
    )
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_oracle_cloud('%s') failed: %s", slug, e)
        return False


def _probe_ultipro(slug: str) -> bool:
    """Return True if slug resolves to a live UKG Pro Recruiting (UltiPro) board.

    Slug format: ``"{host}/{tenant}/{board}"`` (e.g.
    ``"recruiting2.ultipro.com/JAN1000JANI/<board-guid>"``). POSTs the public
    ``LoadSearchResults`` search endpoint with a minimal ``Top=1`` body; a 200
    means the board GUID resolves (empty boards still return 200).
    """
    parts = (slug or "").split("/")
    if len(parts) < 3 or not all(parts[:3]):
        return False
    host, tenant, board = parts[0], parts[1], parts[2]
    url = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    body = {
        "opportunitySearch": {
            "Top": 1,
            "Skip": 0,
            "QueryString": "",
            "OrderBy": [],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "WorkExperiences": [],
            "DegreeFlexFields": [],
            "IsCurrentlyEmployed": False,
            "IsWillingToRelocate": False,
            "IsWillingToTravel": False,
            "EmploymentDesiredFlexFields": [],
        },
    }
    try:
        r = requests.post(
            url, json=body, headers={"Content-Type": "application/json"}, timeout=_PROBE_TIMEOUT
        )
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_ultipro('%s') failed: %s", slug, e)
        return False


def _probe_icims(slug: str) -> bool:
    """Return True if slug resolves to a live iCIMS career portal.

    iCIMS boards are 100% JS-rendered with no public unauthenticated JSON API
    (issue #454), so the probe only confirms the board *exists*: an HTTP GET
    of the portal's ``/jobs/search`` page returning 200 with an iCIMS marker
    in the body. The full JS render + job extraction is the Playwright
    scanner's job (``ats_platforms/_platforms_icims.py``) — keeping the probe
    requests-light avoids paying a browser launch just to confirm liveness.

    Tries the ``careers-`` host first, then ``jobs-`` (both prefixes are in
    active use across tenants). ``allow_redirects=True`` so tenants whose
    portal redirects to a branded subpath still register as live.

    Args:
        slug: iCIMS tenant subdomain (e.g. 'acme' for careers-acme.icims.com).

    Returns:
        True if the slug resolves to a live iCIMS portal.
    """
    for prefix in ("careers", "jobs"):
        url = f"https://{prefix}-{slug}.icims.com/jobs/search"
        try:
            r = requests.get(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        except Exception as e:
            logger.debug("_probe_icims('%s', prefix=%s) failed: %s", slug, prefix, e)
            continue
        if r.status_code == 200 and "icims" in r.text.lower():
            return True
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


def _probe_recruitee(slug: str) -> bool:
    """Return True if slug has at least one active Recruitee offer.

    Recruitee may return 200 with an empty offers list for an inactive
    company (analogous to Lever's Research Pitfall 2), so the probe only
    confirms 'hit' on non-empty offers.

    Args:
        slug: Recruitee subdomain (e.g. 'acme' for acme.recruitee.com).

    Returns:
        True if the slug resolves to a Recruitee company with active offers.
    """
    url = f"https://{slug}.recruitee.com/api/offers/"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        offers = data.get("offers") if isinstance(data, dict) else None
        return isinstance(offers, list) and len(offers) > 0
    except Exception as e:
        logger.debug("_probe_recruitee('%s') failed: %s", slug, e)
        return False


def _probe_breezy(slug: str) -> bool:
    """Return True if slug has at least one active Breezy posting.

    Breezy returns 200 with an empty list for valid-but-empty tenants
    (same pitfall pattern as Lever/Recruitee), so the probe requires
    a non-empty list to confirm 'hit'.

    Args:
        slug: Breezy subdomain (e.g. 'acme' for acme.breezy.hr).

    Returns:
        True if the slug resolves to a Breezy company with active positions.
    """
    url = f"https://{slug}.breezy.hr/json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            positions = data.get("positions") or data.get("jobs") or []
            return isinstance(positions, list) and len(positions) > 0
        return False
    except Exception as e:
        logger.debug("_probe_breezy('%s') failed: %s", slug, e)
        return False


def _probe_jazzhr(slug: str) -> bool:
    """Return True if slug has at least one active JazzHR posting.

    Same empty-list pitfall pattern — non-empty list required for 'hit'.

    Args:
        slug: JazzHR subdomain (e.g. 'acme' for acme.applytojob.com).

    Returns:
        True if the slug resolves to a JazzHR tenant with active jobs.
    """
    url = f"https://{slug}.applytojob.com/apply/jobs/feed"
    try:
        r = requests.get(url, params={"json": "1"}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            jobs = data.get("jobs") or []
            return isinstance(jobs, list) and len(jobs) > 0
        return False
    except Exception as e:
        logger.debug("_probe_jazzhr('%s') failed: %s", slug, e)
        return False


def _probe_phenom(slug: str) -> bool:
    """Return True if Phenom slug has a valid sitemap with job URLs.

    Phenom does not expose a public JSON API. The probe checks if the
    sitemap index exists and contains at least one sitemap with job URLs.
    Uses the locale-aware sitemap discovery from the scanner module.

    Args:
        slug: Phenom careers host (e.g. 'careers.conduent.com').

    Returns:
        True if the slug resolves to a valid Phenom site with job listings.
    """
    from bs4 import BeautifulSoup

    from job_finder.web.ats_platforms._platforms_phenom import _sitemap_index_url

    try:
        sitemap_index_url = _sitemap_index_url(slug)
        r = requests.get(sitemap_index_url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False

        soup = BeautifulSoup(r.text, "xml")
        sitemap_locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

        # Check first sitemap for job URLs
        for sitemap_url in sitemap_locs[:3]:  # Check first 3 sitemaps
            try:
                sr = requests.get(sitemap_url, timeout=_PROBE_TIMEOUT)
                if sr.status_code == 200:
                    ssoup = BeautifulSoup(sr.text, "xml")
                    job_locs = [loc.get_text(strip=True) for loc in ssoup.find_all("loc")]
                    if any("/job/" in url for url in job_locs):
                        return True
            except Exception:
                continue

        return False
    except Exception as e:
        logger.debug("_probe_phenom('%s') failed: %s", slug, e)
        return False


def _probe_pinpoint(slug: str) -> bool:
    """Return True if slug has at least one active Pinpoint posting.

    Pinpoint may return 200 with ``{"data": []}`` for tenants without active
    postings — empty-list pitfall, same as Lever/Recruitee.

    Args:
        slug: Pinpoint subdomain (e.g. 'workwithus' for workwithus.pinpointhq.com).

    Returns:
        True if the slug resolves to a Pinpoint tenant with active postings.
    """
    url = f"https://{slug}.pinpointhq.com/postings.json"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        postings = data.get("data") or []
        return isinstance(postings, list) and len(postings) > 0
    except Exception as e:
        logger.debug("_probe_pinpoint('%s') failed: %s", slug, e)
        return False


def _probe_personio(slug: str) -> bool:
    """Return True if slug has at least one active Personio position.

    Personio publishes XML at .de OR .com; this probe tries .de first then
    falls back to .com on 404. A valid feed with at least one <position> is
    a hit; empty <workzag-jobs> stays a miss (same pitfall pattern).

    Args:
        slug: Personio subdomain (e.g. 'acme' for acme.jobs.personio.de).

    Returns:
        True if the slug resolves to a Personio tenant with active positions.
    """
    for tld in ("de", "com"):
        url = f"https://{slug}.jobs.personio.{tld}/xml"
        try:
            r = requests.get(url, timeout=_PROBE_TIMEOUT)
        except Exception as e:
            logger.debug("_probe_personio('%s', tld=%s) failed: %s", slug, tld, e)
            continue
        if r.status_code == 404:
            continue
        if r.status_code != 200 or not r.content:
            continue
        # Parse cheaply — any <position> element is enough to confirm a hit.
        try:
            import defusedxml.ElementTree as ET

            root = ET.fromstring(r.content)
            for _ in root.iter("position"):
                return True
            return False
        except Exception as e:
            logger.debug("_probe_personio('%s', tld=%s) parse error: %s", slug, tld, e)
            continue
    return False


def _probe_bamboohr(slug: str) -> bool:
    """Return True if slug has at least one active BambooHR posting.

    Probes the public careers widget at /jobs/embed2.php and counts
    ``<li id="bhrPositionID_...">`` items. Tenants without open jobs serve
    a 200 with an empty widget — empty-list pitfall pattern.

    Args:
        slug: BambooHR subdomain (e.g. 'acme' for acme.bamboohr.com).

    Returns:
        True if the slug resolves to a BambooHR tenant with active jobs.
    """
    url = f"https://{slug}.bamboohr.com/jobs/embed2.php"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        # Substring check avoids loading a full HTML parser just for the probe.
        return "bhrPositionID_" in r.text
    except Exception as e:
        logger.debug("_probe_bamboohr('%s') failed: %s", slug, e)
        return False


def _probe_teamtailor(slug: str) -> bool:
    """Return True if slug has at least one active Teamtailor posting.

    Probes the public unkeyed JSON:API at /api/jobs. Tenants without active
    jobs return ``{"data": []}`` — empty-list pitfall, same as others.

    Args:
        slug: Teamtailor subdomain (e.g. 'acme' for acme.teamtailor.com).

    Returns:
        True if the slug resolves to a Teamtailor tenant with active jobs.
    """
    url = f"https://{slug}.teamtailor.com/api/jobs"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        items = data.get("data") or []
        return isinstance(items, list) and len(items) > 0
    except Exception as e:
        logger.debug("_probe_teamtailor('%s') failed: %s", slug, e)
        return False


def _probe_workable(slug: str) -> bool:
    """Return True if slug resolves to a Workable tenant with active jobs.

    Probes the public widget endpoint
    ``https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true``
    which returns ``{"name": ..., "jobs": [...]}``. Empty-jobs path is a
    miss (same pitfall pattern as Lever/Recruitee/etc.).

    Args:
        slug: Workable account slug (first path segment of apply.workable.com URL).
    """
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    try:
        r = requests.get(url, params={"details": "true"}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        jobs = data.get("jobs") or []
        return isinstance(jobs, list) and len(jobs) > 0
    except Exception as e:
        logger.debug("_probe_workable('%s') failed: %s", slug, e)
        return False


def _probe_jobvite(slug: str) -> bool:
    """Return True if slug resolves to a live Jobvite hosted career page.

    Jobvite has no public unauthenticated JSON API; this probe only
    verifies that ``https://jobs.jobvite.com/{slug}`` resolves to a 200
    page (which it does for any active tenant, including those whose
    careers redirect to a custom domain). A 200 here is necessary but
    not sufficient for a real scanner -- see
    ``_platforms_jobvite.py`` for the stub scanner rationale.

    Args:
        slug: Jobvite tenant slug (first path segment of jobs.jobvite.com URL).
    """
    url = f"https://jobs.jobvite.com/{slug}"
    try:
        # allow_redirects=True so custom-domain tenants (e.g. Victaulic ->
        # careers.victaulic.com) still register as live.
        r = requests.get(url, timeout=_PROBE_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except Exception as e:
        logger.debug("_probe_jobvite('%s') failed: %s", slug, e)
        return False


def _probe_paylocity(guid: str) -> bool:
    """Return True if guid resolves to a Paylocity tenant with active jobs.

    Probes the public v2 feed at
    ``https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}``
    which returns ``{"organization": ..., "jobs": [...]}``. Empty-jobs is a miss.

    Args:
        guid: Paylocity tenant GUID (UUID-shaped, extracted from
            ``/recruiting/jobs/All/{guid}`` careers URL).
    """
    url = f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}"
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        jobs = data.get("jobs") or []
        return isinstance(jobs, list) and len(jobs) > 0
    except Exception as e:
        logger.debug("_probe_paylocity('%s') failed: %s", guid, e)
        return False


def _probe_rippling(slug: str) -> bool:
    """Return True if slug resolves to a Rippling tenant with active jobs.

    Probes the public v2 board API at
    ``https://ats.rippling.com/api/v2/board/{slug}/jobs`` which returns
    ``{"items": [...], "page": N, ...}``. Empty-items is a miss.

    Args:
        slug: Rippling board slug (first path segment of ats.rippling.com URL).
    """
    url = f"https://ats.rippling.com/api/v2/board/{slug}/jobs"
    try:
        r = requests.get(url, params={"pageSize": 1}, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        items = data.get("items") or []
        return isinstance(items, list) and len(items) > 0
    except Exception as e:
        logger.debug("_probe_rippling('%s') failed: %s", slug, e)
        return False


def _probe_successfactors(slug: str) -> bool:
    """Return True if slug resolves to a live SuccessFactors job board.

    Slug format: ``"{host}|{company_id}"`` (e.g. ``"career2.successfactors.eu|SwissRe"``).
    Fetches the public XML feed at
    ``https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML``
    and returns True only if the body contains ``<Job-Listing>`` AND at least one
    ``<Job>`` (or ``<JobTitle>``). This avoids false positives on plain SEO sitemaps.

    Args:
        slug: SuccessFactors slug in "host|company_id" format.
    """
    try:
        host, company_id = slug.split("|")
    except ValueError:
        logger.debug("_probe_successfactors('%s'): invalid slug format", slug)
        return False

    url = (
        f"https://{host}/career?company={company_id}&career_ns=job_listing_summary&resultType=XML"
    )
    try:
        r = requests.get(url, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        # Check for job-bearing content, not just "valid XML"
        content = r.text
        return "<Job-Listing" in content and ("<Job>" in content or "<JobTitle>" in content)
    except Exception as e:
        logger.debug("_probe_successfactors('%s') failed: %s", slug, e)
        return False


def _probe_adp(slug: str) -> bool:
    """Return True if slug resolves to a live ADP Workforce Now job board.

    Slug format: client ID UUID (e.g. ``"a6717ebc-f6a8-4a51-856b-f7ebd573645e"``).
    Fetches the public JSON feed at
    ``https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions``
    with ``cid={slug}`` and returns True only if the response contains at least one
    ``jobRequisitions`` item. This avoids false positives on unrelated endpoints.

    Args:
        slug: ADP client ID UUID.
    """
    url = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    params = {
        "cid": slug,
        "ccId": "19000101_000001",
        "lang": "en_US",
        "locale": "en_US",
    }
    try:
        r = requests.get(url, params=params, timeout=_PROBE_TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        if not isinstance(data, dict):
            return False
        reqs = data.get("jobRequisitions") or []
        return isinstance(reqs, list) and len(reqs) > 0
    except Exception as e:
        logger.debug("_probe_adp('%s') failed: %s", slug, e)
        return False
