"""ATS (Applicant Tracking System) scanner and company registry module.

Provides:
- ATS URL extraction from job source_urls (Lever, Greenhouse, Ashby)
- Company record upsert with ATS info and normalization
- Speculative ATS slug probing with persistent cache (hit/miss/pending)
- _title_matches keyword filtering utility shared by Plans 02 and 03

Architecture:
- Thread-safe: probe_ats_slugs() creates own sqlite3 connection (same pattern
  as stale_detector.py and rejection_analyzer.py)
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
from typing import Optional

import requests

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.dedup_normalizer import normalize_company

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

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _title_matches(title: str, target_titles: list[str], exclusions: list[str]) -> bool:
    """Return True if title matches any target keyword and no exclusion keyword.

    Pure Python case-insensitive substring matching. Zero AI API calls.
    Used by Plan 02 (ATS scan functions) and Plan 03 (careers scraper).

    Args:
        title: Job title to evaluate.
        target_titles: List of keywords; title must match at least one
                        (OR semantics). If empty, all titles pass.
        exclusions: List of keywords; title must match none (AND NOT semantics).

    Returns:
        True if title should be included in results, False if filtered out.
    """
    title_lower = title.lower()

    # Must match at least one target title keyword (empty = no filter)
    if target_titles:
        if not any(t.lower() in title_lower for t in target_titles):
            return False

    # Must not match any exclusion keyword
    if any(ex.lower() in title_lower for ex in exclusions):
        return False

    return True

def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    ats_probe_status: str = "pending",
    homepage_url: Optional[str] = None,
) -> Optional[int]:
    """Create or update a company record in the companies table.

    Looks up by normalized company name. If the company exists, updates
    ats_platform, ats_slug, and ats_probe_status only when the new info
    is better (hit > pending > miss — never downgrade from hit to pending).

    Args:
        conn: Open SQLite connection with Migration 7 schema applied.
        name: Raw company name string (will be normalized for lookup).
        ats_platform: ATS platform name ('lever', 'greenhouse', 'ashby', or None).
        ats_slug: ATS slug string, or None if not yet known.
        ats_probe_status: Probe status ('pending', 'hit', or 'miss').
        homepage_url: Company homepage URL, or None.

    Returns:
        The company_id (integer) for the upserted record, or None on error.
    """
    now = datetime.now().isoformat()
    normalized_name = normalize_company(name)

    try:
        # Look up by normalized name
        existing = conn.execute(
            "SELECT id, ats_probe_status FROM companies WHERE name = ?",
            (normalized_name,),
        ).fetchone()

        if existing is None:
            # INSERT new company
            cursor = conn.execute(
                """INSERT INTO companies
                   (name, name_raw, homepage_url, ats_platform, ats_slug,
                    ats_probe_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized_name,
                    name,
                    homepage_url,
                    ats_platform,
                    ats_slug,
                    ats_probe_status,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        else:
            # UPDATE only if new info is better
            company_id = existing[0]
            current_status = existing[1] or "pending"
            current_rank = _PROBE_STATUS_PRECEDENCE.get(current_status, 0)
            new_rank = _PROBE_STATUS_PRECEDENCE.get(ats_probe_status, 0)

            # Only update ATS fields if new status is higher precedence
            if new_rank >= current_rank:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform = COALESCE(?, ats_platform),
                           ats_slug = COALESCE(?, ats_slug),
                           ats_probe_status = ?,
                           homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (
                        ats_platform,
                        ats_slug,
                        ats_probe_status,
                        homepage_url,
                        now,
                        company_id,
                    ),
                )
            else:
                # Still update non-ATS fields (homepage, timestamp)
                conn.execute(
                    """UPDATE companies
                       SET homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (homepage_url, now, company_id),
                )
            conn.commit()
            return company_id

    except Exception as e:
        logger.warning("upsert_company failed for '%s' (non-fatal): %s", name, e)
        return None

def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Lever, Greenhouse, and Ashby APIs for each candidate
    3. Set ats_probe_status='hit' when API returns valid postings
    4. Set ats_probe_status='miss' when all APIs fail/return empty
    5. Lever 200+empty list stays as 'miss' (never 'hit') per Research Pitfall 2

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
        # Only probe companies with pending status
        pending = conn.execute(
            "SELECT id, name_raw FROM companies WHERE ats_probe_status = 'pending'"
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            now = datetime.now().isoformat()

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None

            for slug in candidates:
                # Try Lever first
                if _probe_lever(slug):
                    hit_platform = "lever"
                    hit_slug = slug
                    break

                # Try Greenhouse
                if _probe_greenhouse(slug):
                    hit_platform = "greenhouse"
                    hit_slug = slug
                    break

                # Try Ashby
                if _probe_ashby(slug):
                    hit_platform = "ashby"
                    hit_slug = slug
                    break

            # Update company record based on probe result
            if hit_platform:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform = ?,
                           ats_slug = ?,
                           ats_probe_status = 'hit',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (hit_platform, hit_slug, now, now, company_id),
                )
                summary["hits"] += 1
            else:
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                summary["misses"] += 1

            conn.commit()
            summary["probed"] += 1

            # Polite delay between companies (0.5s per Research Open Question 2)
            time.sleep(0.5)

    logger.info(
        "probe_ats_slugs: probed=%d, hits=%d, misses=%d",
        summary["probed"],
        summary["hits"],
        summary["misses"],
    )
    return summary

# ---------------------------------------------------------------------------
# ATS Scan Functions
# ---------------------------------------------------------------------------

def scan_lever(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Lever API for keyword-matched job postings.

    Fetches all active postings for the given slug and applies _title_matches
    keyword filter. Zero AI API calls — pure keyword matching.

    API: GET https://api.lever.co/v0/postings/{slug}?mode=json

    Args:
        slug: Lever company slug (e.g. 'stripe').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_lever('%s') request failed: %s", slug, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_lever('%s') returned HTTP %d", slug, resp.status_code)
        return []

    try:
        postings = resp.json()
    except Exception as e:
        logger.warning("scan_lever('%s') JSON parse error: %s", slug, e)
        return []

    if not isinstance(postings, list):
        return []

    results = []
    for posting in postings:
        title = posting.get("text", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # Extract salary range when present
        salary_range = posting.get("salaryRange") or {}
        salary_min = salary_range.get("min") if salary_range else None
        salary_max = salary_range.get("max") if salary_range else None

        # Store compensation JSON for equity/bonus/benefits details
        comp_json = json.dumps(salary_range) if salary_range else None

        # Location from categories.location
        categories = posting.get("categories") or {}
        location = categories.get("location") or categories.get("team") or ""

        results.append({
            "title": title,
            "company_source": "Lever",
            "location": location,
            "description": posting.get("descriptionPlain") or "",
            "source_url": posting.get("hostedUrl") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug("scan_lever('%s'): %d postings fetched, %d matched", slug, len(postings), len(results))
    return results

def scan_greenhouse(board_token: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Greenhouse API for keyword-matched job postings.

    Fetches all active jobs with content and pay transparency data.
    CRITICAL: pay_input_ranges values are in cents — divide by 100 for dollars
    (Research Pitfall 7).

    API: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true

    Args:
        board_token: Greenhouse board token (e.g. 'airbnb').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_greenhouse('%s') request failed: %s", board_token, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_greenhouse('%s') returned HTTP %d", board_token, resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("scan_greenhouse('%s') JSON parse error: %s", board_token, e)
        return []

    postings = data.get("jobs", []) if isinstance(data, dict) else []

    results = []
    for posting in postings:
        title = posting.get("title", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # CRITICAL: Greenhouse pay values are in cents — divide by 100 for dollars
        # (Research Pitfall 7: Greenhouse uses cents to avoid floating point issues)
        salary_min = None
        salary_max = None
        comp_json = None
        pay_ranges = posting.get("pay_input_ranges") or []
        if pay_ranges:
            first_range = pay_ranges[0]
            min_cents = first_range.get("min_cents")
            max_cents = first_range.get("max_cents")
            if min_cents is not None:
                salary_min = min_cents // 100
            if max_cents is not None:
                salary_max = max_cents // 100
            comp_json = json.dumps(pay_ranges)

        location_obj = posting.get("location") or {}
        location = location_obj.get("name") or "" if isinstance(location_obj, dict) else ""

        # Content is the full job description HTML
        description = posting.get("content") or ""

        results.append({
            "title": title,
            "company_source": "Greenhouse",
            "location": location,
            "description": description,
            "source_url": posting.get("absolute_url") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug(
        "scan_greenhouse('%s'): %d postings fetched, %d matched",
        board_token, len(postings), len(results),
    )
    return results

def scan_ashby(job_board_name: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Ashby API for keyword-matched job postings.

    Preserves exact slug casing — Ashby slugs are case-sensitive
    (Research Pitfall 3: jobs.ashbyhq.com/OpenAI != jobs.ashbyhq.com/openai).

    API: GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true

    Args:
        job_board_name: Ashby job board name with exact casing (e.g. 'OpenAI', 'Ramp').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    # NOTE: No lowercasing — Ashby slugs are case-sensitive (Research Pitfall 3)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_ashby('%s') request failed: %s", job_board_name, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_ashby('%s') returned HTTP %d", job_board_name, resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("scan_ashby('%s') JSON parse error: %s", job_board_name, e)
        return []

    postings = data.get("jobs", []) if isinstance(data, dict) else []

    results = []
    for posting in postings:
        title = posting.get("title", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # Extract compensation data
        salary_min = None
        salary_max = None
        comp_json = None
        compensation = posting.get("compensation")
        if compensation:
            comp_json = json.dumps(compensation)
            # Extract base salary from summaryComponents
            summary_components = compensation.get("summaryComponents") or []
            for component in summary_components:
                if component.get("compensationType") == "base_salary":
                    salary_min = component.get("minValue")
                    salary_max = component.get("maxValue")
                    break

        # Location: use location field, fall back to empty string
        location = posting.get("location") or ""
        if not location and posting.get("isRemote"):
            location = "Remote"

        description = posting.get("descriptionHtml") or posting.get("descriptionPlain") or ""

        results.append({
            "title": title,
            "company_source": "Ashby",
            "location": location,
            "description": description,
            "source_url": posting.get("jobUrl") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug(
        "scan_ashby('%s'): %d postings fetched, %d matched",
        job_board_name, len(postings), len(results),
    )
    return results

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
        if find_careers_url is not None and scrape_careers_page is not None:
            miss_companies = conn.execute(
                """SELECT id, name_raw, homepage_url, careers_url FROM companies
                   WHERE ats_probe_status IN ('miss', 'error')
                     AND homepage_url IS NOT NULL
                     AND scan_enabled = 1"""
            ).fetchall()

            html_tried = 0

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
                        continue

                    # Step 2: Scrape careers page for keyword-matched jobs
                    scraped_jobs = scrape_careers_page(
                        careers_url, target_titles, title_exclusions,
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

                # Polite delay — HTML scraping is slower than ATS API calls
                time.sleep(1.0)

        # --- Haiku auto-scoring for newly discovered jobs ---
        # Runs AFTER both the ATS API loop and the HTML fallback loop so that
        # all_new_job_keys contains jobs from both sources before scoring begins.
        # Uses scoring_orchestrator directly (no pipeline_runner dependency).
        if all_new_job_keys and score_and_persist_haiku is not None:
            try:
                from job_finder.config import DEFAULT_HAIKU_THRESHOLD

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
                            conn, job_row, config, profile,
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
                                conn, job_row, config, profile,
                            )
                            if s_result is not None:
                                sonnet_evaluated += 1
                        except Exception as sonnet_err:
                            logger.warning(
                                "ATS Sonnet scoring error for '%s': %s -- continuing",
                                dedup_key, sonnet_err,
                            )
                    summary["sonnet_evaluated"] = sonnet_evaluated

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

