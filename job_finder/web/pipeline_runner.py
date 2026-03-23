"""Ingestion pipeline runner for the Flask web app.

Orchestrates Gmail + SerpAPI ingestion with:
- Run-level tracking via email_parse_log (prevents redundant log entries)
- Per-source error isolation (Gmail failure does not stop SerpAPI)
- Per-job error isolation (one bad job does not halt persistence)
- Scoring via JobScorer before persistence
- Deduplication via JobDB.upsert_job (dedup_key: company|title|location)
- Two-tier AI scoring: Haiku fast-filter for all new jobs, Sonnet deep-eval
  for jobs above haiku_threshold.

Thread-safety: Creates a NEW sqlite3 connection per call. This function runs
in the APScheduler background thread -- it must NOT share a connection with
the Flask request thread.
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from job_finder.config import DEFAULT_HAIKU_THRESHOLD, DEFAULT_LOOKBACK_DAYS, DEFAULT_MONTHLY_BUDGET_USD
from job_finder.db import upsert_job, log_run
from job_finder.models import Job
from job_finder.scoring.scorer import JobScorer
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.haiku_scorer import score_job_haiku

try:
    from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
except ImportError:
    evaluate_job_sonnet = None  # type: ignore[assignment]

try:
    from job_finder.web.data_enricher import enrich_job, enrich_company_info
except ImportError:
    enrich_job = None  # type: ignore[assignment]
    enrich_company_info = None  # type: ignore[assignment]

# ats_scanner import is lazy (inside _score_and_persist) to avoid circular import:
# ats_scanner → dedup_normalizer → pipeline_runner → ats_scanner (partial)

try:
    from job_finder.sources.gmail_source import GmailSource
except ImportError:
    GmailSource = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Track last notified budget threshold to avoid repeated notifications.
# Reset on app restart (acceptable for single-user app).
_last_budget_pct_notified: float = 0.0


def run_ingestion(config: dict, db_path: str) -> dict:
    """Run the full ingestion pipeline: fetch -> score -> dedup -> persist -> AI score.

    Creates its own SQLite connection (thread-safe: called from APScheduler
    background thread, not the Flask request thread).

    Args:
        config: Full JF_CONFIG dict (profile + scoring + sources sections).
        db_path: Absolute path to the SQLite database file.

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
    import sqlite3
    runner_conn = sqlite3.connect(db_path)
    runner_conn.row_factory = sqlite3.Row
    scorer = JobScorer(config)

    try:
        # --- Gmail ingestion ---
        gmail_jobs = _fetch_gmail(config, runner_conn, summary)

        # --- SerpAPI ingestion ---
        serpapi_jobs = _fetch_serpapi(config, summary)

        # --- Combine all jobs ---
        all_jobs = gmail_jobs + serpapi_jobs

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

    finally:
        # Always close the connection to release the file lock (important on Windows)
        try:
            runner_conn.close()
        except Exception:
            logger.debug("db close failed during ingestion", exc_info=True)

    # --- Two-tier AI scoring (runs after DB connection is closed) ---
    if new_job_keys and anthropic is not None:
        sonnet_queue, haiku_scored_count = _run_haiku_scoring(new_job_keys, config, db_path)
        summary["haiku_scored"] = haiku_scored_count
        summary["sonnet_queue"] = sonnet_queue
        summary["sonnet_queued"] = len(sonnet_queue)

        # Run Sonnet evaluation for jobs above threshold
        if sonnet_queue:
            sonnet_evaluated = _run_sonnet_evaluation(sonnet_queue, config, db_path)
            summary["sonnet_evaluated"] = sonnet_evaluated
    elif new_job_keys and anthropic is None:
        logger.debug(
            "anthropic package not installed -- skipping AI scoring for %d new jobs",
            len(new_job_keys),
        )

    # --- Budget alert notification (check after AI scoring completes) ---
    _check_budget_alert(config, db_path)

    summary["duration_seconds"] = (datetime.now() - start_time).total_seconds()

    total_fetched = summary["gmail_fetched"] + summary["serpapi_fetched"]
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
        # Non-fatal: company upsert failure does not crash ingestion
        # Lazy import to avoid circular: ats_scanner → dedup_normalizer → pipeline_runner
        try:
            from job_finder.web.ats_scanner import extract_ats_from_urls, upsert_company
        except ImportError:
            extract_ats_from_urls = None  # type: ignore[assignment]
            upsert_company = None  # type: ignore[assignment]
        if upsert_company is not None:
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

    except Exception as e:
        error_msg = f"{job.title} @ {job.company}: {e}"
        summary["job_errors"].append(error_msg)
        logger.warning(
            "Failed to score/persist job '%s' at '%s': %s", job.title, job.company, e
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


def _load_profile(config: dict) -> dict:
    """Load experience profile from disk.

    Args:
        config: Application config dict. Reads scoring.profile_path or defaults
                to "experience_profile.json" in the working directory.

    Returns:
        Profile dict, or empty dict if file not found.
    """
    profile_path = (
        config.get("scoring", {}).get("profile_path")
        or "experience_profile.json"
    )
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Could not load experience profile from '%s': %s", profile_path, e)
        return {}


def _run_haiku_scoring(
    new_job_keys: list[str],
    config: dict,
    db_path: str,
) -> tuple[list[str], int]:
    """Run Haiku fast-filter scoring for a batch of new jobs.

    Creates its own SQLite connection (thread-safe pattern matching run_ingestion).
    For each job in new_job_keys: fetches the row, calls score_job_haiku,
    writes haiku_score and haiku_summary back to the DB.

    Args:
        new_job_keys: List of dedup_key strings for newly-persisted jobs.
        config: Application config dict.
        db_path: Absolute path to the SQLite database file.

    Returns:
        Tuple of (sonnet_queue, haiku_scored_count):
            sonnet_queue: dedup_keys where haiku_score >= haiku_threshold.
            haiku_scored_count: Number of jobs successfully scored.
    """
    if not new_job_keys:
        return [], 0

    if anthropic is None:
        logger.debug("anthropic not installed -- skipping Haiku scoring")
        return [], 0

    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    profile = _load_profile(config)
    client = anthropic.Anthropic()
    sonnet_queue: list[str] = []
    haiku_scored = 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    logger.warning("Haiku: job '%s' not found in DB -- skipping", dedup_key)
                    continue

                job_row = dict(row)

                # --- Enrichment FIRST (before scoring) ---
                if enrich_job is not None and (
                    not job_row.get("jd_full") or job_row.get("salary_min") is None
                ):
                    try:
                        serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
                        enriched = enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            anthropic_client=client,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            # enrich_job already persisted to DB if conn was provided.
                            # Update job_row in memory so Haiku scores the enriched data.
                            job_row.update(enriched)
                            logger.debug(
                                "Enriched job '%s': fields=%s",
                                dedup_key,
                                list(enriched.keys()),
                            )
                    except Exception as enrich_err:
                        logger.debug(
                            "Enrichment failed for '%s' (non-fatal): %s",
                            dedup_key,
                            enrich_err,
                        )

                # --- Pre-Haiku exclusion filter (C3) ---
                exclusions = config.get("profile", {}).get("exclusions", {})
                profile_min_salary = config.get("profile", {}).get("min_salary")
                excluded, reason = should_exclude(job_row, exclusions, profile_min_salary)
                if excluded:
                    logger.info(
                        "Pre-filter excluded '%s' @ '%s': %s",
                        job_row.get("title"),
                        job_row.get("company"),
                        reason,
                    )
                    continue

                # --- Haiku scoring (now uses enriched data) ---
                result = score_job_haiku(client, job_row, profile, conn, config)
                if result is None:
                    logger.debug(
                        "Haiku: no result for '%s' @ '%s' -- skipping",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

                score = result.get("score", 0)
                summary_text = result.get("summary", "")

                conn.execute(
                    "UPDATE jobs SET haiku_score = ?, haiku_summary = ? WHERE dedup_key = ?",
                    (score, summary_text, dedup_key),
                )
                conn.commit()
                haiku_scored += 1

                # --- Borderline re-evaluation band (C2) ---
                # Jobs scoring 42-54 get a second Haiku call with expanded context
                # (4000 chars vs 2000 default) before the Sonnet decision.
                borderline_low = threshold  # 42
                borderline_high = 54
                if borderline_low <= score <= borderline_high:
                    logger.info(
                        "Borderline re-eval for '%s' @ '%s' (initial=%d, band=%d-%d)",
                        job_row.get("title"), job_row.get("company"),
                        score, borderline_low, borderline_high,
                    )
                    reeval_result = score_job_haiku(
                        client, job_row, profile, conn, config,
                        max_chars=4000, purpose="haiku_reeval",
                    )
                    if reeval_result is not None:
                        score = reeval_result.get("score", 0)
                        summary_text = reeval_result.get("summary", "")
                        # Update DB with re-eval score (replaces initial borderline score)
                        conn.execute(
                            "UPDATE jobs SET haiku_score = ?, haiku_summary = ? WHERE dedup_key = ?",
                            (score, summary_text, dedup_key),
                        )
                        conn.commit()
                        logger.info(
                            "Borderline re-eval result for '%s': %d",
                            job_row.get("title"), score,
                        )

                if score >= threshold:
                    sonnet_queue.append(dedup_key)

                # Fire high-score notification for jobs at or above threshold
                if score >= threshold:
                    try:
                        from job_finder.web.notifier import notify_high_score
                        notify_high_score(
                            job_row.get("title", ""),
                            job_row.get("company", ""),
                            score,
                            dedup_key,
                            config,
                        )
                    except Exception:
                        logger.debug("notification dispatch failed for job %s", dedup_key, exc_info=True)

            except Exception as e:
                logger.warning(
                    "Haiku scoring error for job '%s': %s -- continuing", dedup_key, e
                )

    finally:
        conn.close()

    logger.info(
        "Haiku scored %d jobs, %d above threshold (>=%d) for Sonnet",
        haiku_scored,
        len(sonnet_queue),
        threshold,
    )
    return sonnet_queue, haiku_scored


def _run_sonnet_evaluation(
    sonnet_queue: list[str],
    config: dict,
    db_path: str,
) -> int:
    """Run Sonnet deep evaluation for a batch of jobs above the Haiku threshold.

    For each job: relies on jd_full already being populated by enrich_job (which
    ran before Haiku scoring). If jd_full is still missing, skips the job.
    Calls evaluate_job_sonnet and persists sonnet_score and fit_analysis to the DB.

    Args:
        sonnet_queue: List of dedup_keys for jobs to evaluate with Sonnet.
        config: Application config dict.
        db_path: Absolute path to the SQLite database file.

    Returns:
        Count of jobs successfully evaluated by Sonnet.
    """
    if not sonnet_queue:
        return 0

    if anthropic is None:
        logger.debug("anthropic not installed -- skipping Sonnet evaluation")
        return 0

    if evaluate_job_sonnet is None:
        logger.warning("Sonnet evaluation module not available -- skipping")
        return 0

    profile = _load_profile(config)
    client = anthropic.Anthropic()
    sonnet_evaluated = 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        for dedup_key in sonnet_queue:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    logger.warning("Sonnet: job '%s' not found in DB -- skipping", dedup_key)
                    continue

                job_row = dict(row)

                # Job should already have jd_full from enrich_job (ran before Haiku scoring).
                # If still missing after full enrichment pipeline, skip Sonnet eval.
                if not job_row.get("jd_full"):
                    logger.info(
                        "No JD available for '%s' @ '%s' after enrichment, skipping Sonnet eval",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

                # Run Sonnet evaluation
                result = evaluate_job_sonnet(client, job_row, experience_profile=profile, conn=conn, config=config)
                if result is None:
                    logger.info(
                        "Sonnet eval returned None for '%s' @ '%s' (budget or JD missing)",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

                sonnet_score = result.get("score")
                fit_analysis = json.dumps(result.get("fit_analysis", {}))

                conn.execute(
                    "UPDATE jobs SET sonnet_score = ?, fit_analysis = ? WHERE dedup_key = ?",
                    (sonnet_score, fit_analysis, dedup_key),
                )
                conn.commit()
                sonnet_evaluated += 1

                # Company enrichment for Sonnet-scored jobs — best-effort
                if enrich_company_info is not None:
                    try:
                        company = job_row.get("company", "")
                        if company:
                            company_data = enrich_company_info(company)
                            if company_data:
                                logger.debug(
                                    "Company enrichment for '%s': %s",
                                    company,
                                    company_data,
                                )
                    except Exception:
                        logger.debug("company enrichment failed for job %s", dedup_key, exc_info=True)

            except Exception as e:
                logger.warning(
                    "Sonnet evaluation error for job '%s': %s -- continuing", dedup_key, e
                )

    finally:
        conn.close()

    logger.info("Sonnet evaluated %d jobs", sonnet_evaluated)
    return sonnet_evaluated


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
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            from job_finder.web.claude_client import get_cost_stats
            stats = get_cost_stats(conn)
        finally:
            conn.close()

        monthly_spend = stats.get("month", 0.0)
        if monthly_spend <= 0:
            return

        pct = (monthly_spend / budget_cap) * 100

        from job_finder.web.notifier import notify_budget_alert

        if pct >= 100 and _last_budget_pct_notified < 100:
            notify_budget_alert(pct, config)
            _last_budget_pct_notified = 100
        elif pct >= 80 and _last_budget_pct_notified < 80:
            notify_budget_alert(pct, config)
            _last_budget_pct_notified = 80

    except Exception as e:
        logger.debug("Budget alert check failed (non-fatal): %s", e)
