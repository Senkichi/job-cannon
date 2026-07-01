"""Batch ATS reconciliation for job staleness detection.

Fetches the full open-postings set for each tracked company with a known
ATS slug, diffs against stored jobs by posting ID, and marks absentees as
expired. Fast: one HTTP request per company instead of one per tracked job.

Architecture:
- Thread-safe: opens its own sqlite3 connection (same pattern as stale_detector).
- Posting-ID set-diff (robust to URL variance: trailing slashes, tracking params).
- Safety guards:
    * Scan exception or empty result → SKIP company (no mass-expire).
    * Incomplete board (total > fetch cap) → SKIP (completeness gate).
      Applies to Workday (#216) and SmartRecruiters (#217), which both cap
      pagination and could otherwise false-expire the unfetched tail.
    * Unknown platform → SKIP silently (iCIMS, Phenom, UKG, custom — Phase C handles).
    * Scan returned postings but none had parseable IDs → SKIP (scan format drift).
- Live jobs: last_seen refreshed + is_stale cleared (prevents downstream false-stale).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.web.ats_platforms import SCANNERS_BY_NAME
from job_finder.web.ats_platforms._registry import run_platform_scan
from job_finder.web.ats_registry import RECONCILER_POSTING_ID_PATTERNS
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Signal constants (duplicated from expiry_checker to avoid circular import).
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# A completeness-aware live-id fetcher: slug -> (live posting IDs, complete).
_LiveIdFetcher = Callable[[str], tuple[set[str], bool]]

# Greenhouse: multiple real-world URL shapes exist because companies can
# route their public board to their own careers domain. The posting ID
# (Greenhouse's numeric 'id') is the stable identifier:
#   https://boards.greenhouse.io/<slug>/jobs/<id>           canonical
#   https://job-boards.greenhouse.io/<slug>/jobs/<id>       newer domain
#   https://<company>.com/careers/job/<id>?gh_jid=<id>      self-hosted redirect
#   https://boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>   embed flow
# Accept any of those by trying the path pattern first, then the gh_jid
# query param as a fallback.
# NOTE: This special-case chain is NOT folded into the registry — it's checked
# before the dict lookup in _extract_posting_id and must stay local code.
_GREENHOUSE_PATH_RE = re.compile(r"greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE)
_GREENHOUSE_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.IGNORECASE)
_GREENHOUSE_EMBED_RE = re.compile(r"[?&]token=(\d+)", re.IGNORECASE)

# Platforms safe to batch-reconcile.
# Workday and SmartRecruiters are included but use a completeness-gated path in
# reconcile_company (_workday_live_id_set / _smartrecruiters_live_id_set) instead
# of run_platform_scan, so boards too large to fully paginate are skipped rather
# than falsely expiring unseen postings (#216 / #217).
_RECONCILABLE_PLATFORMS: frozenset[str] = frozenset(
    {"lever", "ashby", "smartrecruiters", "greenhouse", "workday", "successfactors", "adp"}
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

    pattern = RECONCILER_POSTING_ID_PATTERNS.get(platform)
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
    from job_finder.web.ats_platforms._registry import BoardGoneError

    try:
        postings, complete = _fetch_postings_with_completeness(slug)
    except BoardGoneError:
        # Board no longer resolves → treat as incomplete so the reconciler SKIPS
        # expiry (identical to the over-cap/partial path). Demoting the stale hit
        # happens on the scan path, not during reconciliation.
        return set(), False
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


def _smartrecruiters_live_id_set(slug: str) -> tuple[set[str], bool]:
    """Fetch SmartRecruiters postings and derive the set of live posting IDs.

    Uses the completeness-aware list fetch directly — does **not** trigger
    per-posting description GETs, which are unnecessary for set-diff
    reconciliation and would waste significant HTTP budget on large boards.

    Returns:
        ``(live_ids, complete)`` where ``complete`` is ``False`` when the
        board exceeds the pagination cap (>500 postings, #217) or the scan
        encountered an error before the first page arrived. IDs are the
        posting ``id`` field, which matches what
        ``_extract_posting_id(stored_url, "smartrecruiters")`` returns from
        stored ``source_urls`` (``jobs.smartrecruiters.com/<slug>/<id>``).
    """
    from job_finder.web.ats_platforms._platforms_smartrecruiters import (
        _fetch_postings_with_completeness,
    )
    from job_finder.web.ats_platforms._registry import BoardGoneError

    try:
        postings, complete = _fetch_postings_with_completeness(slug)
    except BoardGoneError:
        # Board no longer resolves → incomplete → reconciler skips expiry (same
        # as over-cap/partial). Demotion of the stale hit happens on scan path.
        return set(), False
    live_ids: set[str] = set()
    for posting in postings:
        posting_id = posting.get("id")
        if posting_id is not None:
            live_ids.add(str(posting_id))
    return live_ids, complete


# Platforms whose live board can be truncated by a pagination cap and which
# therefore expose a completeness-aware live-id fetch. The reconciler routes
# these through the gate above (skip when the board could not be fully
# fetched) instead of the generic run_platform_scan path, making the
# invariant structural: *expiry may only run against a complete live board*.
# Workday (#216) and SmartRecruiters (#217) both cap pagination; adding a new
# completeness-gated platform is a single entry here plus its `_*_live_id_set`.
#
# Values are *attribute names*, not function references, so the helper is
# resolved from the module namespace at call time — this keeps the historical
# `@patch("...ats_reconciler._workday_live_id_set")` test seam working (a
# captured reference would bypass the patch).
_COMPLETENESS_GATED_FETCHERS: dict[str, str] = {
    "workday": "_workday_live_id_set",
    "smartrecruiters": "_smartrecruiters_live_id_set",
}


def _resolve_live_id_fetcher(platform: str) -> _LiveIdFetcher:
    """Return the completeness-aware live-id fetcher for ``platform``.

    Resolved by name from the module globals so unit tests that patch the
    fetcher attribute (the established Workday test seam) take effect.
    """
    return globals()[_COMPLETENESS_GATED_FETCHERS[platform]]


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
        {'checked', 'live', 'expired', 'unparseable', 'direct_url_cleared',
         'skipped', 'skip_reason'}

    Side effect (Phase 5 staleness): an expiring job that carried a resolved
    primary-source link has its direct_url / direct_url_confidence NULLed and
    its direct_url_attempts reset to 0, so the Apply button stops pointing at a
    dead posting and a future repost is re-resolved from scratch.
    """
    platform = (company_row.get("ats_platform") or "").lower()
    slug = company_row.get("ats_slug") or ""
    company_id = company_row.get("id")

    result = {
        "checked": 0,
        "live": 0,
        "expired": 0,
        "unparseable": 0,
        "direct_url_cleared": 0,
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
    # - Completeness-gated platforms (Workday #216, SmartRecruiters #217):
    #   direct list fetch that carries a `complete` flag (no description GETs
    #   needed). The reconciler may only expire against a *complete* live
    #   board, so a truncated/partial fetch skips the company rather than
    #   false-expiring the unseen tail.
    # - Others: standard run_platform_scan, IDs extracted from source_urls.
    live_id_set: set[str] = set()

    if platform in _COMPLETENESS_GATED_FETCHERS:
        fetcher = _resolve_live_id_fetcher(platform)
        try:
            live_id_set, complete = fetcher(slug)
        except Exception as e:
            logger.warning("reconcile_company: %s/%s scan raised %s", platform, slug, e)
            result["skipped"] = True
            result["skip_reason"] = f"scan_exception:{type(e).__name__}"
            return result

        if not complete:
            logger.warning(
                "reconcile_company: %s '%s' board incomplete — skipping to avoid false-expire",
                platform,
                slug,
            )
            result["skipped"] = True
            result["skip_reason"] = f"{platform}_incomplete"
            return result

        if not live_id_set:
            logger.debug("reconcile_company: %s '%s' scan returned empty", platform, slug)
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
        SELECT dedup_key, source_urls, source_id, direct_url
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
    # dedup_keys carrying a resolved primary-source link (direct_url). When such
    # a job expires, the recorded company posting is gone too — that link must be
    # cleared (Phase 5 staleness), unlike aggregator source_urls which we leave
    # in place because they often outlive the ATS posting.
    direct_url_keys: set[str] = set()

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

        if row["direct_url"]:
            direct_url_keys.add(dedup_key)

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

    # Phase 5 staleness: clear the primary-source link for any expiring job that
    # carried one. The posting it pointed at is provably gone (its ID dropped off
    # the live board), so the Apply button must fall back to the aggregator URL.
    # Reset direct_url_attempts to 0 so a future repost re-enters the resolver and
    # is resolved afresh rather than being skipped as already-attempted.
    cleared_keys = [k for k in expired_keys if k in direct_url_keys]
    for i in range(0, len(cleared_keys), _BATCH):
        chunk = cleared_keys[i : i + _BATCH]
        placeholders = ", ".join("?" * len(chunk))
        conn.execute(
            f"""UPDATE jobs
                   SET direct_url = NULL,
                       direct_url_confidence = NULL,
                       direct_url_attempts = 0
                 WHERE dedup_key IN ({placeholders})""",
            list(chunk),
        )
    result["direct_url_cleared"] = len(cleared_keys)

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
        "reconcile_company: %s/%s checked=%d live=%d expired=%d unparseable=%d "
        "direct_url_cleared=%d",
        platform,
        slug,
        result["checked"],
        result["live"],
        result["expired"],
        result["unparseable"],
        result["direct_url_cleared"],
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
        config: Application config dict. ``config.ats.workday_max_pages``
            (if present) sets the Workday per-board pagination budget for
            this run so large tenants discover their first N pages instead
            of returning zero (issue #216).

    Returns:
        {'companies_checked', 'companies_skipped', 'checked', 'live',
         'expired', 'unparseable'}
    """
    from job_finder.web.ats_platforms._platforms_workday import (
        reset_max_pages,
        set_max_pages,
    )

    summary = {
        "companies_checked": 0,
        "companies_skipped": 0,
        "checked": 0,
        "live": 0,
        "expired": 0,
        "unparseable": 0,
        "direct_url_cleared": 0,
    }

    workday_max_pages = (config or {}).get("ats", {}).get("workday_max_pages")
    token = set_max_pages(workday_max_pages)
    try:
        return _reconcile_all_companies_inner(db_path, summary)
    finally:
        reset_max_pages(token)


def _reconcile_all_companies_inner(db_path: str, summary: dict) -> dict:
    """Body of :func:`reconcile_all_companies` (page-budget already set)."""
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
                summary["direct_url_cleared"] += company_result["direct_url_cleared"]

    logger.info("reconcile_all_companies complete: %s", summary)
    return summary
