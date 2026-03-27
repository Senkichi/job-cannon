"""Scoring runner -- Haiku batch scoring and Sonnet deep evaluation orchestration."""

import logging

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

from job_finder.config import DEFAULT_HAIKU_THRESHOLD
from job_finder.db import JOBS_ALL_COLUMNS
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.haiku_scorer import score_job_haiku
from job_finder.web.scoring_orchestrator import (
    load_scoring_profile,
    score_and_persist_haiku,
    score_and_persist_sonnet,
)

try:
    from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
except ImportError:
    evaluate_job_sonnet = None  # type: ignore[assignment]

try:
    from job_finder.web.data_enricher import enrich_job
    from job_finder.web.company_enricher import enrich_company_info
except ImportError:
    enrich_job = None  # type: ignore[assignment]
    enrich_company_info = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def run_haiku_scoring(
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
    profile = load_scoring_profile(config)
    client = anthropic.Anthropic()
    sonnet_queue: list[str] = []
    haiku_scored = 0

    with standalone_connection(db_path) as conn:
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?", (dedup_key,)
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
                excluded, reason = should_exclude(job_row, exclusions, profile_min_salary, config=config)
                if excluded:
                    logger.info(
                        "Pre-filter excluded '%s' @ '%s': %s",
                        job_row.get("title"),
                        job_row.get("company"),
                        reason,
                    )
                    continue

                # --- Haiku scoring + borderline re-eval + DB persistence ---
                # Delegated to scoring_orchestrator (single point of truth).
                # Pass score_job_haiku as scorer_fn so test patches on
                # scoring_runner.score_job_haiku are captured.
                result = score_and_persist_haiku(
                    conn, job_row, config, client, profile,
                    scorer_fn=score_job_haiku,
                )
                if result is None:
                    logger.debug(
                        "Haiku: no result for '%s' @ '%s' -- skipping",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

                haiku_scored += 1
                score = result.get("score", 0)

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

    logger.info(
        "Haiku scored %d jobs, %d above threshold (>=%d) for Sonnet",
        haiku_scored,
        len(sonnet_queue),
        threshold,
    )
    return sonnet_queue, haiku_scored


def run_sonnet_evaluation(
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

    profile = load_scoring_profile(config)
    client = anthropic.Anthropic()
    sonnet_evaluated = 0

    with standalone_connection(db_path) as conn:
        for dedup_key in sonnet_queue:
            try:
                row = conn.execute(
                    f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?", (dedup_key,)
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

                # --- Sonnet scoring + DB persistence ---
                # Delegated to scoring_orchestrator (single point of truth).
                # Pass evaluate_job_sonnet as evaluator_fn so test patches on
                # scoring_runner.evaluate_job_sonnet are captured.
                result = score_and_persist_sonnet(
                    conn, job_row, config, client, profile,
                    evaluator_fn=evaluate_job_sonnet,
                )
                if result is None:
                    logger.info(
                        "Sonnet eval returned no result for '%s' @ '%s'",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

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

    logger.info("Sonnet evaluated %d jobs", sonnet_evaluated)
    return sonnet_evaluated
