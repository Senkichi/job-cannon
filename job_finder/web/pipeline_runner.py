"""Ingestion pipeline runner for the Flask web app.

Orchestrates Gmail + SerpAPI ingestion with:
- Run-level tracking via email_parse_log (prevents redundant log entries)
- Per-source error isolation (Gmail failure does not stop SerpAPI)
- Per-job error isolation (one bad job does not halt persistence)
- Scoring via JobScorer before persistence
- Deduplication via db.upsert_job (dedup_key: company|title|location)
- Two-tier AI scoring: v3.0 unified scoring via `run_scoring` for jobs above
  `scoring.candidate_score_threshold` (heuristic pre-filter).

Thread-safety: Creates a NEW sqlite3 connection per call. This function runs
in the APScheduler background thread -- it must NOT share a connection with
the Flask request thread.
"""

import logging
from datetime import datetime

from job_finder.db import log_run
from job_finder.scoring.scorer import JobScorer
from job_finder.web.db_helpers import standalone_connection

# v3.0 (Phase 34 Plan 3 Commit E): the unified run_scoring is imported lazily
# inside run_ingestion(). The legacy run_haiku_scoring / run_sonnet_evaluation
# imports are gone — they remain available on scoring_runner for Plan 4 to
# delete alongside the legacy scorer modules.

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
from job_finder.web.ingestion_runner import (  # noqa: F401
    _collect_dataforseo_results,
    _fetch_gmail,
    _fetch_imap,
    _fetch_portal_search,
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
            - jobs_scored: int (heuristic score applied during upsert)
            - job_errors: list[str]
            - scored: int (count of rows routed through score_and_persist_job)
            - classified_apply: int
            - classified_consider: int
            - classified_skip: int
            - classified_reject: int
            - duration_seconds: float

        v3.0 (Phase 34 Plan 3 Commit E): the legacy haiku_scored /
        sonnet_queued / sonnet_queue / sonnet_evaluated keys are removed.
        The unified-scorer path is the only remaining branch; the
        use_unified_scorer config flag is honored but the else-branch is
        gone. Plan 4 removes the flag itself.
    """
    start_time = datetime.now()
    summary = {
        "gmail_fetched": 0,
        "gmail_errors": [],
        "imap_fetched": 0,
        "imap_errors": [],
        "serpapi_fetched": 0,
        "serpapi_errors": [],
        "thordata_fetched": 0,
        "thordata_errors": [],
        "dataforseo_fetched": 0,
        "dataforseo_errors": [],
        "portal_search_fetched": 0,
        "portal_search_errors": [],
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_scored": 0,
        "job_errors": [],
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
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

        # --- Email ingestion: IMAP (default) or Gmail (opt-in) ---
        if config.get("sources", {}).get("imap", {}).get("enabled", False):
            imap_jobs = _fetch_imap(config, summary)
            gmail_jobs = []
        else:
            gmail_jobs = _fetch_gmail(config, runner_conn, summary)
            imap_jobs = []

        # --- SerpAPI ingestion ---
        serpapi_jobs = _fetch_serpapi(config, summary)

        # --- Additional sources ---
        thordata_jobs = _fetch_thordata(config, summary)
        portal_jobs = _fetch_portal_search(config, summary)

        # --- DataForSEO: collect results (blocks until ready or timeout) ---
        dataforseo_jobs = _collect_dataforseo_results(dfse_source, dfse_task_ids, summary)

        # --- Combine all jobs ---
        all_jobs = (
            imap_jobs + gmail_jobs + serpapi_jobs + thordata_jobs + portal_jobs + dataforseo_jobs
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

        if summary["imap_fetched"] > 0 or summary["imap_errors"]:
            try:
                log_run(runner_conn, "imap", summary["imap_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log IMAP run: %s", e)

        if summary["serpapi_fetched"] > 0 or summary["serpapi_errors"]:
            try:
                log_run(runner_conn, "serpapi", summary["serpapi_fetched"], jobs_new, jobs_scored)
            except Exception as e:
                logger.warning("Failed to log SerpAPI run: %s", e)

        if summary["dataforseo_fetched"] > 0 or summary["dataforseo_errors"]:
            try:
                log_run(
                    runner_conn, "dataforseo", summary["dataforseo_fetched"], jobs_new, jobs_scored
                )
            except Exception as e:
                logger.warning("Failed to log DataForSEO run: %s", e)

    # --- AI scoring (runs after DB connection is closed) ---
    # v3.0 unified scoring is the only path (Plan 4 Commit E removed the
    # use_unified_scorer toggle and the legacy two-tier else-branch).
    if score and new_job_keys:
        from job_finder.web.scoring_runner import run_scoring

        scoring_summary = run_scoring(new_job_keys, config, db_path)
        summary["scored"] = scoring_summary.get("scored", 0)
        summary["classified_apply"] = scoring_summary.get("classified_apply", 0)
        summary["classified_consider"] = scoring_summary.get("classified_consider", 0)
        summary["classified_skip"] = scoring_summary.get("classified_skip", 0)
        summary["classified_reject"] = scoring_summary.get("classified_reject", 0)

    summary["duration_seconds"] = (datetime.now() - start_time).total_seconds()

    total_fetched = (
        summary["gmail_fetched"]
        + summary["serpapi_fetched"]
        + summary.get("thordata_fetched", 0)
        + summary.get("dataforseo_fetched", 0)
        + summary.get("portal_search_fetched", 0)
    )
    logger.info(
        "Ingestion complete: %d fetched, %d new, %d scored "
        "(apply=%d, consider=%d, skip=%d, reject=%d) in %.1fs",
        total_fetched,
        summary["jobs_new"],
        summary["scored"],
        summary["classified_apply"],
        summary["classified_consider"],
        summary["classified_skip"],
        summary["classified_reject"],
        summary["duration_seconds"],
    )

    return summary
