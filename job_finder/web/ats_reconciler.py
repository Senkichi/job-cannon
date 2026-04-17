"""Batch ATS reconciliation for job staleness detection.

Fetches the full open-postings set for each tracked company with a known
ATS slug, diffs against stored jobs by posting ID, and marks absentees as
expired. Fast: one HTTP request per company instead of one per tracked job.

Architecture:
- Thread-safe: opens its own sqlite3 connection (same pattern as stale_detector).
- Posting-ID set-diff (robust to URL variance: trailing slashes, tracking params).
- Safety guards:
    * Scan exception or empty result → SKIP company (no mass-expire).
    * Workday pagination cap hit → SKIP (can't distinguish truncation from expired).
    * Unknown platform → SKIP silently (iCIMS, Phenom, UKG, custom — Phase C handles).
    * Scan returned postings but none had parseable IDs → SKIP (scan format drift).
- Live jobs: last_seen refreshed + is_stale cleared (prevents downstream false-stale).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from job_finder.db import persist_job_expiry_state, update_pipeline_status
from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.web.ats_platforms import (
    scan_ashby,
    scan_greenhouse,
    scan_lever,
    scan_smartrecruiters,
    scan_workday,
)
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Signal constants (duplicated from expiry_checker to avoid circular import).
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# Pagination cap used inside scan_workday (ats_platforms.py). If a scan
# returns this many postings the board is truncated and reconciliation
# would falsely expire everything past the cap.
_WORKDAY_CAP = 200

# Posting-ID extraction patterns. Applied to BOTH sides of the set-diff
# (scan output source_url, and stored source_urls entries) so the same
# normalization rule governs both — immune to trailing slashes, UTM
# tracking params, etc.
# Lever: UUID in https://jobs.lever.co/<slug>/<uuid>
_LEVER_POSTING_RE = re.compile(r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE)

# Greenhouse: multiple real-world URL shapes exist because companies can
# route their public board to their own careers domain. The posting ID
# (Greenhouse's numeric 'id') is the stable identifier:
#   https://boards.greenhouse.io/<slug>/jobs/<id>           canonical
#   https://job-boards.greenhouse.io/<slug>/jobs/<id>       newer domain
#   https://<company>.com/careers/job/<id>?gh_jid=<id>      self-hosted redirect
#   https://boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>   embed flow
# Accept any of those by trying the path pattern first, then the gh_jid
# query param as a fallback.
_GREENHOUSE_PATH_RE = re.compile(r"greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE)
_GREENHOUSE_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.IGNORECASE)
_GREENHOUSE_EMBED_RE = re.compile(r"[?&]token=(\d+)", re.IGNORECASE)

# Ashby: UUID after the (case-sensitive) slug, https://jobs.ashbyhq.com/<Slug>/<uuid>
_ASHBY_POSTING_RE = re.compile(r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)")

# Workday: canonical IDs look like "Senior-Data-Scientist_JR101664" or "..._R-123456"
# and are the LAST path segment of a myworkdayjobs.com URL. Must be anchored to the
# Workday domain — an unanchored /job/ match picked up garbage from Google search
# redirects (/job/li/m1/1) and ZipRecruiter path fragments (/Job/<slug>/-in-City).
# Also works around scan_workday's URL-construction quirk that produces
# ".../job//job/<location>/<slug>" (double /job/), since the final segment is
# the same identifier either way.
_WORKDAY_POSTING_RE = re.compile(
    r"myworkdayjobs\.com/[^?#]*?/([^/?#]+)(?:/?(?:[?#]|$))", re.IGNORECASE
)

# SmartRecruiters: https://jobs.smartrecruiters.com/<slug>/<id>[-<slug-text>]
# The canonical stable ID is the alphanumeric prefix. Stored URLs often keep
# the trailing `-slug-text` suffix (SEO-friendly) that scan_smartrecruiters
# strips — capture must stop at the first dash so both forms normalize to
# the same ID.
_SMARTRECRUITERS_POSTING_RE = re.compile(
    r"jobs\.smartrecruiters\.com/[^/]+/([A-Za-z0-9_]+)", re.IGNORECASE
)

_SIMPLE_POSTING_ID_PATTERNS: dict[str, re.Pattern] = {
    "lever": _LEVER_POSTING_RE,
    "ashby": _ASHBY_POSTING_RE,
    # Workday regex is kept for potential Phase C use, but Workday is
    # intentionally excluded from _SUPPORTED_PLATFORMS below — see note.
    "workday": _WORKDAY_POSTING_RE,
    "smartrecruiters": _SMARTRECRUITERS_POSTING_RE,
}

# Platforms safe to batch-reconcile. Workday is intentionally excluded:
# scan_workday's pagination can terminate early (observed in e2e returning
# 40 postings for Walmart, which has thousands), leaving live_id_set
# incomplete and causing false-expires. The 200-cap guard only catches the
# upper bound. Workday jobs fall to Phase C per-URL HTTP GET instead.
# TODO: re-enable once scan_workday exposes total-vs-fetched so we can
# skip incomplete scans safely.
_SUPPORTED_PLATFORMS = frozenset({"lever", "ashby", "smartrecruiters", "greenhouse"})


def _extract_posting_id(url: str, platform: str) -> Optional[str]:
    """Extract a stable posting ID from an ATS job URL.

    Greenhouse has multiple URL shapes in the wild (canonical, job-boards
    subdomain, self-hosted redirects with gh_jid=, embed flows with
    token=). Each is tried in order.

    Returns None for unsupported platforms or URLs that don't match any
    pattern for the given platform.
    """
    if platform == "greenhouse":
        for pattern in (_GREENHOUSE_PATH_RE, _GREENHOUSE_GH_JID_RE, _GREENHOUSE_EMBED_RE):
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    pattern = _SIMPLE_POSTING_ID_PATTERNS.get(platform)
    if pattern is None:
        return None
    match = pattern.search(url)
    return match.group(1) if match else None


def _scan_open_postings(platform: str, slug: str) -> list[dict]:
    """Dispatch to the appropriate scan_* with unfiltered title/exclusion lists.

    target_titles=[] and exclusions=[] trigger the 'empty = no filter' branch
    in ats_platforms._title_matches so we receive the FULL open board, not
    a subset matching our current target profile.
    """
    if platform == "lever":
        return scan_lever(slug, [], [])
    if platform == "greenhouse":
        return scan_greenhouse(slug, [], [])
    if platform == "ashby":
        return scan_ashby(slug, [], [])
    if platform == "smartrecruiters":
        return scan_smartrecruiters(slug, [], [])
    if platform == "workday":
        return scan_workday(slug, [], [])
    return []


def reconcile_company(conn, company_row: dict) -> dict:
    """Reconcile tracked jobs for one company against its live ATS board.

    Build live_id_set from scan output; diff tracked jobs by posting ID.

    - Any source_url of a tracked job yields an ID in live_id_set → LIVE
      (persist expiry_status='live', refresh last_seen, clear is_stale).
    - At least one source_url yields a parseable ID but NONE are in live_id_set
      → EXPIRED (persist expiry_status='expired', archive via pipeline_events
      audit trail).
    - No source_urls yielded parseable IDs → unparseable (Phase C cascade
      will handle it later).

    Args:
        conn: Open sqlite3.Connection.
        company_row: dict with 'id', 'ats_platform', 'ats_slug'.

    Returns:
        {'checked', 'live', 'expired', 'unparseable', 'skipped', 'skip_reason'}
    """
    platform = (company_row.get("ats_platform") or "").lower()
    slug = company_row.get("ats_slug") or ""
    company_id = company_row.get("id")

    result = {
        "checked": 0,
        "live": 0,
        "expired": 0,
        "unparseable": 0,
        "skipped": False,
        "skip_reason": None,
    }

    if not platform or not slug or company_id is None:
        result["skipped"] = True
        result["skip_reason"] = "missing_ats_fields"
        return result

    if platform not in _SUPPORTED_PLATFORMS:
        # iCIMS, Phenom, UKG, custom — no scan_* available; Phase C handles these.
        result["skipped"] = True
        result["skip_reason"] = "unsupported_platform"
        return result

    try:
        postings = _scan_open_postings(platform, slug)
    except Exception as e:
        logger.warning("reconcile_company: %s/%s scan raised %s", platform, slug, e)
        result["skipped"] = True
        result["skip_reason"] = f"scan_exception:{type(e).__name__}"
        return result

    if not postings:
        logger.debug("reconcile_company: %s/%s scan returned empty", platform, slug)
        result["skipped"] = True
        result["skip_reason"] = "scan_empty"
        return result

    if platform == "workday" and len(postings) >= _WORKDAY_CAP:
        logger.warning(
            "reconcile_company: workday '%s' returned %d postings (cap %d) — "
            "skipping to avoid false-expire on truncated board",
            slug, len(postings), _WORKDAY_CAP,
        )
        result["skipped"] = True
        result["skip_reason"] = "workday_truncated"
        return result

    live_id_set: set[str] = set()
    for posting in postings:
        pid = _extract_posting_id(posting.get("source_url", ""), platform)
        if pid:
            live_id_set.add(pid)

    if not live_id_set:
        logger.warning(
            "reconcile_company: %s/%s scan returned %d postings but 0 parseable IDs — "
            "skipping (scan URL format may have drifted)",
            platform, slug, len(postings),
        )
        result["skipped"] = True
        result["skip_reason"] = "no_parseable_live_ids"
        return result

    rows = conn.execute(
        """
        SELECT dedup_key, source_urls
        FROM jobs
        WHERE company_id = ?
          AND pipeline_status IN ('discovered', 'reviewing')
          AND (expiry_status IS NULL OR expiry_status != 'expired')
        """,
        (company_id,),
    ).fetchall()

    now = utc_now_iso()

    for row in rows:
        dedup_key = row["dedup_key"]
        source_urls = safe_json_load(row["source_urls"], default=[]) or []

        job_ids: set[str] = set()
        for url in source_urls:
            pid = _extract_posting_id(url, platform)
            if pid:
                job_ids.add(pid)

        result["checked"] += 1

        if not job_ids:
            result["unparseable"] += 1
            continue

        if job_ids & live_id_set:
            persist_job_expiry_state(conn, dedup_key, LIVE, now)
            conn.execute(
                "UPDATE jobs SET last_seen = ?, is_stale = 0 WHERE dedup_key = ?",
                (now, dedup_key),
            )
            conn.commit()
            result["live"] += 1
        else:
            persist_job_expiry_state(conn, dedup_key, EXPIRED, now)
            update_pipeline_status(
                conn, dedup_key, "archived",
                source="ats_reconciler",
                evidence="ats_batch_reconcile missing_from_board",
            )
            result["expired"] += 1
            logger.info(
                "reconcile_company: archived %s (missing from %s/%s board)",
                dedup_key, platform, slug,
            )

    logger.info(
        "reconcile_company: %s/%s checked=%d live=%d expired=%d unparseable=%d",
        platform, slug,
        result["checked"], result["live"], result["expired"], result["unparseable"],
    )
    return result


def reconcile_all_companies(db_path: str, config: dict | None = None) -> dict:
    """Run batch ATS reconciliation across every company with a usable ATS slug.

    Opens its own sqlite3 connection (APScheduler background thread).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict. Unused currently; reserved for
            per-platform tuning (Workday max_results override, etc.).

    Returns:
        {'companies_checked', 'companies_skipped', 'checked', 'live',
         'expired', 'unparseable'}
    """
    summary = {
        "companies_checked": 0,
        "companies_skipped": 0,
        "checked": 0,
        "live": 0,
        "expired": 0,
        "unparseable": 0,
    }

    with standalone_connection(db_path) as conn:
        try:
            companies = conn.execute(
                """
                SELECT id, ats_platform, ats_slug
                FROM companies
                WHERE ats_platform IS NOT NULL
                  AND ats_slug IS NOT NULL
                  AND scan_enabled = 1
                """
            ).fetchall()
        except Exception:
            logger.exception("reconcile_all_companies: failed to query companies")
            return summary

        logger.info(
            "reconcile_all_companies: %d companies with ATS slugs", len(companies),
        )

        for company in companies:
            company_row = dict(company)
            try:
                company_result = reconcile_company(conn, company_row)
            except Exception:
                logger.exception(
                    "reconcile_all_companies: unexpected error for company_id=%s",
                    company_row.get("id"),
                )
                summary["companies_skipped"] += 1
                continue

            if company_result.get("skipped"):
                summary["companies_skipped"] += 1
            else:
                summary["companies_checked"] += 1
                summary["checked"] += company_result["checked"]
                summary["live"] += company_result["live"]
                summary["expired"] += company_result["expired"]
                summary["unparseable"] += company_result["unparseable"]

    logger.info("reconcile_all_companies complete: %s", summary)
    return summary
