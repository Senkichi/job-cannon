"""ATS (Applicant Tracking System) scanner and company registry module.

Provides:
- ATS URL extraction from job source_urls (Lever, Greenhouse, Ashby)
- Company record upsert with ATS info and normalization
- Speculative ATS slug probing with persistent cache (hit/miss/pending)
- _title_matches keyword filtering utility shared by Plans 02 and 03

Architecture:
- Thread-safe: probe_ats_slugs() creates own sqlite3 connection (same pattern
  as stale_detector.py)
- TESTING guard on probe_ats_slugs to prevent external API calls in tests
- Never re-probes cached misses; never downgrades confirmed hits to pending

ATS URL patterns (Research Pattern 2):
- Lever: jobs.lever.co/{slug}/... and api.lever.co/v0/postings/{slug}
- Greenhouse: boards.greenhouse.io/{slug}/... and boards-api.greenhouse.io/v1/boards/{slug}
- Ashby: jobs.ashbyhq.com/{slug}/... (case-sensitive slug per Research Pitfall 3)
"""

import json
import logging
import sqlite3
import time
from datetime import datetime

import requests  # noqa: F401 — re-exported for test patching of requests.get

from job_finder.db import derive_classification
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.description_formatter import strip_html_to_text

# Scoring orchestrator functions for ATS-discovered job scoring (ImportError guard).
# Uses the centralized orchestrator instead of pipeline_runner's private functions,
# breaking the bidirectional dependency (ats_scanner <-> pipeline_runner).
try:
    from job_finder.web.scoring_orchestrator import score_and_persist_job
except ImportError:
    score_and_persist_job = None  # type: ignore[assignment]

# Lazy import of HTML careers scraper (ImportError guard — Plan 03)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

# Lazy import of homepage discoverer (ImportError guard — Plan 01)
try:
    from job_finder.web.homepage_discoverer import run_homepage_discovery
except ImportError:
    run_homepage_discovery = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

from job_finder.web.ats_detection import (
    derive_slug_candidates,
    extract_ats_from_urls,
)

# Canonical scanner implementations live in ats_platforms.py.
# Re-exported here for backward compatibility with existing callers
# (careers_scraper, enrichment_tiers, run_ats_scan loop, tests/test_ats_scanner.py).
from job_finder.web.ats_platforms import (  # noqa: F401
    _title_matches,
    scan_ashby,
    scan_greenhouse,
    scan_lever,
)
from job_finder.web.ats_prober import (  # noqa: F401
    _BACKOFF_HOURS,
    _MAX_RETRIES,
    _PERMANENT_MISS_CODES,
    _PROBE_STATUS_PRECEDENCE,
    _PROBE_TIMEOUT,
    _TRANSIENT_CODES,
    _compute_retry_after,
    _handle_scan_error,
    _is_transient_error,
    _probe_ashby,
    _probe_greenhouse,
    _probe_lever,
    _probe_lever_with_result,
    _probe_smartrecruiters,
    _probe_workday,
    _reset_retry_state,
    probe_single_company,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# upsert_company lives in ats_scanner._upsert; re-exported below for the
# established `from job_finder.web.ats_scanner import upsert_company` contract.
from job_finder.web.ats_scanner._upsert import upsert_company  # noqa: E402,F401

# probe_ats_slugs lives in ats_scanner._probe; re-exported below for the
# established `from job_finder.web.ats_scanner import probe_ats_slugs` contract.
from job_finder.web.ats_scanner._probe import probe_ats_slugs  # noqa: E402,F401


def promote_ats_from_source_urls(db_path: str, config: dict) -> dict:
    """Promote miss/error companies to ATS-hit using evidence from job source_urls.

    Separate from probe_ats_slugs() — different strategy (DB lookup, not name
    guessing) and different input set (miss/error, not pending).

    For each miss/error company with scan_enabled=1:
    1. Load source_urls from all linked jobs
    2. Extract ATS platform+slug via extract_ats_from_urls()
    3. Verify the slug is live (single API call)
    4. On verified hit, update company to ats_probe_status='hit'

    Thread-safe: creates own sqlite3 connection.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with checked, promoted counts.
    """
    if config.get("TESTING"):
        return {"checked": 0, "promoted": 0}

    summary = {"checked": 0, "promoted": 0}

    with standalone_connection(db_path) as conn:
        missed = conn.execute(
            """SELECT id, name FROM companies
               WHERE ats_probe_status IN ('miss', 'error')
                 AND scan_enabled = 1""",
        ).fetchall()

        for company in missed:
            company_id = company["id"]
            summary["checked"] += 1

            rows = conn.execute(
                "SELECT source_urls FROM jobs WHERE company_id = ? AND source_urls IS NOT NULL",
                (company_id,),
            ).fetchall()

            all_urls = []
            for row in rows:
                try:
                    all_urls.extend(json.loads(row[0] or "[]"))
                except (json.JSONDecodeError, TypeError):
                    continue

            if not all_urls:
                continue

            platform, slug = extract_ats_from_urls(all_urls)
            if not slug:
                continue

            # Verify the slug is live with a single API call
            verified = False
            if platform == "lever":
                verified = _probe_lever(slug)
            elif platform == "greenhouse":
                verified = _probe_greenhouse(slug)
            elif platform == "ashby":
                verified = _probe_ashby(slug)
            elif platform == "workday":
                verified = _probe_workday(slug)
            elif platform == "smartrecruiters":
                verified = _probe_smartrecruiters(slug)

            if not verified:
                continue

            now = datetime.now().isoformat()
            conn.execute(
                """UPDATE companies
                   SET ats_platform = ?,
                       ats_slug = ?,
                       ats_probe_status = 'hit',
                       ats_probe_attempted_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (platform, slug, now, now, company_id),
            )
            conn.commit()
            summary["promoted"] += 1
            logger.info(
                "promote_ats: %s -> %s:%s (from job source_urls)",
                company["name"],
                platform,
                slug,
            )

    logger.info(
        "promote_ats_from_source_urls: checked=%d, promoted=%d",
        summary["checked"],
        summary["promoted"],
    )
    return summary


def run_ats_scan(db_path: str, config: dict) -> dict:
    """Scan all enabled companies' ATS platforms for keyword-matched job postings.

    Thread-safe: creates its own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Query companies WHERE ats_probe_status='hit' AND scan_enabled=1
    2. For each company, call scan_lever/scan_greenhouse/scan_ashby
    3. Apply keyword filter using config profile.target_titles and exclusions
    4. For each matched job, create Job object and call upsert_job
    5. Collect dedup_keys of newly-discovered jobs
    6. Score new jobs via scoring_orchestrator (Haiku fast-filter)
    7. Evaluate jobs above haiku_threshold via scoring_orchestrator (Sonnet deep-eval)
    8. Insert company_scan_log row and update company.last_scanned_at
    9. Insert activity feed entry into runs table
    10. Return summary dict

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag, profile section.

    Returns:
        Dict with keys: companies_scanned, jobs_discovered, jobs_new,
        scored, classified_apply, classified_consider, classified_skip,
        classified_reject, errors.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("run_ats_scan: TESTING mode — skipping API calls")
        return {
            "companies_scanned": 0,
            "jobs_discovered": 0,
            "jobs_new": 0,
            "scored": 0,
            "classified_apply": 0,
            "classified_consider": 0,
            "classified_skip": 0,
            "classified_reject": 0,
            "html_scraped": 0,
            "homepages_discovered": 0,
            "errors": [],
        }

    # Extract keyword filter settings from config
    profile = config.get("profile", {})
    target_titles = profile.get("target_titles", [])
    exclusions_cfg = profile.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    summary: dict = {
        "companies_scanned": 0,
        "jobs_discovered": 0,
        "jobs_new": 0,
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "html_scraped": 0,
        "homepages_discovered": 0,
        "errors": [],
    }
    all_new_job_keys: list[str] = []

    with standalone_connection(db_path) as conn:
        # Phase A — ATS-API scan for confirmed-hit + retry-eligible-error companies.
        _run_ats_api_scan(
            conn, db_path, target_titles, title_exclusions, summary, all_new_job_keys
        )

        # Phase B — Homepage discovery for companies missing homepage_url. Runs
        # BEFORE the HTML fallback so newly-discovered homepages are available.
        _run_homepage_discovery_phase(db_path, config, summary)

        # Phase C — HTML fallback for miss/error companies that DO have a homepage.
        _run_html_fallback_scan(
            conn,
            db_path,
            config,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
        )

        # Phase D — Auto-scoring for newly discovered jobs across both phases.
        _score_new_ats_jobs(conn, config, all_new_job_keys, summary)

        # Phase E — Activity feed entry so Dashboard Recent Activity shows 'ats_scan'.
        _log_ats_scan_run(conn, summary)

    logger.info(
        "ATS scan complete: %d companies scanned, %d jobs discovered, %d new, %d scored "
        "(apply=%d, consider=%d, skip=%d, reject=%d)",
        summary["companies_scanned"],
        summary["jobs_discovered"],
        summary["jobs_new"],
        summary["scored"],
        summary.get("classified_apply", 0),
        summary.get("classified_consider", 0),
        summary.get("classified_skip", 0),
        summary.get("classified_reject", 0),
    )
    return summary


# ---------------------------------------------------------------------------
# Phase helpers for run_ats_scan
#
# Each helper mutates `summary` (and where relevant, `all_new_job_keys`) in
# place to match the original inline-loop semantics. Keeping mutation in place
# preserves the exact public-API behavior of run_ats_scan(); refactoring to
# a return-value protocol is deferred to S8.
# ---------------------------------------------------------------------------


def _run_ats_api_scan(
    conn: sqlite3.Connection,
    db_path: str,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
) -> None:
    """Phase A: scan confirmed-hit companies (and retry-eligible errors) via ATS API."""
    # Query companies with confirmed ATS slug (hit) AND error companies eligible
    # for retry (past their retry_after backoff window).
    companies = conn.execute(
        """SELECT id, name_raw, ats_platform, ats_slug
           FROM companies
           WHERE (
               (ats_probe_status = 'hit' AND scan_enabled = 1)
               OR
               (ats_probe_status = 'error' AND scan_enabled = 1
                AND (retry_after IS NULL OR retry_after < datetime('now')))
           )"""
    ).fetchall()

    for company in companies:
        _scan_one_company_via_ats_api(
            conn, db_path, company, target_titles, title_exclusions, summary, all_new_job_keys
        )
        # Polite delay between companies (0.5s)
        time.sleep(0.5)


def _scan_one_company_via_ats_api(
    conn: sqlite3.Connection,
    db_path: str,
    company,  # sqlite3.Row
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
) -> None:
    """Scan a single company via its ATS API; upsert + log + retry-track."""
    company_id = company["id"]
    company_name = company["name_raw"]
    platform = company["ats_platform"]
    slug = company["ats_slug"]
    now = datetime.now().isoformat()

    logger.info("ATS scan: scanning %s (%s/%s)", company_name, platform, slug)

    try:
        if platform == "lever":
            job_dicts = scan_lever(slug, target_titles, title_exclusions)
        elif platform == "greenhouse":
            job_dicts = scan_greenhouse(slug, target_titles, title_exclusions)
        elif platform == "ashby":
            job_dicts = scan_ashby(slug, target_titles, title_exclusions)
        elif platform == "workday":
            from job_finder.web.ats_platforms import scan_workday

            job_dicts = scan_workday(slug, target_titles, title_exclusions)
        elif platform == "smartrecruiters":
            from job_finder.web.ats_platforms import scan_smartrecruiters

            job_dicts = scan_smartrecruiters(slug, target_titles, title_exclusions)
        else:
            logger.warning(
                "Unknown ATS platform '%s' for company '%s'", platform, company_name
            )
            job_dicts = []

        company_jobs_found = len(job_dicts)
        summary["jobs_discovered"] += company_jobs_found

        # Upsert each matched job (uses inner standalone_connection per Phase A semantics).
        with standalone_connection(db_path) as scan_conn:
            for job_dict in job_dicts:
                _upsert_one_ats_api_job(
                    conn, scan_conn, company_name, job_dict, summary, all_new_job_keys
                )

        # Log company scan
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
               VALUES (?, ?, ?)""",
            (company_id, now, company_jobs_found),
        )

        # Update company last_scanned_at and jobs_found_total
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
        logger.error("ATS scan error for '%s': %s", company_name, company_err)

        # Distinguish transient vs permanent failures for retry tracking
        if _is_transient_error(company_err):
            try:
                _handle_scan_error(conn, company_id, company_name, str(company_err), now)
            except Exception as retry_err:
                logger.warning(
                    "Failed to update retry state for '%s': %s", company_name, retry_err
                )

        # Still log the failed scan attempt
        try:
            conn.execute(
                """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                   VALUES (?, ?, 0, ?)""",
                (company_id, now, str(company_err)),
            )
            conn.commit()
        except Exception:
            logger.debug(
                "failed to insert error scan log for %s", company_name, exc_info=True
            )


def _upsert_one_ats_api_job(
    conn: sqlite3.Connection,
    scan_conn: sqlite3.Connection,
    company_name: str,
    job_dict: dict,
    summary: dict,
    all_new_job_keys: list,
) -> None:
    """Upsert a single ATS-API-discovered job; promote jd_full + comp_data_json on first-seen."""
    try:
        # First-seen salary wins: only set salary if job is new
        # (no existing salary data). Check existing record first.
        from job_finder.models import Job

        candidate_dedup_key = Job.normalized_dedup_key(
            company_name, job_dict["title"]
        )
        existing_row = conn.execute(
            "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?",
            (candidate_dedup_key,),
        ).fetchone()
        if existing_row and (
            existing_row["salary_min"] is not None
            or existing_row["salary_max"] is not None
        ):
            # Existing job has salary — preserve it (first-seen wins)
            salary_min = None
            salary_max = None
        else:
            salary_min = job_dict.get("salary_min")
            salary_max = job_dict.get("salary_max")

        job = Job(
            title=job_dict["title"],
            company=company_name,
            location=job_dict.get("location") or "",
            source=job_dict["company_source"],  # 'Lever', 'Greenhouse', 'Ashby'
            source_url=job_dict.get("source_url") or "",
            salary_min=salary_min,
            salary_max=salary_max,
            description=job_dict.get("description") or "",
        )
        from job_finder.db import upsert_job

        is_new = upsert_job(scan_conn, job)

        # Promote ATS description to jd_full (DQ-03)
        # Strip HTML to prevent CSS soup from inflating AI scores
        raw_desc = job_dict.get("description") or ""
        clean_desc = strip_html_to_text(raw_desc) if "<" in raw_desc else raw_desc
        if len(clean_desc) > 200:
            try:
                conn.execute(
                    "UPDATE jobs SET jd_full = COALESCE(jd_full, ?) WHERE dedup_key = ?",
                    (clean_desc[:8000], job.dedup_key),
                )
                conn.commit()
            except Exception as e:
                logger.warning(
                    "Failed to promote ATS description to jd_full for %s: %s",
                    job.dedup_key,
                    e,
                )

        if is_new:
            summary["jobs_new"] += 1
            all_new_job_keys.append(job.dedup_key)

            # Store comp_json for new jobs only (first-seen wins)
            comp_json = job_dict.get("comp_json")
            if comp_json:
                try:
                    conn.execute(
                        "UPDATE jobs SET comp_data_json = ? WHERE dedup_key = ?",
                        (comp_json, job.dedup_key),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to store comp_data_json for %s: %s",
                        job.dedup_key,
                        e,
                    )

    except Exception as job_err:
        error_msg = f"{company_name} job error: {job_err}"
        summary["errors"].append(error_msg)
        logger.warning("ATS scan job error: %s", error_msg)


def _run_homepage_discovery_phase(
    db_path: str, config: dict, summary: dict
) -> None:
    """Phase B: discover homepages for companies missing homepage_url."""
    if run_homepage_discovery is None:
        return
    try:
        discovery_result = run_homepage_discovery(db_path, config)
        logger.info(
            "Homepage discovery: %d checked, %d found",
            discovery_result.get("companies_checked", 0),
            discovery_result.get("homepages_found", 0),
        )
        summary["homepages_discovered"] = discovery_result.get("homepages_found", 0)
    except Exception as disc_err:
        logger.warning("Homepage discovery failed (non-fatal): %s", disc_err)
        summary["homepages_discovered"] = 0


def _run_html_fallback_scan(
    conn: sqlite3.Connection,
    db_path: str,
    config: dict,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
) -> None:
    """Phase C: HTML scrape miss/error companies that have a homepage_url."""
    # Companies with ats_probe_status='miss' but with homepage_url get scraped.
    # This loop runs AFTER the ATS API loop (which handles hit companies).
    # Guard: only run if careers_scraper was imported successfully.
    if find_careers_url is None or scrape_careers_page is None:
        return

    miss_companies = conn.execute(
        """SELECT id, name_raw, homepage_url, careers_url FROM companies
           WHERE ats_probe_status IN ('miss', 'error')
             AND homepage_url IS NOT NULL
             AND scan_enabled = 1"""
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
    now = datetime.now().isoformat()

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
                        location="",
                        source="careers_page",
                        source_url=scraped_job.get("url") or "",
                        salary_min=None,
                        salary_max=None,
                        description=scraped_job.get("description", ""),
                    )
                    is_new = upsert_job(html_conn, job)
                    if is_new:
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


def _score_new_ats_jobs(
    conn: sqlite3.Connection,
    config: dict,
    all_new_job_keys: list,
    summary: dict,
) -> None:
    """Phase D: score newly-discovered jobs via score_and_persist_job."""
    # v3.0 (Phase 34 Plan 3 Commit A): uses unified score_and_persist_job;
    # per-classification counters replace haiku_scored / sonnet_evaluated.
    if not all_new_job_keys or score_and_persist_job is None:
        return
    try:
        scored_count = 0

        for dedup_key in all_new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue
                job_row = dict(row)

                result = score_and_persist_job(
                    job_row,
                    conn,
                    config,
                )
                if result is None:
                    continue
                scored_count += 1
                if getattr(result, "status", None) != "ok" or result.data is None:
                    continue
                cls = derive_classification(
                    result.data.sub_scores, job_row.get("legitimacy_note")
                )
                key = f"classified_{cls}"
                summary[key] = summary.get(key, 0) + 1
            except Exception as job_err:
                logger.warning(
                    "ATS scoring error for '%s': %s -- continuing",
                    dedup_key,
                    job_err,
                )

        summary["scored"] = scored_count

    except Exception as score_err:
        logger.warning("ATS scan scoring failed (non-fatal): %s", score_err)


def _log_ats_scan_run(conn: sqlite3.Connection, summary: dict) -> None:
    """Phase E: insert one runs-table row so Dashboard Recent Activity shows the scan."""
    try:
        conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                "ats_scan",
                summary["jobs_discovered"],
                summary["jobs_new"],
                summary.get("scored", 0),
            ),
        )
        conn.commit()
    except Exception as runs_err:
        logger.warning("Failed to insert ATS scan activity feed entry: %s", runs_err)
