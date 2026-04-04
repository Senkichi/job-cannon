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
from datetime import datetime
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from job_finder.config import DEFAULT_LOOKBACK_DAYS, DEFAULT_MONTHLY_BUDGET_USD
from job_finder.db import upsert_job, log_run
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
        # --- Gmail ingestion ---
        gmail_jobs = _fetch_gmail(config, runner_conn, summary)

        # --- SerpAPI ingestion ---
        serpapi_jobs = _fetch_serpapi(config, summary)

        # --- Thordata ingestion ---
        thordata_jobs = _fetch_thordata(config, summary)

        # --- ScaleSerp ingestion ---
        scaleserp_jobs = _fetch_scaleserp(config, summary)

        # --- DataForSEO ingestion ---
        dataforseo_jobs = _fetch_dataforseo(config, summary)

        # --- Combine all jobs ---
        all_jobs = gmail_jobs + serpapi_jobs + thordata_jobs + scaleserp_jobs + dataforseo_jobs

        # --- Score and persist each job (per-job error isolation) ---
        for job in all_jobs:
            _score_and_persist(job, scorer, runner_conn, summary, new_job_keys)

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
        "Ingestion complete: %d fetched, %d new, %d haiku-scored, %d sonnet-evaluated in %.1fs",
        total_fetched,
        summary["jobs_new"],
        summary["haiku_scored"],
        summary["sonnet_evaluated"],
        summary["duration_seconds"],
    )

    return summary


def _fetch_gmail(config: dict, conn: sqlite3.Connection, summary: dict) -> list[Job]:
    """Fetch jobs from Gmail with run-level email_parse_log tracking.

    Uses a run-level log entry (not per-message) to track that Gmail was polled.
    Per-message dedup is handled by upsert_job's dedup_key logic.

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

    try:
        source = GmailSource()
        jobs = source.fetch_jobs(lookback_days=lookback_days)
        summary["gmail_fetched"] = len(jobs)

        # Log parse failure activity feed entries
        # (per locked decision: "Non-meta emails that parse to zero jobs create
        # an activity feed entry")
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

        # Log the run to email_parse_log
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


def _fetch_dataforseo(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from DataForSEO Google Jobs SERP API with error isolation.

    Uses async task queue (no live endpoint). Submits all queries as a single
    POST batch, then polls tasks_ready until all complete or timeout is reached.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from DataForSEO.
    """
    dataforseo_config = config.get("sources", {}).get("dataforseo", {})
    if not dataforseo_config.get("enabled", False):
        logger.debug("DataForSEO source disabled in config.")
        return []

    api_key = dataforseo_config.get("api_key", "")
    if not api_key:
        msg = "DataForSEO API key not configured"
        summary["dataforseo_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = dataforseo_config.get("queries", [])
    if not queries:
        logger.debug("No DataForSEO queries configured.")
        return []

    max_age_days = dataforseo_config.get("max_age_days", 7)
    depth = dataforseo_config.get("depth", 20)
    priority = dataforseo_config.get("priority", 1)
    poll_interval = dataforseo_config.get("poll_interval_seconds", 30)
    poll_timeout = dataforseo_config.get("poll_timeout_seconds", 360)

    try:
        from job_finder.sources.dataforseo_source import DataForSEOSource

        source = DataForSEOSource(
            api_key,
            max_age_days=max_age_days,
            depth=depth,
            priority=priority,
            poll_interval_seconds=poll_interval,
            poll_timeout_seconds=poll_timeout,
        )
        jobs = source.fetch_jobs(queries)
        summary["dataforseo_fetched"] = len(jobs)

        logger.info("DataForSEO: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["dataforseo_errors"].append(error_msg)
        logger.warning("DataForSEO ingestion failed: %s", error_msg)
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
