"""Persistence helpers for the careers crawler.

After a tier produces a list of `dict` jobs for a company, the
orchestrator calls these helpers to:
- Upsert each scraped job (creating a `Job` model object) into the
  `jobs` table.
- Stamp the company's `careers_crawl_last_at`, `last_scanned_at`,
  `careers_crawl_tier`, and `jobs_found_total` columns.
- Append a row to `company_scan_log` for the per-run audit trail.
- On a per-company exception, only stamp `careers_crawl_last_at` so a
  consistently-failing company doesn't block stalest-first ordering.

The `Job` and `upsert_job` imports are kept lazy inside `_upsert_and_log`
because `job_finder.models` and `job_finder.db` are heavy modules that
are not needed when the crawler runs in TESTING mode.
"""

from __future__ import annotations

import logging

from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def _upsert_and_log(
    jobs: list[dict],
    company_id: int,
    company_name: str,
    now: str,
    db_path: str,
    summary: dict,
    all_new_job_keys: list[str],
    tier_used: str,
) -> None:
    """Upsert discovered jobs and update company timestamps."""
    from job_finder.db import upsert_job
    from job_finder.models import Job
    from job_finder.parsed_job import DenylistedCompanyError, ParsedJob

    company_jobs_found = len(jobs)
    company_jobs_new = 0
    summary["jobs_found"] += company_jobs_found

    with standalone_connection(db_path) as upsert_conn:
        for scraped_job in jobs:
            try:
                job = Job(
                    title=scraped_job["title"],
                    company=company_name,
                    location="",
                    source="careers_crawl",
                    source_url=scraped_job.get("url") or "",
                    salary_min=None,
                    salary_max=None,
                    description=scraped_job.get("description", ""),
                )
                try:
                    parsed = ParsedJob.from_job(job)
                except DenylistedCompanyError:
                    continue
                result = upsert_job(upsert_conn, parsed, company_id=company_id)
                if result.kind == "inserted":
                    summary["jobs_new"] += 1
                    company_jobs_new += 1
                    all_new_job_keys.append(job.dedup_key)
            except Exception as job_err:
                error_msg = f"{company_name} job error: {job_err}"
                summary["errors"].append(error_msg)
                logger.warning("careers_crawler job error: %s", error_msg)

    with standalone_connection(db_path) as ts_conn:
        ts_conn.execute(
            """UPDATE companies
               SET careers_crawl_last_at = ?,
                   last_scanned_at = ?,
                   careers_crawl_tier = ?,
                   jobs_found_total = (
                       SELECT COUNT(*) FROM jobs WHERE company_id = ?
                   )
               WHERE id = ?""",
            (now, now, tier_used, company_id, company_id),
        )
        ts_conn.execute(
            """INSERT INTO company_scan_log
               (company_id, scanned_at, jobs_found, jobs_matched)
               VALUES (?, ?, ?, ?)""",
            (company_id, now, company_jobs_new, company_jobs_found),
        )
        ts_conn.commit()

    summary["companies_crawled"] += 1

    if company_jobs_found:
        logger.info(
            "careers_crawler: %s — %d jobs found (%d new) [%s]",
            company_name,
            company_jobs_found,
            company_jobs_new,
            tier_used,
        )


def _update_timestamp_on_error(
    db_path: str,
    company_id: int,
    now: str,
) -> None:
    """Update crawl timestamp on error so company doesn't block the queue."""
    try:
        with standalone_connection(db_path) as err_conn:
            err_conn.execute(
                "UPDATE companies SET careers_crawl_last_at = ? WHERE id = ?",
                (now, company_id),
            )
            err_conn.commit()
    except Exception:
        pass
