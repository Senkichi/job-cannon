"""Batch ATS reconciliation for job staleness detection.

Fetches the full open-postings set for each tracked company with a known
ATS slug, diffs against stored jobs by posting ID, and marks absentees as
expired. Fast: one HTTP request per company instead of one per tracked job.

Architecture:
- Thread-safe: opens its own sqlite3 connection (same pattern as stale_detector).
- Posting-ID set-diff (robust to URL variance: trailing slashes, tracking params).
- Safety guards:
    * Scan exception or empty result → SKIP company (no mass-expire).
    * Workday incomplete board (total > fetch cap) → SKIP (completeness gate).
    * Unknown platform → SKIP silently (iCIMS, Phenom, UKG, custom — Phase C handles).
    * Scan returned postings but none had parseable IDs → SKIP (scan format drift).
- Live jobs: last_seen refreshed + is_stale cleared (prevents downstream false-stale).
"""

from __future__ import annotations

import logging
import re

from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.web.ats_platforms import SCANNERS_BY_NAME
from job_finder.web.ats_platforms._registry import run_platform_scan
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Signal constants (duplicated from expiry_checker to avoid circular import).
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

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
    # Workday: used by _extract_posting_id on stored source_urls (both sides of
    # the set-diff must use the same normalization rule).
    "workday": _WORKDAY_POSTING_RE,
    "smartrecruiters": _SMARTRECRUITERS_POSTING_RE,
}

# Platforms safe to batch-reconcile.
# Workday is included but uses a completeness-gated path in reconcile_company
# (_workday_live_id_set) instead of run_platform_scan, so boards too large to
# fully paginate are skipped rather than falsely expiring unseen postings.
_RECONCILABLE_PLATFORMS: frozenset[str] = frozenset(
    {"lever", "ashby", "smartrecruiters", "greenhouse", "workday"}
)


def _extract_posting_id(url: str, platform: str) -> str | None:
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
    """Dispatch to the registered scanner with unfiltered title/exclusion lists."""
    if platform not in _RECONCILABLE_PLATFORMS:
        return []
    return run_platform_scan(SCANNERS_BY_NAME[platform], slug, [], [])


def _workday_live_id_set(slug: str) -> tuple[set[str], bool]:
    """Fetch Workday postings and derive the set of live posting IDs.

    Uses the completeness-aware CXS list fetch directly — does **not** trigger
    per-posting description GETs, which are unnecessary for set-diff
    reconciliation and would waste significant HTTP budget on large boards.

    Returns:
        ``(live_ids, complete)`` where ``complete`` is ``False`` when the
        board is too large to fully paginate or the scan encountered an error
        before the first page arrived.  IDs are the last path-segment of
        ``externalPath`` (e.g. ``"Senior-Data-Scientist_R-12345"``), which
        matches what ``_extract_posting_id(stored_url, "workday")`` returns
        from stored ``source_urls``.
    """
    from job_finder.web.ats_platforms._platforms_workday import _fetch_postings_with_completeness

    postings, complete = _fetch_postings_with_completeness(slug)
    live_ids: set[str] = set()
    for posting in postings:
        external_path = posting.get("externalPath", "")
        if external_path:
            # externalPath is "/job/Title_R-12345" or bare "Title_R-12345";
            # the last segment is the stable per-posting identifier that
            # matches what _WORKDAY_POSTING_RE extracts from stored source_urls.
            segment = external_path.rstrip("/").rsplit("/", 1)[-1]
            if segment:
                live_ids.add(segment)
    return live_ids, complete


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

    if platform not in _RECONCILABLE_PLATFORMS:
        # iCIMS, Phenom, UKG, custom — no scan_* available; Phase C handles these.
        result["skipped"] = True
        result["skip_reason"] = "unsupported_platform"
        return result

    # Build live_id_set.  Strategy differs by platform:
    # - Workday: completeness-aware direct fetch (no description GETs needed).
    # - Others:  standard run_platform_scan, IDs extracted from source_urls.
    live_id_set: set[str] = set()

    if platform == "workday":
        try:
            live_id_set, complete = _workday_live_id_set(slug)
        except Exception as e:
            logger.warning("reconcile_company: %s/%s scan raised %s", platform, slug, e)
            result["skipped"] = True
            result["skip_reason"] = f"scan_exception:{type(e).__name__}"
            return result

        if not complete:
            logger.warning(
                "reconcile_company: workday '%s' board incomplete — "
                "skipping to avoid false-expire",
                slug,
            )
            result["skipped"] = True
            result["skip_reason"] = "workday_incomplete"
            return result

        if not live_id_set:
            logger.debug("reconcile_company: workday '%s' scan returned empty", slug)
            result["skipped"] = True
            result["skip_reason"] = "scan_empty"
            return result

    else:
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

        for posting in postings:
            pid = _extract_posting_id(posting.get("source_url", ""), platform)
            if pid:
                live_id_set.add(pid)

        if not live_id_set:
            logger.warning(
                "reconcile_company: %s/%s scan returned %d postings but 0 parseable IDs — "
                "skipping (scan URL format may have drifted)",
                platform,
                slug,
                len(postings),
            )
            result["skipped"] = True
            result["skip_reason"] = "no_parseable_live_ids"
            return result

    rows = conn.execute(
        """
        SELECT dedup_key, source_urls, source_id
        FROM jobs
        WHERE company_id = ?
          AND pipeline_status IN ('discovered', 'reviewing')
          AND (expiry_status IS NULL OR expiry_status != 'expired')
        """,
        (company_id,),
    ).fetchall()

    now = utc_now_iso()

    # Phase 1: classify every tracked job into live | expired | unparseable.
    # No writes here — read-only set membership check.
    live_keys: list[str] = []
    expired_keys: list[str] = []

    # source_id fallback (issue #218): for the aggregator-ingested cohort
    # (Gmail alerts whose URLs carry no ATS posting-id segment), the stored
    # `source_id` column may still hold the platform-native id when the same
    # job was also seen by the ATS scanner. Gated to platforms whose scanner
    # persists `source_id` in the live-board namespace (greenhouse numeric id,
    # smartrecruiters id). Lever/ashby/workday use UUID/path-segment ids that
    # the scanner does not store in `source_id`, so a blind union would risk
    # a cross-namespace false-positive.
    _SOURCE_ID_FALLBACK_PLATFORMS = {"greenhouse", "smartrecruiters"}
    source_id_fallback = platform in _SOURCE_ID_FALLBACK_PLATFORMS

    for row in rows:
        dedup_key = row["dedup_key"]
        source_urls = safe_json_load(row["source_urls"], default=[]) or []

        job_ids: set[str] = set()
        for url in source_urls:
            pid = _extract_posting_id(url, platform)
            if pid:
                job_ids.add(pid)

        if not job_ids and source_id_fallback:
            sid = (row["source_id"] or "").strip()
            if sid:
                job_ids.add(sid)

        result["checked"] += 1

        if not job_ids:
            result["unparseable"] += 1
            continue

        if job_ids & live_id_set:
            live_keys.append(dedup_key)
        else:
            expired_keys.append(dedup_key)

    # Phase 2: apply all writes in a single transaction so the SQLite writer
    # is acquired once per company instead of once (or twice) per job-row.
    # The previous per-row commit pattern blew past the 2-retry budget in
    # persist_job_expiry_state under concurrent-write contention (orphan +
    # registry + staleness running in the same window). Inlining the SQL
    # bypasses the helpers' internal commits without changing semantics:
    # the same columns are written, and the from_status == 'archived' skip
    # in update_pipeline_status is reproduced here explicitly.
    _BATCH = 500

    # Live: batched UPDATE (expiry_status + expiry_checked_at + last_seen + is_stale).
    for i in range(0, len(live_keys), _BATCH):
        chunk = live_keys[i : i + _BATCH]
        placeholders = ", ".join("?" * len(chunk))
        conn.execute(
            f"""UPDATE jobs
                   SET expiry_status = 'live',
                       expiry_checked_at = ?,
                       last_seen = ?,
                       is_stale = 0
                 WHERE dedup_key IN ({placeholders})""",
            [now, now, *chunk],
        )
    result["live"] = len(live_keys)

    # Expired: batched expiry write first, then per-row pipeline_status +
    # pipeline_events (event insert needs each row's from_status). All
    # statements stay in the same implicit transaction; one commit at the end.
    for i in range(0, len(expired_keys), _BATCH):
        chunk = expired_keys[i : i + _BATCH]
        placeholders = ", ".join("?" * len(chunk))
        conn.execute(
            f"""UPDATE jobs
                   SET expiry_status = 'expired',
                       expiry_checked_at = ?
                 WHERE dedup_key IN ({placeholders})""",
            [now, *chunk],
        )

    if expired_keys:
        placeholders = ", ".join("?" * len(expired_keys))
        current_statuses = {
            r["dedup_key"]: r["pipeline_status"]
            for r in conn.execute(
                f"SELECT dedup_key, pipeline_status FROM jobs WHERE dedup_key IN ({placeholders})",
                expired_keys,
            ).fetchall()
        }
        for dk in expired_keys:
            from_status = current_statuses.get(dk)
            if from_status is None or from_status == "archived":
                continue
            conn.execute(
                "UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key = ?",
                (dk,),
            )
            conn.execute(
                """INSERT INTO pipeline_events
                       (job_id, from_status, to_status, timestamp, source, evidence)
                   VALUES (?, ?, 'archived', ?, 'ats_reconciler',
                           'ats_batch_reconcile missing_from_board')""",
                (dk, from_status, now),
            )
            logger.info(
                "reconcile_company: archived %s (missing from %s/%s board)",
                dk,
                platform,
                slug,
            )
    result["expired"] = len(expired_keys)

    conn.commit()

    logger.info(
        "reconcile_company: %s/%s checked=%d live=%d expired=%d unparseable=%d",
        platform,
        slug,
        result["checked"],
        result["live"],
        result["expired"],
        result["unparseable"],
    )

    # IA-13 visibility: when every tracked job for a company is unparseable,
    # Phase B produced no real liveness signal — the company has silently
    # degraded to aggregator-only tracking. Mirror the no_parseable_live_ids
    # warn so 100%-aggregator companies are greppable in app.log.
    if result["checked"] > 0 and result["unparseable"] == result["checked"]:
        logger.warning(
            "reconcile_company: %s/%s checked=%d all unparseable — "
            "no liveness signal (aggregator-only stored URLs?)",
            platform,
            slug,
            result["checked"],
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
            "reconcile_all_companies: %d companies with ATS slugs",
            len(companies),
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
