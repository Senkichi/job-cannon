"""ATS scan orchestrator + Phase A/B/D/E helpers.

run_ats_scan is the public entry; the underscore-prefixed helpers below
implement four of the five scan phases (A: ATS API, B: Homepage discovery,
D: Scoring, E: Activity-feed log). Phase C (HTML fallback) lives in
_run_html.py because its careers_scraper import-graph is independent of
the ATS-API path.

Each phase helper mutates `summary` (and where relevant, `all_new_job_keys`)
in place to match the original inline-loop semantics. Refactoring to a
return-value protocol is deferred to S8.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime

from job_finder.db import derive_classification
from job_finder.secrets import get_secret
from job_finder.web.ats_platforms_internal._platforms_ashby import SCANNER as _ASHBY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_bamboohr import (
    SCANNER as _BAMBOOHR_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_breezy import SCANNER as _BREEZY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_greenhouse import (
    SCANNER as _GREENHOUSE_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_jazzhr import SCANNER as _JAZZHR_SCANNER
from job_finder.web.ats_platforms_internal._platforms_jobvite import SCANNER as _JOBVITE_SCANNER
from job_finder.web.ats_platforms_internal._platforms_lever import SCANNER as _LEVER_SCANNER
from job_finder.web.ats_platforms_internal._platforms_paylocity import (
    SCANNER as _PAYLOCITY_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_personio import (
    SCANNER as _PERSONIO_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_pinpoint import (
    SCANNER as _PINPOINT_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_recruitee import (
    SCANNER as _RECRUITEE_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_rippling import (
    SCANNER as _RIPPLING_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_smartrecruiters import (
    SCANNER as _SMARTRECRUITERS_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_teamtailor import (
    SCANNER as _TEAMTAILOR_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_workable import (
    SCANNER as _WORKABLE_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_workday import SCANNER as _WORKDAY_SCANNER
from job_finder.web.ats_platforms_internal._registry import PlatformScanner, run_platform_scan
from job_finder.web.ats_prober import _handle_scan_error, _is_transient_error
from job_finder.web.ats_scanner._run_html import _run_html_fallback_scan
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.description_formatter import strip_html_to_text

# Platform key -> PlatformScanner registry. Adding a new platform = add an
# entry here + a new _platforms_X.py module; the dispatch in
# _scan_one_company_via_ats_api never changes.
_PLATFORM_SCANNERS: dict[str, PlatformScanner] = {
    "lever": _LEVER_SCANNER,
    "greenhouse": _GREENHOUSE_SCANNER,
    "ashby": _ASHBY_SCANNER,
    "workday": _WORKDAY_SCANNER,
    "smartrecruiters": _SMARTRECRUITERS_SCANNER,
    "recruitee": _RECRUITEE_SCANNER,
    "breezy": _BREEZY_SCANNER,
    "jazzhr": _JAZZHR_SCANNER,
    "pinpoint": _PINPOINT_SCANNER,
    "personio": _PERSONIO_SCANNER,
    "bamboohr": _BAMBOOHR_SCANNER,
    "teamtailor": _TEAMTAILOR_SCANNER,
    # Round 6 (2026-05-27 audit B2-roadmap):
    "workable": _WORKABLE_SCANNER,
    "jobvite": _JOBVITE_SCANNER,  # stub: probe-only, see _platforms_jobvite.py
    "paylocity": _PAYLOCITY_SCANNER,
    "rippling": _RIPPLING_SCANNER,
}

# Scoring orchestrator functions for ATS-discovered job scoring (ImportError guard).
# Uses the centralized orchestrator instead of pipeline_runner's private functions,
# breaking the bidirectional dependency (ats_scanner <-> pipeline_runner).
try:
    from job_finder.web.scoring_orchestrator import score_and_persist_job
except ImportError:
    score_and_persist_job = None  # type: ignore[assignment]

# Lazy import of homepage discoverer (ImportError guard — Plan 01)
try:
    from job_finder.web.homepage_discoverer import run_homepage_discovery
except ImportError:
    run_homepage_discovery = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


class _ProgressTracker:
    """Shared counter for Phase A + Phase C company-level progress.

    Both phases iterate companies; tick() bumps the count and forwards
    (scanned, total) to the caller-supplied callback. A failing callback
    must never abort a scan, so the call is wrapped in a broad try.
    """

    __slots__ = ("_callback", "_scanned", "_total")

    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self._scanned = 0
        self._total = total

    def tick(self) -> None:
        self._scanned += 1
        if self._callback is None:
            return
        try:
            self._callback(self._scanned, self._total)
        except Exception:
            logger.debug("progress callback raised — continuing scan", exc_info=True)


# Default v3 sub_score sum cutoff for the high-score-history gate (Phase A + C).
# v3 sub_scores are 6 axes x 1-5 each (sum range 6-30). The empirical break
# point for "company has produced relevant work for this profile" is ~20:
# at >=20 the cohort is dominated by apply+consider, below 20 it's dominated
# by reject. Override per-deployment via config.ats.high_score_history_threshold.
_DEFAULT_HIGH_SCORE_THRESHOLD = 20


def _high_score_history_clause() -> str:
    """SQL fragment for the ats_scan high-score-history gate.

    Companies pass IF either (a) they have no scored jobs yet (bootstrap
    pass — new companies need a first scan to build history), OR (b) at
    least one prior job has a v3 sub_score sum >= ?. Use with one bind
    parameter: the threshold integer (typically 20).

    Score-based, not classification-based — the classifier has had
    reliability issues in the past; sub_scores are the underlying signal.
    """
    return """(
        NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE (j.company = companies.name OR j.company = companies.name_raw)
              AND j.sub_scores_json IS NOT NULL
        )
        OR EXISTS (
            SELECT 1 FROM jobs j
            WHERE (j.company = companies.name OR j.company = companies.name_raw)
              AND j.sub_scores_json IS NOT NULL
              AND (COALESCE(json_extract(j.sub_scores_json, '$.title_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.location_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.comp_fit'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.domain_match'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.seniority_match'), 0) +
                   COALESCE(json_extract(j.sub_scores_json, '$.skills_match'), 0)) >= ?
        )
    )"""


def _count_phase_a_eligible(conn: sqlite3.Connection, threshold: int) -> int:
    """Count Phase A companies (hit OR retry-eligible error) subject to the gate."""
    row = conn.execute(
        f"""SELECT COUNT(*) FROM companies
           WHERE (
               (ats_probe_status = 'hit' AND scan_enabled = 1)
               OR
               (ats_probe_status = 'error' AND scan_enabled = 1
                AND (retry_after IS NULL OR retry_after < datetime('now')))
           )
           AND {_high_score_history_clause()}""",
        (threshold,),
    ).fetchone()
    return int(row[0]) if row else 0


def _count_phase_c_eligible(conn: sqlite3.Connection, threshold: int) -> int:
    """Count Phase C companies (miss/error with homepage) subject to the gate."""
    row = conn.execute(
        f"""SELECT COUNT(*) FROM companies
           WHERE ats_probe_status IN ('miss', 'error')
             AND homepage_url IS NOT NULL
             AND scan_enabled = 1
             AND {_high_score_history_clause()}""",
        (threshold,),
    ).fetchone()
    return int(row[0]) if row else 0


def run_ats_scan(
    db_path: str,
    config: dict,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Scan all enabled companies' ATS platforms for keyword-matched job postings.

    Thread-safe: creates its own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Query companies WHERE ats_probe_status='hit' AND scan_enabled=1
    2. For each company, call scan_lever/scan_greenhouse/scan_ashby
    3. Apply keyword filter using config profile.target_titles and exclusions
    4. For each matched job, create Job object and call upsert_job
    5. Collect dedup_keys of newly-discovered jobs
    6. Score new jobs via scoring_orchestrator (v3.0 unified `run_scoring`)
    7. Insert company_scan_log row and update company.last_scanned_at
    8. Insert activity feed entry into runs table
    9. Return summary dict
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

    # High-score-history gate: skip companies whose past scored jobs are all
    # below the cutoff. See _high_score_history_clause for semantics.
    high_score_threshold = int(
        config.get("ats", {}).get(
            "high_score_history_threshold", _DEFAULT_HIGH_SCORE_THRESHOLD
        )
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
        # Compute total upfront so the progress-callback's (scanned, total)
        # pair is stable for the full scan (Phase A + Phase C). Phase B and
        # Phase D aren't per-company iterations and don't tick the tracker.
        # The route's initial _scannable_count is a Phase-A-only estimate;
        # the first callback invocation corrects total on the session row.
        total_companies = _count_phase_a_eligible(
            conn, high_score_threshold
        ) + _count_phase_c_eligible(conn, high_score_threshold)
        tracker = _ProgressTracker(progress_callback, total_companies)

        # Phase A — ATS-API scan for confirmed-hit + retry-eligible-error companies.
        _run_ats_api_scan(
            conn,
            db_path,
            target_titles,
            title_exclusions,
            summary,
            all_new_job_keys,
            high_score_threshold,
            tracker,
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
            high_score_threshold,
            tracker,
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
# ---------------------------------------------------------------------------


def _run_ats_api_scan(
    conn: sqlite3.Connection,
    db_path: str,
    target_titles: list,
    title_exclusions: list,
    summary: dict,
    all_new_job_keys: list,
    high_score_threshold: int,
    tracker: "_ProgressTracker | None" = None,
) -> None:
    """Phase A: scan confirmed-hit companies (and retry-eligible errors) via ATS API."""
    # Query companies with confirmed ATS slug (hit) AND error companies eligible
    # for retry (past their retry_after backoff window). Gated by the
    # high-score-history clause so companies that have only ever produced
    # low-scoring jobs are skipped (bootstrap exception for never-scored).
    companies = conn.execute(
        f"""SELECT id, name_raw, ats_platform, ats_slug
           FROM companies
           WHERE (
               (ats_probe_status = 'hit' AND scan_enabled = 1)
               OR
               (ats_probe_status = 'error' AND scan_enabled = 1
                AND (retry_after IS NULL OR retry_after < datetime('now')))
           )
           AND {_high_score_history_clause()}""",
        (high_score_threshold,),
    ).fetchall()

    for company in companies:
        _scan_one_company_via_ats_api(
            conn, db_path, company, target_titles, title_exclusions, summary, all_new_job_keys
        )
        if tracker is not None:
            tracker.tick()
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
        scanner = _PLATFORM_SCANNERS.get(platform)
        if scanner is None:
            logger.warning("Unknown ATS platform '%s' for company '%s'", platform, company_name)
            job_dicts = []
        else:
            job_dicts = run_platform_scan(scanner, slug, target_titles, title_exclusions)

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
            logger.debug("failed to insert error scan log for %s", company_name, exc_info=True)


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

        candidate_dedup_key = Job.normalized_dedup_key(company_name, job_dict["title"])
        existing_row = conn.execute(
            "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?",
            (candidate_dedup_key,),
        ).fetchone()
        if existing_row and (
            existing_row["salary_min"] is not None or existing_row["salary_max"] is not None
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

        is_new = upsert_job(
            scan_conn,
            job,
            locations_structured=job_dict.get("locations_structured"),
        )

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


def _run_homepage_discovery_phase(db_path: str, config: dict, summary: dict) -> None:
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


def _score_new_ats_jobs(
    conn: sqlite3.Connection,
    config: dict,
    all_new_job_keys: list,
    summary: dict,
) -> None:
    """Phase D: enrich sparse rows, then score via score_and_persist_job.

    Matches careers_crawl: shell listings (short HTML fallback, thin API text)
    often lack jd_full / salary / location until enrich_job runs.
    """
    # v3.0 (Phase 34 Plan 3 Commit A): uses unified score_and_persist_job;
    # per-classification counters replace haiku_scored / sonnet_evaluated.
    if not all_new_job_keys or score_and_persist_job is None:
        return
    try:
        try:
            from job_finder.web.data_enricher import enrich_job as _enrich_job
        except ImportError:
            _enrich_job = None  # type: ignore[assignment,misc]

        serpapi_key = get_secret("sources.serpapi.api_key", config=config)
        scored_count = 0

        for dedup_key in all_new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue
                job_row = dict(row)

                if _enrich_job is not None and (
                    not job_row.get("jd_full")
                    or job_row.get("salary_min") is None
                    or not job_row.get("location")
                ):
                    try:
                        enriched = _enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "ATS scan enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

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
                cls = derive_classification(result.data.sub_scores, job_row.get("legitimacy_note"))
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
