"""Scoring runner -- Haiku batch scoring, Sonnet deep evaluation, and
unified v3.0 scoring orchestration.

Exposes three entry points:

- ``run_haiku_scoring`` / ``run_sonnet_evaluation`` — legacy two-tier path.
- ``run_scoring`` — Phase 34 Plan 2 unified v3.0 runner. Calls
  ``score_and_persist_job`` per dedup_key, preserving the pre-score
  liveness gate per CONTEXT D-11. Gated at the caller (pipeline_runner)
  behind the ``use_unified_scorer`` config flag until Plan 4 removes the
  legacy path.
"""

import logging
import shutil
import sqlite3
from datetime import datetime, timezone

from job_finder.config import DEFAULT_HAIKU_THRESHOLD
from job_finder.db import JOBS_ALL_COLUMNS, persist_job_expiry_state, update_pipeline_status
from job_finder.web.expiry_checker import check_job_liveness, EXPIRED as _EXPIRED
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.haiku_scorer import score_job_haiku
from job_finder.web.scoring_orchestrator import (
    load_scoring_profile,
    score_and_persist_haiku,
    score_and_persist_job,
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

    if not shutil.which("claude"):
        logger.debug("claude CLI not found -- skipping Haiku scoring")
        return [], 0

    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    profile = load_scoring_profile(config)
    sonnet_queue: list[str] = []
    haiku_scored = 0

    with standalone_connection(db_path) as conn:
        # Batch prefetch all job rows (BATCH-01) — O(1) query instead of O(N)
        placeholders = ",".join("?" * len(new_job_keys))
        rows = conn.execute(
            f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key IN ({placeholders})",
            new_job_keys,
        ).fetchall()
        job_rows_by_key = {r["dedup_key"]: dict(r) for r in rows}

        for dedup_key in new_job_keys:
            try:
                job_row = job_rows_by_key.get(dedup_key)
                if job_row is None:
                    logger.warning("Haiku: job '%s' not found in DB -- skipping", dedup_key)
                    continue

                # --- Enrichment FIRST (before scoring) ---
                if enrich_job is not None and (
                    not job_row.get("jd_full") or job_row.get("salary_min") is None
                ):
                    try:
                        serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
                        enriched = enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
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
                    # Auto-dismiss: only transition 'discovered' jobs to 'dismissed'
                    if job_row.get("pipeline_status") == "discovered":
                        update_pipeline_status(
                            conn, dedup_key, "dismissed",
                            source="exclusion_filter", evidence=reason,
                        )
                    continue

                # --- Haiku scoring + borderline re-eval + DB persistence ---
                # Delegated to scoring_orchestrator (single point of truth).
                # Pass score_job_haiku as scorer_fn so test patches on
                # scoring_runner.score_job_haiku are captured.
                result = score_and_persist_haiku(
                    conn, job_row, config, profile,
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

    if not shutil.which("claude"):
        logger.debug("claude CLI not found -- skipping Sonnet evaluation")
        return 0

    if evaluate_job_sonnet is None:
        logger.warning("Sonnet evaluation module not available -- skipping")
        return 0

    profile = load_scoring_profile(config)
    sonnet_evaluated = 0

    with standalone_connection(db_path) as conn:
        # Batch prefetch all job rows (BATCH-02) — O(1) query instead of O(N)
        placeholders = ",".join("?" * len(sonnet_queue))
        rows = conn.execute(
            f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key IN ({placeholders})",
            sonnet_queue,
        ).fetchall()
        job_rows_by_key = {r["dedup_key"]: dict(r) for r in rows}

        for dedup_key in sonnet_queue:
            try:
                job_row = job_rows_by_key.get(dedup_key)
                if job_row is None:
                    logger.warning("Sonnet: job '%s' not found in DB -- skipping", dedup_key)
                    continue

                # Job should already have jd_full from enrich_job (ran before Haiku scoring).
                # If still missing after full enrichment pipeline, skip Sonnet eval.
                if not job_row.get("jd_full"):
                    logger.info(
                        "No JD available for '%s' @ '%s' after enrichment, skipping Sonnet eval",
                        job_row.get("title"),
                        job_row.get("company"),
                    )
                    continue

                # --- Liveness gate (before the expensive Sonnet call) ---
                # Per .planning/career-ops-adoption-plan.md: gate Sonnet eval, not
                # Haiku. Ingestion-time URL probes are fragile (network flakes, deep
                # links that 404 despite the job being live in its ATS), so we only
                # pay their cost before the expensive call — Haiku runs regardless.
                liveness = check_job_liveness(job_row)
                now_iso = datetime.now(timezone.utc).isoformat()
                persist_job_expiry_state(conn, dedup_key, liveness, now_iso)

                if liveness == _EXPIRED:
                    logger.info(
                        "Liveness gate: archiving expired '%s' @ '%s'",
                        job_row.get("title"), job_row.get("company"),
                    )
                    update_pipeline_status(
                        conn, dedup_key, "archived",
                        source="sonnet_liveness", evidence="quick_liveness_check expired",
                    )
                    continue

                # --- Sonnet scoring + DB persistence ---
                # Delegated to scoring_orchestrator (single point of truth).
                # Pass evaluate_job_sonnet as evaluator_fn so test patches on
                # scoring_runner.evaluate_job_sonnet are captured.
                result = score_and_persist_sonnet(
                    conn, job_row, config, profile,
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


# ---------------------------------------------------------------------------
# Unified v3.0 runner (Phase 34 Plan 2)
# ---------------------------------------------------------------------------


def run_scoring(
    new_job_keys: list[str],
    config: dict,
    db_path: str,
) -> dict:
    """Unified v3.0 scoring runner -- replaces run_haiku_scoring +
    run_sonnet_evaluation once Plan 4 lands.

    For each dedup_key in ``new_job_keys``:

    1. Fetch the jobs row (skip silently if missing).
    2. Pre-score liveness gate (CONTEXT D-11) — matches the position used by
       the legacy ``run_sonnet_evaluation``. Dead jobs are counted as skipped
       and never hit the scorer.
    3. Delegate scoring + persistence to ``score_and_persist_job``, which
       performs the atomic dual-write of new columns AND legacy shim
       (CONTEXT D-16).

    Returns a summary dict with counters for scored / skipped / error cases
    and per-classification breakdown. Counter keys match the new pipeline
    summary shape introduced in Plan 2 commit A (Plan 3 Commit E collapses
    the legacy haiku_scored / sonnet_queued / sonnet_evaluated keys).
    """
    summary = {
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "skipped_dead": 0,
        "skipped_no_jd": 0,
        "errors": 0,
    }

    if not new_job_keys:
        return summary

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
                    (dedup_key,),
                ).fetchone()
                if row is None:
                    logger.warning(
                        "run_scoring: job '%s' not found in DB -- skipping",
                        dedup_key,
                    )
                    continue
                job = dict(row)

                # Liveness gate (D-11): pre-score, same position as legacy
                # Sonnet path. Expired rows get the standard archive update
                # and are counted as skipped_dead.
                liveness = check_job_liveness(job)
                now_iso = datetime.now(timezone.utc).isoformat()
                persist_job_expiry_state(conn, dedup_key, liveness, now_iso)
                if liveness == _EXPIRED:
                    logger.info(
                        "run_scoring liveness gate: archiving expired '%s' @ '%s'",
                        job.get("title"),
                        job.get("company"),
                    )
                    update_pipeline_status(
                        conn, dedup_key, "archived",
                        source="run_scoring_liveness",
                        evidence="quick_liveness_check expired",
                    )
                    summary["skipped_dead"] += 1
                    continue

                result = score_and_persist_job(job, conn, config)

                if result is None:
                    summary["skipped_no_jd"] += 1
                    continue

                status = getattr(result, "status", None)
                if status == "skipped":
                    summary["skipped_no_jd"] += 1
                    continue
                if status == "error":
                    summary["errors"] += 1
                    continue

                summary["scored"] += 1

                # Re-read classification for the per-class counter. Single
                # small SELECT keeps the counter faithful to what actually
                # landed on disk (including Python-derived classification
                # overrides via legitimacy_note).
                cls_row = conn.execute(
                    "SELECT classification FROM jobs WHERE dedup_key = ?",
                    (dedup_key,),
                ).fetchone()
                if cls_row and cls_row[0]:
                    key = f"classified_{cls_row[0]}"
                    if key in summary:
                        summary[key] += 1

            except Exception as e:
                logger.warning(
                    "run_scoring error for job '%s': %s -- continuing",
                    dedup_key,
                    e,
                )
                summary["errors"] += 1
    finally:
        conn.close()

    logger.info(
        "run_scoring: %d scored, %d dead, %d no-jd, %d errors",
        summary["scored"],
        summary["skipped_dead"],
        summary["skipped_no_jd"],
        summary["errors"],
    )
    return summary
