"""Ingestion pipeline runner for the Flask web app.

Orchestrates Gmail + SerpAPI + Thordata ingestion with:
- Run-level tracking via email_parse_log (prevents redundant log entries)
- Per-source error isolation (Gmail failure does not stop SerpAPI)
- Per-job error isolation (one bad job does not halt persistence)
- Scoring via JobScorer before persistence
- Deduplication via db.upsert_job (dedup_key: company|title|location)
- Two-tier AI scoring: Haiku fast-filter for all new jobs, Sonnet deep-eval
  for jobs above haiku_threshold.

Thread-safety: Creates a NEW sqlite3 connection per call. This function runs
in the APScheduler background thread -- it must NOT share a connection with
the Flask request thread.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from job_finder.sources.dataforseo_source import DataForSEOSource

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from job_finder.config import DEFAULT_LOOKBACK_DAYS, DEFAULT_MONTHLY_BUDGET_USD
from job_finder.db import upsert_job, log_run
from job_finder.json_utils import utc_now_iso
from job_finder.models import Job
from job_finder.scoring.scorer import JobScorer
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_runner import run_haiku_scoring, run_sonnet_evaluation

# ats_scanner import is lazy (inside _score_and_persist) to avoid circular import:
# ats_scanner → dedup_normalizer → pipeline_runner → ats_scanner (partial)

try:
    from job_finder.sources.gmail_source import GmailSource
except ImportError:
    GmailSource = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Track last notified budget threshold to avoid repeated notifications.
# Reset on app restart (acceptable for single-user app).
# Thread-safety: ALL reads and writes of _last_budget_pct_notified MUST occur
# inside a `with _budget_alert_lock:` block to prevent data races.
_budget_alert_lock = threading.Lock()
_last_budget_pct_notified: float = 0.0


def run_ingestion(db_path: str, config: dict) -> dict:
    """Run the full ingestion pipeline: fetch -> score -> dedup -> persist -> AI score.

    Creates its own SQLite connection (thread-safe: called from APScheduler
    background thread, not the Flask request thread).

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Full JF_CONFIG dict (profile + scoring + sources sections).

    Returns:
        Summary dict with keys:
            - gmail_fetched: int
            - gmail_errors: list[str]
            - serpapi_fetched: int
            - serpapi_errors: list[str]
            - thordata_fetched: int
            - thordata_errors: list[str]
            - jobs_new: int
            - jobs_updated: int
            - jobs_touch_only: int
            - jobs_scored: int
            - job_errors: list[str]
            - haiku_scored: int
            - sonnet_queued: int
            - sonnet_queue: list[str]
            - sonnet_evaluated: int
            - duration_seconds: float
    """
    start_time = datetime.now()
    summary = {
        "gmail_fetched": 0,
        "gmail_errors": [],
        "serpapi_fetched": 0,
        "serpapi_errors": [],
        "thordata_fetched": 0,
        "thordata_errors": [],
        "scaleserp_fetched": 0,
        "scaleserp_errors": [],
        "dataforseo_fetched": 0,
        "dataforseo_errors": [],
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_touch_only": 0,
        "jobs_scored": 0,
        "job_errors": [],
        "haiku_scored": 0,
        "sonnet_queued": 0,
        "sonnet_queue": [],
        "sonnet_evaluated": 0,
        "duration_seconds": 0.0,
    }
    new_job_keys: list[str] = []

    # New connection per call -- thread-safe
    scorer = JobScorer(config)

    with standalone_connection(db_path) as runner_conn:
        # --- Phase 1: Submit DataForSEO tasks (non-blocking ~2s) ---
        # Tasks are submitted first so DataForSEO's 60-120s server-side processing
        # overlaps with Gmail's ~70s fetch, cutting total ingestion time by ~60s.
        dataforseo_task_ids, dataforseo_source = _submit_dataforseo_tasks(config, summary)

        # --- Phase 2: Fetch other sources (DataForSEO processes server-side) ---
        gmail_jobs = _fetch_gmail(config, runner_conn, summary)
        serpapi_jobs = _fetch_serpapi(config, summary)
        thordata_jobs = _fetch_thordata(config, summary)
        scaleserp_jobs = _fetch_scaleserp(config, summary)

        # --- Phase 3: Collect DataForSEO results (tasks likely complete by now) ---
        dataforseo_jobs = _collect_dataforseo_results(
            dataforseo_source, dataforseo_task_ids, summary
        )

        # --- Combine all jobs ---
        all_jobs = gmail_jobs + serpapi_jobs + thordata_jobs + scaleserp_jobs + dataforseo_jobs

        # --- Batch pre-check: skip scoring for already-known, non-archived jobs ---
        existing_statuses: dict[str, str] = {}
        candidate_keys = [job.dedup_key for job in all_jobs]
        if candidate_keys:
            # SQLite SQLITE_MAX_VARIABLE_NUMBER default is 999; chunk for safety
            for i in range(0, len(candidate_keys), 900):
                chunk = candidate_keys[i : i + 900]
                placeholders = ",".join("?" * len(chunk))
                rows = runner_conn.execute(
                    f"SELECT dedup_key, pipeline_status FROM jobs WHERE dedup_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for r in rows:
                    existing_statuses[r[0]] = r[1]

        skipped = 0
        full_scoring_count = 0
        # --- Score and persist each job (per-job error isolation) ---
        for job in all_jobs:
            if (
                job.dedup_key in existing_statuses
                and existing_statuses[job.dedup_key] != "archived"
                and job.salary_min is None
                and job.salary_max is None
            ):
                # Known, non-archived job with no new salary data — lightweight touch only
                try:
                    _touch_existing_job(job, runner_conn, summary)
                    skipped += 1
                except Exception as e:
                    logger.warning(
                        "Touch-update failed for '%s': %s — falling back to full scoring",
                        job.dedup_key,
                        e,
                    )
                    _score_and_persist(job, scorer, runner_conn, summary, new_job_keys)
                    full_scoring_count += 1
            else:
                _score_and_persist(job, scorer, runner_conn, summary, new_job_keys)
                full_scoring_count += 1

        logger.info(
            "Pre-dedup: %d known (touch-only), %d routed to full scoring",
            skipped,
            full_scoring_count,
        )

        # --- Log run totals to runs table ---
        jobs_new = summary["jobs_new"]
        jobs_scored = summary["jobs_scored"]

        if summary["gmail_fetched"] > 0 or summary["gmail_errors"]:
            try:
                log_run(runner_conn, "gmail", summary["gmail_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log Gmail run: %s", e)

        if summary["serpapi_fetched"] > 0 or summary["serpapi_errors"]:
            try:
                log_run(runner_conn, "serpapi", summary["serpapi_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log SerpAPI run: %s", e)

        if summary["thordata_fetched"] > 0 or summary["thordata_errors"]:
            try:
                log_run(runner_conn, "thordata", summary["thordata_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log Thordata run: %s", e)

        if summary["scaleserp_fetched"] > 0 or summary["scaleserp_errors"]:
            try:
                log_run(runner_conn, "scaleserp", summary["scaleserp_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log ScaleSerp run: %s", e)

        if summary["dataforseo_fetched"] > 0 or summary["dataforseo_errors"]:
            try:
                log_run(runner_conn, "dataforseo", summary["dataforseo_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log DataForSEO run: %s", e)

        # --- Prune stale data (TTL housekeeping) ---
        gmail_lookback = config.get("sources", {}).get("gmail", {}).get(
            "lookback_days", DEFAULT_LOOKBACK_DAYS
        )
        _prune_stale_data(runner_conn, gmail_lookback)

    # --- Two-tier AI scoring (runs after DB connection is closed) ---
    if new_job_keys and anthropic is not None:
        try:
            sonnet_queue, haiku_scored_count = run_haiku_scoring(new_job_keys, config, db_path)
            summary["haiku_scored"] = haiku_scored_count
            summary["sonnet_queue"] = sonnet_queue
            summary["sonnet_queued"] = len(sonnet_queue)
        except Exception as e:
            logger.error("Haiku scoring failed (non-fatal): %s", e)
            sonnet_queue = []

        # Run Sonnet evaluation for jobs above threshold
        if sonnet_queue:
            try:
                sonnet_evaluated = run_sonnet_evaluation(sonnet_queue, config, db_path)
                summary["sonnet_evaluated"] = sonnet_evaluated
            except Exception as e:
                logger.error("Sonnet evaluation failed (non-fatal): %s", e)
    elif new_job_keys and anthropic is None:
        logger.debug(
            "anthropic package not installed -- skipping AI scoring for %d new jobs",
            len(new_job_keys),
        )

    # --- Budget alert notification (check after AI scoring completes) ---
    _check_budget_alert(config, db_path)

    summary["duration_seconds"] = (datetime.now() - start_time).total_seconds()

    total_fetched = (
        summary["gmail_fetched"] + summary["serpapi_fetched"]
        + summary["thordata_fetched"] + summary["scaleserp_fetched"]
        + summary["dataforseo_fetched"]
    )
    logger.info(
        "Ingestion complete: %d fetched, %d new, %d touch-only, %d haiku-scored, %d sonnet-evaluated in %.1fs",
        total_fetched,
        summary["jobs_new"],
        summary["jobs_touch_only"],
        summary["haiku_scored"],
        summary["sonnet_evaluated"],
        summary["duration_seconds"],
    )

    return summary


def _fetch_gmail(config: dict, conn: sqlite3.Connection, summary: dict) -> list[Job]:
    """Fetch jobs from Gmail with per-message deduplication via email_parse_log.

    Before fetching, queries email_parse_log for message IDs already processed
    within the lookback window and passes them to GmailSource.fetch_jobs() so
    those messages are skipped entirely. After fetching, bulk-inserts the newly
    processed IDs so they are skipped on the next sync.

    Args:
        config: Full config dict.
        conn: SQLite connection for email_parse_log writes.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects parsed from Gmail.
    """
    gmail_config = config.get("sources", {}).get("gmail", {})
    if not gmail_config.get("enabled", True):
        logger.debug("Gmail source disabled in config.")
        return []

    run_id = f"gmail_run_{datetime.now().isoformat()}"
    lookback_days = gmail_config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)

    # --- Query known message IDs from email_parse_log ---
    known_ids: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT message_id FROM email_parse_log"
            " WHERE sender = 'gmail'"
            " AND processed_at >= datetime('now', ?)"
            " AND message_id NOT LIKE 'gmail_run_%'",
            (f"-{lookback_days} days",),
        ).fetchall()
        known_ids = {row[0] for row in rows}
        logger.debug("Gmail dedup: %d known message IDs in email_parse_log", len(known_ids))
    except Exception as e:
        logger.warning("Failed to query email_parse_log for dedup (proceeding without): %s", e)

    try:
        source = GmailSource()
        jobs, new_ids = source.fetch_jobs(
            lookback_days=lookback_days,
            processed_message_ids=known_ids,
        )
        summary["gmail_fetched"] = len(jobs)

        logger.info(
            "Gmail dedup: %d known, %d newly processed",
            len(known_ids),
            len(new_ids),
        )

        # --- Bulk-insert newly processed message IDs into email_parse_log ---
        # jobs_found=0 is a dedup-only placeholder for job-alert rows.  It does
        # NOT mean "this email had zero jobs" — it simply marks the message as
        # seen so the next sync can skip it.  This value must never be used for
        # analytics; use the jobs table directly for per-source job counts.
        if new_ids:
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO email_parse_log"
                    " (message_id, sender, processed_at, jobs_found)"
                    " VALUES (?, 'gmail', datetime('now'), 0)",
                    [(mid,) for mid in new_ids],
                )
                conn.commit()
                logger.debug(
                    "Gmail dedup: inserted %d message IDs into email_parse_log", len(new_ids)
                )
            except Exception as e:
                logger.warning("Failed to bulk-insert message IDs into email_parse_log: %s", e)

        # --- Log parse failure activity feed entries ---
        # (per locked decision: "Non-meta emails that parse to zero jobs create
        # an activity feed entry")
        # Duplicate-failure protection is implicit: messages already in
        # email_parse_log are filtered out by the dedup query above, so
        # parse_failures only contains newly processed messages.
        for failure in getattr(source, "parse_failures", []):
            try:
                fail_sender = failure.get("sender", "unknown")
                domain = (
                    fail_sender.split("@")[-1].replace(".", "_")
                    if "@" in fail_sender
                    else fail_sender
                )
                conn.execute(
                    "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)"
                    " VALUES (?, ?, 0, 0, 0)",
                    (datetime.now().isoformat(), f"{domain}_parse_failure"),
                )
                conn.commit()
                logger.debug("Zero-job email routed to activity feed: %s", fail_sender)
            except Exception as e:
                logger.warning("Failed to log parse failure to runs: %s", e)

        # --- Log the run-level summary to email_parse_log ---
        _log_to_email_parse_log(conn, run_id, "gmail", len(jobs), None)

        logger.info("Gmail: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["gmail_errors"].append(error_msg)
        logger.warning("Gmail ingestion failed: %s", error_msg)

        # Log the failure
        _log_to_email_parse_log(conn, run_id, "gmail", 0, error_msg)

        return []


def _fetch_serpapi(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from SerpAPI with error isolation.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from SerpAPI.
    """
    serpapi_config = config.get("sources", {}).get("serpapi", {})
    if not serpapi_config.get("enabled", False):
        logger.debug("SerpAPI source disabled in config.")
        return []

    api_key = serpapi_config.get("api_key", "")
    if not api_key:
        msg = "SerpAPI key not configured"
        summary["serpapi_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = serpapi_config.get("queries", [])
    if not queries:
        logger.debug("No SerpAPI queries configured.")
        return []

    try:
        from job_finder.sources.serpapi_source import SerpAPISource

        source = SerpAPISource(api_key)
        jobs = source.fetch_jobs(queries)
        summary["serpapi_fetched"] = len(jobs)

        logger.info("SerpAPI: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["serpapi_errors"].append(error_msg)
        logger.warning("SerpAPI ingestion failed: %s", error_msg)
        return []


def _fetch_thordata(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from Thordata Google Jobs SERP API with error isolation.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from Thordata.
    """
    thordata_config = config.get("sources", {}).get("thordata", {})
    if not thordata_config.get("enabled", False):
        logger.debug("Thordata source disabled in config.")
        return []

    api_key = thordata_config.get("api_key", "")
    if not api_key:
        msg = "Thordata API key not configured"
        summary["thordata_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = thordata_config.get("queries", [])
    if not queries:
        logger.debug("No Thordata queries configured.")
        return []

    max_age_days = thordata_config.get("max_age_days", 3)

    try:
        from job_finder.sources.thordata_source import ThordataSource

        source = ThordataSource(api_key, max_age_days=max_age_days)
        jobs = source.fetch_jobs(queries)
        summary["thordata_fetched"] = len(jobs)

        logger.info("Thordata: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["thordata_errors"].append(error_msg)
        logger.warning("Thordata ingestion failed: %s", error_msg)
        return []


def _fetch_scaleserp(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from ScaleSerp Google Jobs API with error isolation.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from ScaleSerp.
    """
    scaleserp_config = config.get("sources", {}).get("scaleserp", {})
    if not scaleserp_config.get("enabled", False):
        logger.debug("ScaleSerp source disabled in config.")
        return []

    api_key = scaleserp_config.get("api_key", "")
    if not api_key:
        msg = "ScaleSerp key not configured"
        summary["scaleserp_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = scaleserp_config.get("queries", [])
    if not queries:
        logger.debug("No ScaleSerp queries configured.")
        return []

    try:
        from job_finder.sources.scaleserp_source import ScaleSerpSource

        source = ScaleSerpSource(api_key)
        jobs = source.fetch_jobs(queries)
        summary["scaleserp_fetched"] = len(jobs)

        logger.info("ScaleSerp: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["scaleserp_errors"].append(error_msg)
        logger.warning("ScaleSerp ingestion failed: %s", error_msg)
        return []


def _submit_dataforseo_tasks(
    config: dict, summary: dict
) -> tuple[list[str], Optional["DataForSEOSource"]]:
    """Submit DataForSEO tasks early (non-blocking ~2s).

    Returns (task_ids, source_instance). The source is returned so
    _collect_dataforseo_results can reuse it without re-extracting config.
    Returns ([], None) if source is disabled, unconfigured, or submission fails.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        Tuple of (list of task ID strings, DataForSEOSource instance or None).
    """
    dataforseo_config = config.get("sources", {}).get("dataforseo", {})
    if not dataforseo_config.get("enabled", False):
        logger.debug("DataForSEO source disabled in config.")
        return [], None

    api_key = dataforseo_config.get("api_key", "")
    if not api_key:
        msg = "DataForSEO API key not configured"
        summary["dataforseo_errors"].append(msg)
        logger.warning(msg)
        return [], None

    queries = dataforseo_config.get("queries", [])
    if not queries:
        logger.debug("No DataForSEO queries configured.")
        return [], None

    try:
        from job_finder.sources.dataforseo_source import DataForSEOSource

        source = DataForSEOSource(
            api_key,
            max_age_days=dataforseo_config.get("max_age_days", 7),
            depth=dataforseo_config.get("depth", 20),
            priority=dataforseo_config.get("priority", 1),
            poll_interval_seconds=dataforseo_config.get("poll_interval_seconds", 30),
            poll_timeout_seconds=dataforseo_config.get("poll_timeout_seconds", 360),
        )
        task_ids = source.submit_tasks(queries)
        if not task_ids:
            msg = f"DataForSEO: submit returned no task IDs for {len(queries)} queries (all tasks rejected)"
            summary["dataforseo_errors"].append(msg)
            logger.warning("DataForSEO: submit_tasks returned no task IDs for %d queries", len(queries))
            return [], None
        logger.info("DataForSEO: submitted %d tasks (non-blocking)", len(task_ids))
        return task_ids, source

    except Exception as e:
        summary["dataforseo_errors"].append(str(e))
        logger.warning("DataForSEO task submission failed: %s", e)
        return [], None


def _collect_dataforseo_results(
    source: Optional["DataForSEOSource"],
    task_ids: list[str],
    summary: dict,
) -> list[Job]:
    """Collect results for previously submitted DataForSEO tasks.

    Args:
        source: DataForSEOSource instance returned by _submit_dataforseo_tasks,
                or None if submission was skipped/failed.
        task_ids: Task UUIDs returned by submit_tasks().
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects. Empty if source is None or task_ids is empty.
    """
    if not source or not task_ids:
        return []

    try:
        t0 = time.monotonic()
        jobs = source.collect_results(task_ids)
        elapsed = time.monotonic() - t0
        summary["dataforseo_fetched"] = len(jobs)
        logger.info("DataForSEO collect: %.1fs, %d jobs", elapsed, len(jobs))
        return jobs

    except Exception as e:
        summary["dataforseo_errors"].append(str(e))
        logger.warning("DataForSEO result collection failed: %s", e)
        return []


def _score_and_persist(
    job: Job, scorer: JobScorer, conn, summary: dict, new_job_keys: list[str]
) -> None:
    """Score a single job and persist it. Errors are logged but not re-raised.

    Per-job error isolation: if scoring or persistence fails for one job,
    processing continues for the remaining jobs.

    Args:
        job: Job object to score and persist.
        scorer: Initialized JobScorer instance.
        conn: Open sqlite3 connection.
        summary: Mutable summary dict to update.
        new_job_keys: Mutable list; new job dedup_keys are appended here.
    """
    try:
        # Score the job (updates job.score and job.score_breakdown in place)
        scored_job = scorer.score_jobs([job])
        if scored_job:
            job = scored_job[0]
            summary["jobs_scored"] += 1

        # Persist (upsert handles dedup by dedup_key)
        is_new = upsert_job(conn, job)
        if is_new:
            summary["jobs_new"] += 1
            new_job_keys.append(job.dedup_key)
        else:
            summary["jobs_updated"] += 1

        # Company auto-population: create/update company record for every job
        _upsert_job_company(conn, job)

    except Exception as e:
        error_msg = f"{job.title} @ {job.company}: {e}"
        summary["job_errors"].append(error_msg)
        logger.warning(
            "Failed to score/persist job '%s' at '%s': %s", job.title, job.company, e
        )


def _touch_existing_job(job: Job, conn: sqlite3.Connection, summary: dict) -> None:
    """Lightweight update for already-known jobs: touch last_seen and merge source/source_url.

    Skips scoring, full upsert merge logic, and company upsert for jobs whose
    dedup_key already exists in the DB and whose incoming data carries no new
    salary information worth merging. Updates last_seen timestamp and merges
    the incoming job.source and job.source_url into the existing JSON columns
    without duplicates.

    Args:
        job: Incoming Job with a dedup_key already present in DB.
        conn: Open sqlite3 connection.
        summary: Mutable summary dict — increments ``jobs_touch_only``.
    """
    conn.execute(
        """UPDATE jobs
           SET last_seen = ?,
               sources = (
                   SELECT json_group_array(value)
                   FROM (
                       -- UNION (not UNION ALL) provides set-semantics, deduplicating sources
                       SELECT value FROM json_each(sources)
                       UNION
                       SELECT value FROM json_each(json_array(?))
                   )
               ),
               source_urls = (
                   SELECT json_group_array(value)
                   FROM (
                       -- UNION (not UNION ALL) provides set-semantics, deduplicating URLs
                       -- CASE WHEN handles empty source_url: both ? bind job.source_url
                       SELECT value FROM json_each(source_urls)
                       UNION
                       SELECT value FROM json_each(CASE WHEN ? != '' THEN json_array(?) ELSE '[]' END)
                   )
               )
           WHERE dedup_key = ?""",
        (utc_now_iso(), job.source, job.source_url, job.source_url, job.dedup_key),
    )
    conn.commit()
    summary["jobs_touch_only"] += 1


def _upsert_job_company(conn, job: Job) -> None:
    """Create or update the company record associated with a job.

    Non-fatal: any error is logged at DEBUG level and does not crash ingestion.
    Lazy import to avoid circular: ats_scanner → dedup_normalizer → pipeline_runner.

    Args:
        conn: Open sqlite3 connection.
        job: Job object whose company should be upserted.
    """
    try:
        from job_finder.web.ats_detection import extract_ats_from_urls
        from job_finder.web.ats_scanner import upsert_company
    except ImportError:
        return

    try:
        # job.source_url is a single URL string; wrap in list for extract_ats_from_urls
        source_url = job.source_url or ""
        source_urls = [source_url] if source_url else []
        ats_platform, ats_slug = extract_ats_from_urls(source_urls)
        probe_status = "hit" if ats_slug else "pending"
        company_id = upsert_company(
            conn,
            name=job.company,
            ats_platform=ats_platform,
            ats_slug=ats_slug,
            ats_probe_status=probe_status,
        )
        if company_id:
            conn.execute(
                "UPDATE jobs SET company_id = ? WHERE dedup_key = ?",
                (company_id, job.dedup_key),
            )
            conn.commit()
    except Exception as company_err:
        logger.debug(
            "Company upsert failed for '%s' (non-fatal): %s",
            job.company,
            company_err,
        )


def _prune_stale_data(conn: sqlite3.Connection, lookback_days: int = 7) -> None:
    """Prune stale entries from both the runs and email_parse_log tables.

    Covers two tables:

    **runs table** — accumulates ~1,000 rows/day from parse_failure entries:
    - parse_failure rows older than 30 days are deleted
    - All rows older than 90 days are deleted

    **email_parse_log table** — stores per-message Gmail dedup rows and
    run-level summary rows (sender='gmail', message_id='gmail_run_...');
    both are pruned at the same TTL:
    - Rows with sender='gmail' older than ``max(lookback_days * 2, 14)`` days
      are deleted. The TTL is at least 14 days and scales with lookback_days
      so that dedup records are never pruned while Gmail still returns those
      emails on the next sync.

    Non-fatal: any error is logged at WARNING level and does not interrupt
    ingestion.

    Args:
        conn: Active SQLite connection.
        lookback_days: Gmail lookback window (from config). Used to compute
            the email_parse_log TTL as max(lookback_days * 2, 14).
    """
    email_parse_log_ttl = max(lookback_days * 2, 14)
    try:
        conn.execute(
            "DELETE FROM runs"
            " WHERE timestamp < datetime('now', '-30 days')"
            " AND source LIKE '%parse_failure%'"
        )
        conn.execute(
            "DELETE FROM runs WHERE timestamp < datetime('now', '-90 days')"
        )
        # Trim email_parse_log rows (both per-message dedup rows and run-level
        # summary rows with sender='gmail').  TTL scales with lookback_days so
        # dedup records are never expired while Gmail still returns those emails.
        # At ~300 rows/run × 3 runs/day that's ~109K rows/year without pruning.
        conn.execute(
            "DELETE FROM email_parse_log"
            " WHERE processed_at < datetime('now', ?)"
            " AND sender = 'gmail'",
            (f"-{email_parse_log_ttl} days",),
        )
        conn.commit()
        logger.debug(
            "Pruned stale runs and email_parse_log rows (email_parse_log TTL: %d days)",
            email_parse_log_ttl,
        )
    except Exception as e:
        logger.warning("Failed to prune stale data: %s", e)


def _log_to_email_parse_log(
    conn: sqlite3.Connection,
    message_id: str,
    sender: str,
    jobs_found: int,
    error: Optional[str],
) -> None:
    """Insert a record into email_parse_log.

    Uses INSERT OR IGNORE so re-runs with the same message_id don't fail.

    Args:
        conn: Active SQLite connection.
        message_id: Unique ID for this log entry (run-level or message-level).
        sender: Source label (e.g., "gmail", "no-reply@ziprecruiter.com").
        jobs_found: Number of jobs parsed from this email/run.
        error: Error message if parsing failed, else None.
    """
    try:
        conn.execute(
            """INSERT OR IGNORE INTO email_parse_log
               (message_id, sender, processed_at, jobs_found, error)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, sender, datetime.now().isoformat(), jobs_found, error),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to write to email_parse_log: %s", e)


def _check_budget_alert(config: dict, db_path: str) -> None:
    """Check monthly AI spend and fire budget alert notification if thresholds crossed.

    Tracks the last notified threshold level via module-level variable to avoid
    repeated notifications within a single app session. Resets on app restart.

    Args:
        config: Full JF_CONFIG dict (reads scoring.monthly_budget_usd and
                notifications.budget_alert toggle).
        db_path: Path to the SQLite database file.
    """
    global _last_budget_pct_notified

    budget_cap = config.get("scoring", {}).get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)
    if not budget_cap or budget_cap <= 0:
        return

    try:
        with standalone_connection(db_path) as conn:
            from job_finder.web.claude_client import get_cost_stats
            stats = get_cost_stats(conn)

        monthly_spend = stats.get("month", 0.0)
        if monthly_spend <= 0:
            return

        pct = (monthly_spend / budget_cap) * 100

        from job_finder.web.notifier import notify_budget_alert

        with _budget_alert_lock:
            if pct >= 100 and _last_budget_pct_notified < 100:
                notify_budget_alert(pct, config)
                _last_budget_pct_notified = 100
            elif pct >= 80 and _last_budget_pct_notified < 80:
                notify_budget_alert(pct, config)
                _last_budget_pct_notified = 80

    except Exception as e:
        logger.debug("Budget alert check failed (non-fatal): %s", e)
