"""Playwright-class ATS scan path — JS-rendered, no-public-API boards (iCIMS).

Parallel architecture to the requests-based ``PlatformScanner`` registry
path in ``_run.py``. iCIMS (and future JS-rendered, no-API platforms) cannot
ride the ``slug -> list[dict]`` registry contract because they need a live
browser to render the board. This module owns that lifecycle:

1. ``_run_playwright_scan`` is the phase — it queries all Playwright-class
   companies, opens **one** ``sync_playwright()`` block + ``chromium`` browser
   for the whole batch (never one browser per company), and drives each
   company through the scanner.
2. ``run_playwright_platform_scan`` is the per-company driver — the analog of
   ``_registry.run_platform_scan``: fetch → title gate → normalize → log.
3. Upserts route through ``_run._upsert_one_ats_api_job`` (shared with the
   requests path) so iCIMS jobs land in ``jobs`` identically and get picked
   up by the shared Phase D scoring loop.

The ``_run <-> _run_playwright`` import cycle is broken the same way
``_run_html`` breaks it: the shared ``_run`` helpers are imported
function-locally, so importing this module at ``_run`` load time does not
re-enter ``_run``.

Extracted as a discrete submodule (mirroring ``_run_html.py``) to keep each
ats_scanner file under the house line cap.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_platforms._platforms_icims import SCANNER as _ICIMS_SCANNER
from job_finder.web.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from job_finder.web.ats_prober import _handle_scan_error, _is_transient_error
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Platforms handled by the Playwright-class path rather than the requests
# registry. Phase A excludes these so ``_scan_one_company_via_ats_api`` never
# sees them and logs "Unknown ATS platform"; this phase owns them instead.
_PLAYWRIGHT_SCANNERS: dict[str, PlaywrightPlatformScanner] = {
    "icims": _ICIMS_SCANNER,
}

PLAYWRIGHT_PLATFORMS: frozenset[str] = frozenset(_PLAYWRIGHT_SCANNERS)
"""Platform keys routed to the Playwright phase (consumed by ``_run.py``)."""

# Default load-more click budget when ``config.ats.icims_max_load_more_clicks``
# is unset. Matches the scanner module's own default.
_DEFAULT_MAX_LOAD_MORE = 5


def playwright_platform_exclusion_clause() -> str:
    """SQL fragment excluding Playwright-class platforms from the requests path.

    Returns a ``(ats_platform IS NULL OR ats_platform NOT IN (...))`` clause
    built from the internal ``PLAYWRIGHT_PLATFORMS`` constant. The values are
    a hardcoded lowercase code constant (never user input), so inlining them
    as quoted literals is injection-safe — there are no bind parameters to
    thread through the existing f-string-composed Phase A queries.
    """
    quoted = ",".join(f"'{p}'" for p in sorted(PLAYWRIGHT_PLATFORMS))
    return f"(ats_platform IS NULL OR ats_platform NOT IN ({quoted}))"


def run_playwright_platform_scan(
    scanner: PlaywrightPlatformScanner,
    browser,
    slug: str,
    target_titles: list,
    exclusions: list,
    *,
    max_load_more: int = _DEFAULT_MAX_LOAD_MORE,
) -> list[dict]:
    """Run one Playwright-class scan: render → title gate → normalize → log.

    The browser-owning analog of ``_registry.run_platform_scan``: every raw
    posting that ``_title_matches`` accepts is normalized via
    ``scanner.posting_to_job`` and appended. The debug count log mirrors the
    requests-path shape.

    Args:
        scanner: The platform's ``PlaywrightPlatformScanner`` value object.
        browser: Playwright ``Browser`` owned by the caller's lifecycle.
        slug: Per-company platform identifier (iCIMS tenant subdomain).
        target_titles: Title-match keywords for inclusion.
        exclusions: Title-match keywords for exclusion (AND-NOT).
        max_load_more: Per-board "load more" click budget.

    Returns:
        Canonical job dicts for matched postings. Empty on render error or
        no matches.
    """
    from job_finder.web.ats_platforms import _title_matches

    postings = list(scanner.fetch_postings(browser, slug, max_load_more=max_load_more))

    results: list[dict] = []
    for posting in postings:
        title = scanner.title_of(posting)
        if not _title_matches(title, target_titles, exclusions):
            continue
        job_dict = scanner.posting_to_job(posting, slug)
        if job_dict is not None:
            results.append(job_dict)

    logger.debug(
        "scan_%s('%s'): %d postings fetched, %d matched",
        scanner.name,
        slug,
        len(postings),
        len(results),
    )
    return results


def _playwright_phase_query() -> str:
    """SQL for the Playwright phase cohort (mirrors Phase A's status gate)."""
    from job_finder.web.ats_scanner._run import _high_score_history_clause

    quoted = ",".join(f"'{p}'" for p in sorted(PLAYWRIGHT_PLATFORMS))
    return f"""SELECT id, name_raw, ats_platform, ats_slug
           FROM companies
           WHERE ats_platform IN ({quoted})
             AND (
                 (ats_probe_status = 'hit' AND scan_enabled = 1)
                 OR
                 (ats_probe_status = 'error' AND scan_enabled = 1
                  AND (retry_after IS NULL OR retry_after < datetime('now')))
             )
             AND {_high_score_history_clause()}"""


def count_playwright_eligible(conn: sqlite3.Connection, threshold: int) -> int:
    """Count Playwright-phase companies subject to the high-score gate."""
    row = conn.execute(
        _playwright_phase_query().replace(
            "SELECT id, name_raw, ats_platform, ats_slug", "SELECT COUNT(*)", 1
        ),
        (threshold,),
    ).fetchone()
    return int(row[0]) if row else 0


def _run_playwright_scan(
    conn: sqlite3.Connection,
    db_path: str,
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    high_score_threshold: int,
    tracker=None,
) -> None:
    """Phase A2: scan Playwright-class companies (iCIMS) under one browser.

    Batches every eligible Playwright-platform company under a single
    ``sync_playwright()`` + ``chromium.launch()`` block. A no-op when no such
    companies exist or when Playwright is not installed (optional heavy dep).
    """
    companies = conn.execute(_playwright_phase_query(), (high_score_threshold,)).fetchall()
    if not companies:
        return

    max_load_more = int(
        config.get("ats", {}).get("icims_max_load_more_clicks", _DEFAULT_MAX_LOAD_MORE)
    )

    # Lazy attribute access triggers careers_crawler's PEP-562 hook, which
    # raises ImportError with install instructions when playwright is absent.
    import job_finder.web.careers_crawler as _cc

    try:
        sync_playwright = _cc.sync_playwright
    except ImportError as exc:
        logger.warning("Playwright not installed — skipping iCIMS scan phase: %s", exc)
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for company in companies:
                _scan_one_company_via_playwright(
                    conn,
                    db_path,
                    company,
                    browser,
                    target_titles,
                    title_exclusions,
                    summary,
                    all_new_job_keys,
                    max_load_more,
                )
                if tracker is not None:
                    tracker.tick()
                # Polite delay between companies (rendering is heavier than API).
                time.sleep(0.5)
        finally:
            try:
                browser.close()
            except Exception:
                logger.debug("iCIMS scan: browser.close() failed", exc_info=True)


def _scan_one_company_via_playwright(
    conn: sqlite3.Connection,
    db_path: str,
    company,  # sqlite3.Row
    browser,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    max_load_more: int,
) -> None:
    """Render + scan a single Playwright-class company; upsert + log + retry-track.

    Models ``_run._scan_one_company_via_ats_api`` but dispatches through the
    ``_PLAYWRIGHT_SCANNERS`` map with the shared browser. Upserts reuse the
    requests-path helper so jobs land identically and feed Phase D scoring.
    """
    from job_finder.web.ats_scanner._run import _upsert_one_ats_api_job

    company_id = company["id"]
    company_name = company["name_raw"]
    platform = company["ats_platform"]
    slug = company["ats_slug"]
    now = utc_now_iso()

    logger.info("ATS scan (playwright): scanning %s (%s/%s)", company_name, platform, slug)

    scanner = _PLAYWRIGHT_SCANNERS.get(platform)
    if scanner is None:
        # Defensive: the phase query only selects PLAYWRIGHT_PLATFORMS, so this
        # is unreachable unless the map and the constant drift apart.
        logger.warning("No Playwright scanner for platform '%s' (%s)", platform, company_name)
        return

    try:
        job_dicts = run_playwright_platform_scan(
            scanner,
            browser,
            slug,
            target_titles,
            title_exclusions,
            max_load_more=max_load_more,
        )

        company_jobs_found = len(job_dicts)
        summary["jobs_discovered"] += company_jobs_found

        with standalone_connection(db_path) as scan_conn:
            for job_dict in job_dicts:
                _upsert_one_ats_api_job(
                    conn,
                    scan_conn,
                    company_name,
                    job_dict,
                    summary,
                    all_new_job_keys,
                    company_id=company_id,
                )

        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
               VALUES (?, ?, ?)""",
            (company_id, now, company_jobs_found),
        )
        conn.execute(
            """UPDATE companies
               SET last_scanned_at = ?,
                   jobs_found_total = jobs_found_total + ?
               WHERE id = ?""",
            (now, company_jobs_found, company_id),
        )
        conn.commit()
        summary["companies_scanned"] += 1

    except Exception as company_err:
        error_msg = f"{company_name}: {company_err}"
        summary["errors"].append(error_msg)
        logger.error("ATS scan (playwright) error for '%s': %s", company_name, company_err)

        if _is_transient_error(company_err):
            try:
                _handle_scan_error(conn, company_id, company_name, str(company_err), now)
            except Exception as retry_err:
                logger.warning(
                    "Failed to update retry state for '%s': %s", company_name, retry_err
                )

        try:
            conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                   VALUES (?, ?, 0, ?)""",
                (company_id, now, str(company_err)),
            )
            conn.commit()
        except Exception:
            logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)
