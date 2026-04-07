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

Private helpers live in ingestion_runner.py and are re-exported here so that
existing patch paths (job_finder.web.pipeline_runner.*) continue to work
without any test changes.
"""

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from job_finder.config import DEFAULT_LOOKBACK_DAYS
from job_finder.db import upsert_job, log_run
from job_finder.scoring.scorer import JobScorer
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_runner import run_haiku_scoring, run_sonnet_evaluation

try:
    from job_finder.sources.gmail_source import GmailSource
except ImportError:
    GmailSource = None  # type: ignore[assignment,misc]

# Re-export all helpers from ingestion_runner so existing patch paths work:
#   patch("job_finder.web.pipeline_runner._fetch_gmail", ...)  ← still valid
#   patch("job_finder.web.pipeline_runner.GmailSource", ...)   ← still valid (imported above)
#   patch("job_finder.web.pipeline_runner.anthropic", ...)     ← still valid (imported above)
#   patch("job_finder.web.pipeline_runner.upsert_job", ...)    ← still valid (imported above)
from job_finder.web.ingestion_runner import (  # noqa: E402
    _collect_dataforseo_results,
    _fetch_gmail,
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
    # Scoring functions now self-gate on tier_has_configured_provider() —
    # no need to check Anthropic availability at the pipeline level.
    if new_job_keys:
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


def _check_budget_alert(config: dict, db_path: str) -> None:
    """Check daily AI spend and fire budget alert notification if thresholds crossed.

    Tracks the last notified threshold level via module-level variable to avoid
    repeated notifications within a single app session. Resets on app restart.

    Args:
        config: Full JF_CONFIG dict (reads scoring.daily_budget_usd and
                notifications.budget_alert toggle).
        db_path: Path to the SQLite database file.
    """
    global _last_budget_pct_notified

    from job_finder.web.claude_client import DEFAULT_DAILY_BUDGET_USD
    budget_cap = config.get("scoring", {}).get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    if not budget_cap or budget_cap <= 0:
        return

    try:
        with standalone_connection(db_path) as conn:
            from job_finder.web.claude_client import get_cost_stats
            stats = get_cost_stats(conn)

        daily_spend = stats.get("today", 0.0)
        if daily_spend <= 0:
            return

        pct = (daily_spend / budget_cap) * 100

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
