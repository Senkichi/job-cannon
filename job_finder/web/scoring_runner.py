"""Scoring runner -- unified v3.0 scoring orchestration (Phase 34 Plan 4).

Single entry point: ``run_scoring`` calls ``score_and_persist_job`` per
dedup_key, preserving the pre-score liveness gate per CONTEXT D-11.
The legacy two-tier (Haiku + Sonnet) entry points were removed in
Plan 4 Commit E.
"""

import logging
import sqlite3

from job_finder.db import JOBS_ALL_COLUMNS, persist_job_expiry_state, update_pipeline_status
from job_finder.json_utils import utc_now_iso
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.expiry_checker import EXPIRED as _EXPIRED
from job_finder.web.expiry_checker import check_job_liveness
from job_finder.web.legitimacy_scanner import scan_legitimacy
from job_finder.web.scoring_orchestrator import (
    score_and_persist_job,
)

try:
    from job_finder.web.data_enricher import enrich_job
except ImportError:
    enrich_job = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def run_scoring(
    new_job_keys: list[str],
    config: dict,
    db_path: str,
) -> dict:
    """Unified v3.0 scoring runner -- replaced run_haiku_scoring +
    run_sonnet_evaluation (Plan 4 Commit E removed the legacy two-tier path).

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

                # Exclusion-filter auto-dismiss (preserved from legacy
                # run_haiku_scoring). Jobs matching the user's title-keyword /
                # company exclusion list never reach the scorer; they get
                # pipeline_status='dismissed' (only when currently 'discovered'
                # so manual reviewing/applied state is not overwritten).
                profile = config.get("profile") or {}
                excluded, reason = should_exclude(
                    job,
                    profile.get("exclusions") or {},
                    min_salary=profile.get("min_salary"),
                    config=config,
                )
                if excluded:
                    if (job.get("pipeline_status") or "discovered") == "discovered":
                        update_pipeline_status(
                            conn,
                            dedup_key,
                            "dismissed",
                            source="run_scoring_exclusion",
                            evidence=reason or "matched exclusion filter",
                        )
                    summary["skipped_no_jd"] += 1
                    continue

                # Liveness gate (D-11): pre-score. Expired rows get the
                # standard archive update and are counted as skipped_dead.
                liveness = check_job_liveness(job)
                now_iso = utc_now_iso()
                persist_job_expiry_state(conn, dedup_key, liveness, now_iso)
                if liveness == _EXPIRED:
                    logger.info(
                        "run_scoring liveness gate: archiving expired '%s' @ '%s'",
                        job.get("title"),
                        job.get("company"),
                    )
                    update_pipeline_status(
                        conn,
                        dedup_key,
                        "archived",
                        source="run_scoring_liveness",
                        evidence="quick_liveness_check expired",
                    )
                    summary["skipped_dead"] += 1
                    continue

                # Legitimacy scan — Phase 49.07.  Run BEFORE
                # score_and_persist_job so that persist_job_assessment
                # reads the updated legitimacy_note from the DB row when
                # it calls derive_classification.  The UPDATE uses
                # "AND legitimacy_note IS NULL" to preserve any manually-
                # set note (e.g. admin override via /admin/review).
                jd_text = job.get("jd_full") or ""
                if jd_text:
                    leg_note = scan_legitimacy(jd_text)
                    if leg_note:
                        conn.execute(
                            "UPDATE jobs SET legitimacy_note = ?"
                            " WHERE dedup_key = ? AND legitimacy_note IS NULL",
                            (leg_note, dedup_key),
                        )
                        conn.commit()
                        logger.info(
                            "run_scoring: legitimacy_scanner flagged '%s' @ '%s' — %s",
                            job.get("title"),
                            job.get("company"),
                            leg_note,
                        )

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
