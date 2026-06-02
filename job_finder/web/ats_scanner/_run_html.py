"""HTML-fallback scan path for ATS-miss / ATS-error companies that have a homepage.

Runs after the ATS-API scan loop in `run_ats_scan`. For each miss/error
company with a homepage_url:
1. Use cached `careers_url` or discover one via `careers_scraper.find_careers_url`.
2. Scrape that page via `careers_scraper.scrape_careers_page`.
3. Upsert each matched job; cache `careers_url` on first discovery.

Extracted from ats_scanner/_run.py during S7c (portfolio cleanup) to keep
each ats_scanner submodule under the 600-line house cap.
"""

import logging
import sqlite3
import time

from job_finder.json_utils import utc_now_iso
from job_finder.web.db_helpers import standalone_connection

# Lazy import of HTML careers scraper (ImportError guard — Plan 03)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _run_html_fallback_scan(
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
    """Phase C: HTML scrape miss/error companies that have a homepage_url."""
    # Companies with ats_probe_status='miss' but with homepage_url get scraped.
    # This loop runs AFTER the ATS API loop (which handles hit companies).
    # Same high-score-history gate as Phase A — see _run.py for the clause.
    # Function-local import breaks the _run <-> _run_html cycle.
    from job_finder.web.ats_scanner._run import _high_score_history_clause

    if find_careers_url is None or scrape_careers_page is None:
        return

    miss_companies = conn.execute(
        f"""SELECT id, name_raw, homepage_url, careers_url FROM companies
           WHERE ats_probe_status IN ('miss', 'error')
             AND homepage_url IS NOT NULL
             AND scan_enabled = 1
             AND {_high_score_history_clause()}""",
        (high_score_threshold,),
    ).fetchall()

    for miss_company in miss_companies:
        _scan_one_company_via_html(
            conn,
            db_path,
            miss_company,
            config,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
        )
        if tracker is not None:
            tracker.tick()
        # Polite delay — HTML scraping is slower than ATS API calls
        time.sleep(1.0)


def _scan_one_company_via_html(
    conn: sqlite3.Connection,
    db_path: str,
    miss_company,  # sqlite3.Row
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
) -> None:
    """Scrape a single miss/error company's careers page; upsert + log + cache careers_url."""
    # Type narrowing: callers gate on these in _run_html_fallback_scan; restate the
    # invariant here so this helper is type-safe in isolation.
    assert find_careers_url is not None and scrape_careers_page is not None
    miss_company_id = miss_company["id"]
    miss_company_name = miss_company["name_raw"]
    miss_homepage_url = miss_company["homepage_url"]
    now = utc_now_iso()

    logger.info(
        "ATS HTML fallback: scanning %s via homepage %s",
        miss_company_name,
        miss_homepage_url,
    )

    try:
        # Step 1: Use cached careers_url or discover from homepage
        careers_url = miss_company["careers_url"]
        newly_discovered_careers = False
        if not careers_url:
            careers_url = find_careers_url(
                miss_homepage_url,
                conn=conn,
                config=config,
            )
            if careers_url:
                newly_discovered_careers = True
        if not careers_url:
            logger.debug(
                "ATS HTML fallback: no careers link found for %s",
                miss_company_name,
            )
            return

        # Step 2: Scrape careers page for keyword-matched jobs
        scraped_jobs = scrape_careers_page(
            careers_url,
            target_titles,
            title_exclusions,
            conn=conn,
            config=config,
        )

        company_html_found = len(scraped_jobs)

        # Step 3: Create Job objects and upsert
        from job_finder.db import upsert_job
        from job_finder.models import Job

        with standalone_connection(db_path) as html_conn:
            for scraped_job in scraped_jobs:
                try:
                    job = Job(
                        title=scraped_job["title"],
                        company=miss_company_name,
                        location=scraped_job.get("location", ""),
                        source="careers_page",
                        source_url=scraped_job.get("url") or "",
                        salary_min=None,
                        salary_max=None,
                        description=scraped_job.get("description", ""),
                    )
                    result = upsert_job(html_conn, job, company_id=miss_company_id)
                    if result.kind == "inserted":
                        summary["jobs_new"] += 1
                        all_new_job_keys.append(job.dedup_key)
                    summary["html_scraped"] += 1
                except Exception as job_err:
                    error_msg = f"{miss_company_name} HTML job error: {job_err}"
                    summary["errors"].append(error_msg)
                    logger.warning("ATS HTML fallback job error: %s", error_msg)

        # Step 4: Log company scan
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
               VALUES (?, ?, ?)""",
            (miss_company_id, now, company_html_found),
        )

        # Step 5: Update company last_scanned_at, jobs_found_total,
        # and cache newly discovered careers_url for future runs.
        if newly_discovered_careers:
            conn.execute(
                """UPDATE companies
                   SET last_scanned_at = ?,
                       careers_url = ?,
                       jobs_found_total = (
                           SELECT COUNT(*) FROM jobs WHERE company_id = ?
                       )
                   WHERE id = ?""",
                (now, careers_url, miss_company_id, miss_company_id),
            )
        else:
            conn.execute(
                """UPDATE companies
                   SET last_scanned_at = ?,
                       jobs_found_total = (
                           SELECT COUNT(*) FROM jobs WHERE company_id = ?
                       )
                   WHERE id = ?""",
                (now, miss_company_id, miss_company_id),
            )
        conn.commit()

    except Exception as html_err:
        error_msg = f"{miss_company_name} HTML fallback error: {html_err}"
        summary["errors"].append(error_msg)
        logger.error(
            "ATS HTML fallback error for '%s': %s",
            miss_company_name,
            html_err,
        )
