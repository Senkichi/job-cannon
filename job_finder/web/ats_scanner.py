"""ATS scan orchestration module.

Provides probe_ats_slugs (speculative slug probing) and run_ats_scan (top-level
scan orchestrator). Both live here so that test patches against
``ats_scanner._probe_*_with_result`` and ``ats_scanner.time`` bind correctly.

run_ats_scan flow:
1. Query companies WHERE ats_probe_status='hit' AND scan_enabled=1
2. Call scan_lever/scan_greenhouse/scan_ashby per company
3. Run HTML fallback scraping for miss companies with homepages
4. Auto-score newly discovered jobs via scoring_orchestrator (Haiku → Sonnet)
5. Write company_scan_log and runs activity feed rows

Architecture:
- Thread-safe: creates own sqlite3 connections (same pattern as stale_detector.py)
- TESTING guard: returns early when config.get('TESTING') is True

Re-exports from sibling modules for backward compatibility:
- ats_company: upsert_company, find_or_create_company
- ats_platforms: _title_matches, scan_lever, scan_greenhouse, scan_ashby
- ats_detection: extract_ats_from_urls, derive_slug_candidates
- ats_prober: probe_single_company, _probe_lever_with_result, etc.
"""

import logging
import time
from datetime import datetime

import requests  # noqa: F401 — kept so tests can patch ats_scanner.requests.get

from job_finder.web.db_helpers import standalone_connection

# Scoring orchestrator functions for ATS-discovered job scoring (ImportError guard).
# Uses the centralized orchestrator instead of pipeline_runner's private functions,
# breaking the bidirectional dependency (ats_scanner <-> pipeline_runner).
try:
    from job_finder.web.scoring_orchestrator import (
        load_scoring_profile,
        score_and_persist_haiku,
        score_and_persist_sonnet,
    )
except ImportError:
    load_scoring_profile = None  # type: ignore[assignment]
    score_and_persist_haiku = None  # type: ignore[assignment]
    score_and_persist_sonnet = None  # type: ignore[assignment]

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

# Re-exports from ats_detection and ats_prober for backward compatibility
from job_finder.web.ats_detection import extract_ats_from_urls, derive_slug_candidates  # noqa: E402
from job_finder.web.ats_prober import (  # noqa: E402
    probe_single_company,
    _probe_lever_with_result,
    _probe_greenhouse_with_result,
    _probe_ashby_with_result,
    _probe_lever,
    _probe_greenhouse,
    _probe_ashby,
    _compute_retry_after,
    _is_transient_error,
    _handle_scan_error,
    _reset_retry_state,
    _PROBE_STATUS_PRECEDENCE,
    _BACKOFF_HOURS,
    _MAX_RETRIES,
    _TRANSIENT_CODES,
    _PERMANENT_MISS_CODES,
    _PROBE_TIMEOUT,
)

# Re-exports from ats_company and ats_platforms for backward compatibility
from job_finder.web.ats_company import upsert_company, find_or_create_company  # noqa: F401,E402
from job_finder.web.ats_platforms import (  # noqa: F401,E402
    _title_matches,
    scan_lever,
    scan_greenhouse,
    scan_ashby,
)

_PROBE_BATCH_LIMIT = 150  # Max companies probed per scheduled run (~7-8 min wall-clock)
_HTML_BATCH_LIMIT = 50   # Max miss companies scraped per HTML fallback run


# ---------------------------------------------------------------------------
# Speculative slug probing
# ---------------------------------------------------------------------------


def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Greenhouse (highest hit rate), Ashby, then Lever for each candidate
    3. Set ats_probe_status='hit' when API returns valid postings
    4. Set ats_probe_status='miss' when all APIs fail/return empty
    5. Lever 200+empty list stays as 'miss' (never 'hit') per Research Pitfall 2

    Lives in ats_scanner (not ats_platforms) so that test patches against
    ats_scanner._probe_*_with_result and ats_scanner.time remain effective.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with probed, hits, misses counts.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("probe_ats_slugs: TESTING mode — skipping API calls")
        return {"probed": 0, "hits": 0, "misses": 0}

    summary = {"probed": 0, "hits": 0, "misses": 0}

    with standalone_connection(db_path) as conn:
        # Only probe companies with pending status, oldest first, up to batch limit
        pending = conn.execute(
            "SELECT id, name_raw FROM companies WHERE ats_probe_status = 'pending'"
            " ORDER BY created_at ASC LIMIT ?",
            (_PROBE_BATCH_LIMIT,),
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            now = datetime.now().isoformat()

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None
            last_transient_error: Exception | None = None

            # Probe order: Greenhouse (highest hit rate) -> Ashby -> Lever (lowest).
            # Per-platform timeouts are isolated so a slow API doesn't block others.
            _probers = [
                ("greenhouse", _probe_greenhouse_with_result),
                ("ashby", _probe_ashby_with_result),
                ("lever", _probe_lever_with_result),
            ]
            for slug in candidates:
                slug_transient_count = 0
                for platform_name, probe_fn in _probers:
                    try:
                        if probe_fn(slug):
                            hit_platform, hit_slug = platform_name, slug
                            break
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                        last_transient_error = e
                        slug_transient_count += 1
                        continue  # Skip this platform, try next
                    except requests.exceptions.HTTPError as e:
                        # Distinguish transient (5xx/429) from permanent (4xx) HTTP errors.
                        status = getattr(getattr(e, "response", None), "status_code", None)
                        if status is not None and _is_transient_error(status):
                            last_transient_error = e
                            slug_transient_count += 1
                        continue  # Skip this platform, try next
                if hit_platform:
                    break
                if slug_transient_count == len(_probers):
                    # All platforms returned transient errors for this slug; servers
                    # are unreachable, so further slugs will also fail. Abort loop
                    # to avoid O(slugs × platforms × timeout) blocked calls.
                    break

            if hit_platform:
                conn.execute(
                    """UPDATE companies SET ats_platform = ?, ats_slug = ?,
                       ats_probe_status = 'hit', ats_probe_attempted_at = ?,
                       updated_at = ? WHERE id = ?""",
                    (hit_platform, hit_slug, now, now, company_id),
                )
                summary["hits"] += 1
            elif last_transient_error is not None:
                # At least one transient error (5xx/429/timeout) — preserve retry eligibility.
                try:
                    _handle_scan_error(conn, company_id, company_name, str(last_transient_error), now)
                except Exception as retry_err:
                    logger.warning("Failed to update retry state for '%s': %s", company_name, retry_err)
                # _handle_scan_error manages probe status; don't count as miss
            else:
                conn.execute(
                    """UPDATE companies SET ats_probe_status = 'miss',
                       ats_probe_attempted_at = ?, updated_at = ? WHERE id = ?""",
                    (now, now, company_id),
                )
                summary["misses"] += 1
            conn.commit()

            summary["probed"] += 1
            time.sleep(0.5)

    logger.info(
        "probe_ats_slugs: probed=%d, hits=%d, misses=%d",
        summary["probed"],
        summary["hits"],
        summary["misses"],
    )
    return summary


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


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
        haiku_scored, sonnet_evaluated, errors.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("run_ats_scan: TESTING mode — skipping API calls")
        return {
            "companies_scanned": 0,
            "jobs_discovered": 0,
            "jobs_new": 0,
            "haiku_scored": 0,
            "sonnet_evaluated": 0,
            "html_scraped": 0,
            "homepages_discovered": 0,
            "errors": [],
        }

    # Extract keyword filter settings from config
    profile = config.get("profile", {})
    target_titles = profile.get("target_titles", [])
    exclusions_cfg = profile.get("exclusions", {})
    title_exclusions = exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []

    summary = {
        "companies_scanned": 0,
        "jobs_discovered": 0,
        "jobs_new": 0,
        "haiku_scored": 0,
        "sonnet_evaluated": 0,
        "html_scraped": 0,
        "homepages_discovered": 0,
        "errors": [],
    }
    all_new_job_keys: list[str] = []

    # Best-effort Anthropic client for Haiku fallback calls in careers scraper.
    # Passing client=None to call_model() routes through free providers when configured.
    _anthropic_client = None
    try:
        import anthropic as _anthropic_mod
        _anthropic_client = _anthropic_mod.Anthropic()
    except (ImportError, Exception):
        logger.debug("Anthropic client not available — haiku tier will use free providers if configured")

    with standalone_connection(db_path) as conn:
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
            company_id = company["id"]
            company_name = company["name_raw"]
            platform = company["ats_platform"]
            slug = company["ats_slug"]
            now = datetime.now().isoformat()

            logger.info("ATS scan: scanning %s (%s/%s)", company_name, platform, slug)

            # Call the appropriate scan function
            try:
                if platform == "lever":
                    job_dicts = scan_lever(slug, target_titles, title_exclusions)
                elif platform == "greenhouse":
                    job_dicts = scan_greenhouse(slug, target_titles, title_exclusions)
                elif platform == "ashby":
                    job_dicts = scan_ashby(slug, target_titles, title_exclusions)
                else:
                    logger.warning("Unknown ATS platform '%s' for company '%s'", platform, company_name)
                    job_dicts = []

                company_jobs_found = len(job_dicts)
                company_jobs_new = 0  # post-dedup: truly new insertions this scan
                summary["jobs_discovered"] += company_jobs_found

                # Upsert each matched job
                from job_finder.db import upsert_job
                from job_finder.models import Job

                with standalone_connection(db_path) as scan_conn:
                    for job_dict in job_dicts:
                        try:
                            # First-seen salary wins: only set salary if job is new
                            # (no existing salary data). Check existing record first.
                            from job_finder.models import Job
                            candidate_dedup_key = Job.normalized_dedup_key(company_name, job_dict["title"])
                            existing_row = conn.execute(
                                "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?",
                                (candidate_dedup_key,),
                            ).fetchone()
                            if existing_row and (existing_row["salary_min"] is not None or existing_row["salary_max"] is not None):
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
                            is_new = upsert_job(scan_conn, job)

                            # Promote ATS description to jd_full (DQ-03)
                            raw_desc = job_dict.get("description") or ""
                            if len(raw_desc) > 200:
                                try:
                                    conn.execute(
                                        "UPDATE jobs SET jd_full = COALESCE(jd_full, ?) WHERE dedup_key = ?",
                                        (raw_desc[:8000], job.dedup_key),
                                    )
                                    conn.commit()
                                except Exception as e:
                                    logger.warning("Failed to promote ATS description to jd_full for %s: %s", job.dedup_key, e)

                            if is_new:
                                summary["jobs_new"] += 1
                                company_jobs_new += 1
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
                                        logger.warning("Failed to store comp_data_json for %s: %s", job.dedup_key, e)

                        except Exception as job_err:
                            error_msg = f"{company_name} job error: {job_err}"
                            summary["errors"].append(error_msg)
                            logger.warning("ATS scan job error: %s", error_msg)

                # Log company scan (jobs_found=new insertions, jobs_matched=API matches pre-dedup)
                conn.execute(
                    """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched)
                       VALUES (?, ?, ?, ?)""",
                    (company_id, now, company_jobs_new, company_jobs_found),
                )

                # Update company last_scanned_at and jobs_found_total.
                # Subquery counts currently linked jobs — self-heals after backfill links new ones.
                conn.execute(
                    """UPDATE companies
                       SET last_scanned_at = ?,
                           jobs_found_total = (
                               SELECT COUNT(*) FROM jobs WHERE company_id = ?
                           )
                       WHERE id = ?""",
                    (now, company_id, company_id),
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
                        logger.warning("Failed to update retry state for '%s': %s", company_name, retry_err)

                # Still log the failed scan attempt
                try:
                    conn.execute(
                        """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, error)
                           VALUES (?, ?, 0, ?)""",
                        (company_id, now, str(company_err)),
                    )
                    conn.commit()
                except Exception:
                    logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)

            # Polite delay between companies (0.5s)
            time.sleep(0.5)

        # --- Homepage discovery pre-step ---
        # Discover homepages for companies missing homepage_url BEFORE the HTML
        # fallback loop, so newly-discovered homepages are immediately available.
        if run_homepage_discovery is not None:
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

        # --- HTML fallback loop for miss companies ---
        # Companies with ats_probe_status='miss' but with homepage_url get scraped.
        # This loop runs AFTER the ATS API loop (which handles hit companies).
        # Guard: only run if careers_scraper was imported successfully.
        # Bounded by _HTML_BATCH_LIMIT and ordered by oldest-first to ensure
        # all miss companies get cycled through over successive runs.
        if find_careers_url is not None and scrape_careers_page is not None:
            miss_companies = conn.execute(
                """SELECT id, name_raw, homepage_url FROM companies
                   WHERE ats_probe_status = 'miss'
                     AND homepage_url IS NOT NULL
                     AND scan_enabled = 1
                   ORDER BY last_scanned_at ASC NULLS FIRST, id ASC
                   LIMIT ?""",
                (_HTML_BATCH_LIMIT,),
            ).fetchall()

            html_tried = 0
            html_careers_found = 0
            html_jobs_scraped = 0
            html_no_careers = 0
            html_exceptions = 0

            for miss_company in miss_companies:
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
                    html_tried += 1
                    # Step 1: Find careers URL from homepage
                    careers_url = find_careers_url(
                        miss_homepage_url,
                        client=_anthropic_client,
                        conn=conn,
                        config=config,
                    )
                    if not careers_url:
                        html_no_careers += 1
                        logger.debug(
                            "ATS HTML fallback: no careers link found for %s",
                            miss_company_name,
                        )
                        # Still update last_scanned_at so this company rotates to the back
                        conn.execute(
                            "UPDATE companies SET last_scanned_at = ? WHERE id = ?",
                            (now, miss_company_id),
                        )
                        conn.commit()
                        continue
                    html_careers_found += 1

                    # Step 2: Scrape careers page for keyword-matched jobs
                    scraped_jobs = scrape_careers_page(
                        careers_url, target_titles, title_exclusions,
                        client=_anthropic_client,
                        conn=conn,
                        config=config,
                    )

                    company_html_found = len(scraped_jobs)
                    company_html_new = 0  # post-dedup: truly new insertions this scan

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
                                    company_html_new += 1
                                    all_new_job_keys.append(job.dedup_key)
                                html_jobs_scraped += 1
                                summary["html_scraped"] += 1
                            except Exception as job_err:
                                error_msg = f"{miss_company_name} HTML job error: {job_err}"
                                summary["errors"].append(error_msg)
                                logger.warning("ATS HTML fallback job error: %s", error_msg)

                    # Step 4: Log company scan (jobs_found=new insertions, jobs_matched=scraped pre-dedup)
                    conn.execute(
                        """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched)
                           VALUES (?, ?, ?, ?)""",
                        (miss_company_id, now, company_html_new, company_html_found),
                    )

                    # Step 5: Update company last_scanned_at and jobs_found_total.
                    # Subquery counts currently linked jobs — self-heals after backfill links new ones.
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
                    html_exceptions += 1
                    error_msg = f"{miss_company_name} HTML fallback error: {html_err}"
                    summary["errors"].append(error_msg)
                    logger.error(
                        "ATS HTML fallback error for '%s': %s",
                        miss_company_name,
                        html_err,
                    )

                # Polite delay — HTML scraping is slower than ATS API calls
                time.sleep(1.0)

            logger.info(
                "ATS HTML fallback summary: tried=%d, careers_found=%d, "
                "jobs_scraped=%d, no_careers=%d, exceptions=%d",
                html_tried, html_careers_found, html_jobs_scraped,
                html_no_careers, html_exceptions,
            )

        # --- Haiku auto-scoring for newly discovered jobs ---
        # Runs AFTER both the ATS API loop and the HTML fallback loop so that
        # all_new_job_keys contains jobs from both sources before scoring begins.
        # Uses scoring_orchestrator directly (no pipeline_runner dependency).
        if all_new_job_keys and score_and_persist_haiku is not None:
            try:
                from job_finder.config import DEFAULT_HAIKU_THRESHOLD
                from job_finder.web.model_provider import tier_has_configured_provider

                try:
                    import anthropic as _anthropic_inline
                except ImportError:
                    _anthropic_inline = None

                _scoring_client = None
                if _anthropic_inline is not None:
                    try:
                        _scoring_client = _anthropic_inline.Anthropic()
                    except Exception:
                        pass

                if not tier_has_configured_provider("haiku", config, _scoring_client):
                    logger.debug("No routable haiku provider — skipping ATS auto-scoring")
                    raise ImportError("No routable haiku provider")

                client = _scoring_client
                profile = load_scoring_profile(config)
                threshold = config.get("scoring", {}).get(
                    "haiku_threshold", DEFAULT_HAIKU_THRESHOLD
                )
                sonnet_queue: list[str] = []
                haiku_scored_count = 0

                for dedup_key in all_new_job_keys:
                    try:
                        row = conn.execute(
                            "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                        ).fetchone()
                        if row is None:
                            continue
                        job_row = dict(row)

                        result = score_and_persist_haiku(
                            conn, job_row, config, client, profile,
                        )
                        if result is None:
                            continue
                        haiku_scored_count += 1
                        score = result.get("score", 0)
                        if score >= threshold:
                            sonnet_queue.append(dedup_key)
                    except Exception as job_err:
                        logger.warning(
                            "ATS Haiku scoring error for '%s': %s -- continuing",
                            dedup_key, job_err,
                        )

                summary["haiku_scored"] = haiku_scored_count

                # Sonnet evaluation for jobs above threshold
                if sonnet_queue and score_and_persist_sonnet is not None:
                    sonnet_evaluated = 0
                    for dedup_key in sonnet_queue:
                        try:
                            row = conn.execute(
                                "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                            ).fetchone()
                            if row is None:
                                continue
                            job_row = dict(row)
                            if not job_row.get("jd_full"):
                                continue
                            s_result = score_and_persist_sonnet(
                                conn, job_row, config, client, profile,
                            )
                            if s_result is not None:
                                sonnet_evaluated += 1
                        except Exception as sonnet_err:
                            logger.warning(
                                "ATS Sonnet scoring error for '%s': %s -- continuing",
                                dedup_key, sonnet_err,
                            )
                    summary["sonnet_evaluated"] = sonnet_evaluated

            except ImportError:
                logger.debug("anthropic not installed — skipping Haiku scoring for ATS jobs")
            except Exception as score_err:
                logger.warning("ATS scan Haiku scoring failed (non-fatal): %s", score_err)

        # --- Activity feed entry ---
        # Insert into runs table so Dashboard Recent Activity shows 'ats_scan'
        try:
            conn.execute(
                "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now().isoformat(),
                    "ats_scan",
                    summary["jobs_discovered"],
                    summary["jobs_new"],
                    summary.get("haiku_scored", 0),
                ),
            )
            conn.commit()
        except Exception as runs_err:
            logger.warning("Failed to insert ATS scan activity feed entry: %s", runs_err)

    logger.info(
        "ATS scan complete: %d companies scanned, %d jobs discovered, %d new, %d haiku-scored",
        summary["companies_scanned"],
        summary["jobs_discovered"],
        summary["jobs_new"],
        summary["haiku_scored"],
    )
    return summary
