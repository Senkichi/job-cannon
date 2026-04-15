"""Ingestion pipeline runner for the Flask web app.

Orchestrates Gmail + SerpAPI ingestion with:
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

# Re-export all helpers from ingestion_runner so existing patch paths work:
#   patch("job_finder.web.pipeline_runner._fetch_gmail", ...)  ← still valid
#   patch("job_finder.web.pipeline_runner.GmailSource", ...)   ← still valid (imported above)
#   patch("job_finder.web.pipeline_runner.upsert_job", ...)    ← still valid (imported above)
from job_finder.web.ingestion_runner import (  # noqa: E402
    _collect_dataforseo_results,
    _fetch_gmail,
    _fetch_portal_search,
    _fetch_scaleserp,
    _fetch_serpapi,
    _fetch_thordata,
    _log_to_email_parse_log,
    _prune_stale_data,
    _score_and_persist,
    _submit_dataforseo_tasks,
    _touch_existing_job,
    _upsert_job_company,
)

logger = logging.getLogger(__name__)

# Track last notified budget threshold to avoid repeated notifications.
# Reset on app restart (acceptable for single-user app).
# Thread-safety: ALL reads and writes of _last_budget_pct_notified MUST occur
# inside a `with _budget_alert_lock:` block to prevent data races.
_budget_alert_lock = threading.Lock()
_last_budget_pct_notified: float = 0.0

def run_ingestion(db_path: str, config: dict, *, score: bool = True) -> dict:
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
        "portal_search_fetched": 0,
        "portal_search_errors": [],
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
        # --- DataForSEO: submit tasks early (non-blocking ~2s) ---
        # Async task-queue API: submit now, collect after other sources finish.
        # Other sources run while DataForSEO processes server-side (~60-90s).
        dfse_task_ids, dfse_source = _submit_dataforseo_tasks(config, summary)

        # --- Gmail ingestion ---
        gmail_jobs = _fetch_gmail(config, runner_conn, summary)

        # --- SerpAPI ingestion ---
        serpapi_jobs = _fetch_serpapi(config, summary)

        # --- Additional sources ---
        thordata_jobs = _fetch_thordata(config, summary)
        scaleserp_jobs = _fetch_scaleserp(config, summary)
        portal_jobs = _fetch_portal_search(config, summary)

        # --- DataForSEO: collect results (blocks until ready or timeout) ---
        dataforseo_jobs = _collect_dataforseo_results(dfse_source, dfse_task_ids, summary)

        # --- Combine all jobs ---
        all_jobs = (
            gmail_jobs + serpapi_jobs + thordata_jobs
            + scaleserp_jobs + portal_jobs + dataforseo_jobs
        )

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

        if summary["dataforseo_fetched"] > 0 or summary["dataforseo_errors"]:
            try:
                log_run(runner_conn, "dataforseo", summary["dataforseo_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log DataForSEO run: %s", e)

    # --- Two-tier AI scoring (runs after DB connection is closed) ---
    if score and new_job_keys:
        sonnet_queue, haiku_scored_count = run_haiku_scoring(new_job_keys, config, db_path)
        summary["haiku_scored"] = haiku_scored_count
        summary["sonnet_queue"] = sonnet_queue
        summary["sonnet_queued"] = len(sonnet_queue)

        # Run Sonnet evaluation for jobs above threshold
        if sonnet_queue:
            sonnet_evaluated = run_sonnet_evaluation(sonnet_queue, config, db_path)
            summary["sonnet_evaluated"] = sonnet_evaluated
    # --- Budget alert notification (check after AI scoring completes) ---
    _check_budget_alert(config, db_path)

    summary["duration_seconds"] = (datetime.now() - start_time).total_seconds()

    total_fetched = (
        summary["gmail_fetched"] + summary["serpapi_fetched"]
        + summary.get("thordata_fetched", 0) + summary.get("scaleserp_fetched", 0)
        + summary.get("dataforseo_fetched", 0) + summary.get("portal_search_fetched", 0)
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
