"""Batch scoring blueprint — unified batch scoring routes (v3.0).

Single ``batch_score_start`` route plus ``_run_batch_bg`` worker.
``batch_score_sessions.session_type`` uses ``scoring`` (legacy rows may
still store ``haiku`` / ``sonnet``; the status UI treats them as scoring).
"""

import logging
import threading

from flask import Blueprint, current_app, render_template

from job_finder.db import JOBS_ALL_COLUMNS, update_pipeline_status
from job_finder.json_utils import utc_now_iso
from job_finder.web._htmx import htmx_fragment
from job_finder.web.db_helpers import (
    PollingSessionConfig,
    render_polling_status,
    standalone_connection,
)
from job_finder.web.exclusion_filter import (
    SCORABLE_CANDIDATE_ORDER_BY,
    SCORABLE_CANDIDATE_WHERE,
    count_scorable,
    should_exclude,
)
from job_finder.web.live_events import COSTS_CHANGED, JOBS_CHANGED, PIPELINE_CHANGED
from job_finder.web.live_events import publish as publish_live

logger = logging.getLogger(__name__)

batch_scoring_bp = Blueprint("batch_scoring", __name__, url_prefix="/dashboard")

_SESSION_TYPE_SCORING = "scoring"


def _start_batch_session():
    """Shared core for POST /batch-score/start."""
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    with standalone_connection(db_path) as conn:
        total = count_scorable(conn, config)

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                scored=0,
                skipped=0,
                status="done",
                message="No scorable jobs — all unscored jobs are dismissed or already classified.",
                error_msg=None,
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES (?, 'running', ?, 0, ?)",
            (_SESSION_TYPE_SCORING, total, now),
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if not testing:
        t = threading.Thread(
            target=_run_batch_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_batch_score_progress.html",
        session_id=session_id,
        total=total,
        scored=0,
        skipped=0,
        cancelling=False,
    )


@batch_scoring_bp.route("/batch-score/start", methods=["POST"], strict_slashes=False)
def batch_score_start():
    """Start async unified batch scoring — returns HTMX polling fragment.

    Counts jobs with classification IS NULL (not yet processed by the unified
    scorer) and kicks off a daemon thread that routes each row through
    score_and_persist_job.
    """
    return _start_batch_session()


def _batch_done_ctx(session, status: str, error_msg: str | None) -> dict:
    return {
        "scored": session["scored"],
        "skipped": session["skipped"],
        "status": status,
        "message": None,
        "error_msg": error_msg,
    }


def _batch_progress_ctx(session) -> dict:
    return {
        "session_id": session["id"],
        "total": session["total"],
        "scored": session["scored"],
        "skipped": session["skipped"],
        "cancelling": (session["status"] == "cancelling"),
    }


_BATCH_HX_TRIGGER = {"dashboard-refresh": None, "jobs-updated": None}


@batch_scoring_bp.route("/batch-score/status/<int:session_id>", strict_slashes=False)
@htmx_fragment("dashboard.index")
def batch_score_status(session_id):
    """Poll route for batch scoring progress (delegates to ``render_polling_status``).

    Returns _batch_score_progress.html (WITH hx-trigger) when still running.
    Returns _batch_score_done.html (WITH HX-Trigger-After-Settle) on terminal/timeout.
    """
    return render_polling_status(
        current_app.config["DB_PATH"],
        session_id,
        PollingSessionConfig(
            progress_template="dashboard/_batch_score_progress.html",
            done_template="dashboard/_batch_score_done.html",
            progress_ctx=_batch_progress_ctx,
            done_ctx=_batch_done_ctx,
            not_found_ctx={
                "scored": 0,
                "skipped": 0,
                "status": "error",
                "message": None,
                "error_msg": "Session not found",
            },
            hx_trigger_after_settle=_BATCH_HX_TRIGGER,
            session_label="Batch",
        ),
    )


@batch_scoring_bp.route(
    "/batch-score/cancel/<int:session_id>", methods=["POST"], strict_slashes=False
)
def batch_score_cancel(session_id):
    """Cancel a running batch score session.

    Sets status='cancelling' in DB. The background thread checks status
    before each job and will set status='cancelled' when it sees 'cancelling'.
    Returns a progress fragment that keeps polling until the thread finishes.
    """
    db_path = current_app.config["DB_PATH"]

    with standalone_connection(db_path) as conn:
        conn.execute(
            "UPDATE batch_score_sessions SET status = 'cancelling' WHERE id = ? AND status = 'running'",
            (session_id,),
        )
        conn.commit()
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(
            "dashboard/_batch_score_done.html",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg="Session not found.",
        )

    # Return progress fragment with cancelling=True — polling continues until
    # the background thread sets status='cancelled'
    return render_template(
        "dashboard/_batch_score_progress.html",
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        skipped=session["skipped"],
        cancelling=True,
    )


def _run_batch_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run the unified v3.0 scorer for all unscored jobs.

    v3.0 (Phase 34 Plan 3 Commit B): replaces the previous two-phase
    worker pair with a single worker that routes each job through
    score_and_persist_job. The query predicate selects rows where
    classification is not yet populated — the unified scorer writes
    classification on every row, so one predicate covers both the
    pre-v3 filter pass and the pre-v3 evaluation pass.

    Delegates per-job scoring + persistence to
    scoring_orchestrator.score_and_persist_job. This function handles
    thread-own DB connection, cancellation checks, exclusion filtering,
    session progress tracking, and activity logging.

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        config: Application config dict.
    """
    try:
        from job_finder.web.job_scorer import scoring_precheck
        from job_finder.web.scoring_orchestrator import score_and_persist_job
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return
    # Candidate context is now resolved (and memoized) inside
    # score_and_persist_job → _resolve_candidate_context(config). This
    # blueprint no longer builds it manually; the orchestrator is the
    # single source of truth so the other six call sites can't bypass it.

    try:
        with standalone_connection(db_path) as conn:
            # Coarse candidate universe — the SAME shared SQL pre-filter
            # count_scorable uses (SCORABLE_CANDIDATE_WHERE), so the dashboard
            # "N unscored" tile / batch ``total`` and the rows this worker pulls
            # can never disagree on the universe. Per-row scorability (exclusion
            # auto-dismiss + completeness gates) is then decided below by the
            # same should_exclude + scoring_precheck the count uses via
            # is_scorable. The WHERE filters classification IS NULL, dismissed/
            # archived, and quarantined (I-16/I-17) rows; ORDER scores the
            # freshest first (served by idx_jobs_last_seen). The legacy heuristic
            # ``score`` column was dropped in m113.
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs "
                f"WHERE {SCORABLE_CANDIDATE_WHERE} {SCORABLE_CANDIDATE_ORDER_BY}"
            ).fetchall()

            # BATCH-04: Pre-loop cancellation check (was per-job inside loop)
            status_row = conn.execute(
                "SELECT status FROM batch_score_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if status_row and status_row["status"] == "cancelling":
                conn.execute(
                    "UPDATE batch_score_sessions SET status = 'cancelled', finished_at = ? WHERE id = ?",
                    (utc_now_iso(), session_id),
                )
                conn.commit()
                return

            scored_count = 0
            skipped_count = 0

            for row in rows:
                job_row = dict(row)

                # Pre-scoring exclusion filter — auto-dismiss excluded jobs silently.
                # Excluded jobs are NOT counted against scored/skipped: total was
                # computed by count_scorable() which already excludes them.
                exclusions = config.get("profile", {}).get("exclusions", {})
                profile_min_salary = config.get("profile", {}).get("min_salary")
                excluded, reason = should_exclude(
                    job_row, exclusions, profile_min_salary, config=config
                )
                if excluded:
                    logger.info(
                        "Batch scoring: excluded '%s': %s",
                        job_row.get("dedup_key"),
                        reason,
                    )
                    dedup_key = job_row.get("dedup_key")
                    if dedup_key and job_row.get("pipeline_status") == "discovered":
                        update_pipeline_status(
                            conn,
                            dedup_key,
                            "dismissed",
                            source="exclusion_filter",
                            evidence=reason,
                        )
                    continue

                # Completeness gate (jd_full + P3.2 location) — a row score_job
                # would no-op (awaiting_jd / awaiting_location) is NOT scorable
                # and was never counted in `total` (count_scorable applies the
                # identical scoring_precheck gates). Skip WITHOUT counting it
                # toward scored/skipped so `processed` (scored + skipped) can
                # never exceed `total` — the "205/174 processed" overrun. The
                # row stays classification IS NULL and self-heals into the next
                # run once enrichment fills jd_full / location. The coarse SELECT
                # above still surfaces these rows so the exclusion auto-dismiss
                # above keeps firing on them; only the counting changes.
                if scoring_precheck(job_row) is not None:
                    continue

                try:
                    result = score_and_persist_job(job_row, conn, config)
                    # score_and_persist_job returns a ScoringResult envelope for
                    # ok/skipped/error and only writes classification on "ok".
                    # Counting non-None as "scored" inflated scored_count with
                    # rows that were silently no-op'd (e.g. missing jd_full), so
                    # the dashboard showed "N scored" while count_scorable still
                    # found those N rows on the next refresh.
                    if result is not None and getattr(result, "status", None) == "ok":
                        scored_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logger.warning(
                        "Batch scoring: error scoring job '%s': %s -- continuing",
                        job_row.get("dedup_key"),
                        e,
                    )
                    skipped_count += 1

                # Flush progress counters periodically so the polling endpoint sees updates
                processed = scored_count + skipped_count
                if processed % 5 == 0:
                    conn.execute(
                        "UPDATE batch_score_sessions "
                        "SET scored = ?, skipped = ?, last_tick_at = ? WHERE id = ?",
                        (scored_count, skipped_count, utc_now_iso(), session_id),
                    )
                    conn.commit()

            # Final flush before finishing
            conn.execute(
                "UPDATE batch_score_sessions "
                "SET scored = ?, skipped = ?, last_tick_at = ? WHERE id = ?",
                (scored_count, skipped_count, utc_now_iso(), session_id),
            )
            conn.commit()

            # Budget-skip eventing (WP3): when a batch run scored NOTHING and
            # the cost gate is closed, the budget is the plausible terminal
            # cause — record exactly ONE activity row for the whole run (never
            # per job, never per cascade-provider skip).
            if scored_count == 0 and skipped_count > 0:
                try:
                    from job_finder.web.activity_tracker import (
                        ACTION_SCORING_SKIPPED_BUDGET,
                        log_activity,
                    )
                    from job_finder.web.claude_client import cost_gate

                    if not cost_gate(conn, config, "scoring"):
                        log_activity(
                            db_path,
                            ACTION_SCORING_SKIPPED_BUDGET,
                            metadata={"path": "batch", "skipped_count": skipped_count},
                        )
                except Exception:
                    logger.debug("budget-skip activity logging failed", exc_info=True)

            # All jobs processed — mark done
            _finish_session(conn, db_path, session_id, "done", _SESSION_TYPE_SCORING)

    except Exception as e:
        logger.error("Batch scoring background thread failed: %s", e)
        _mark_session_error(db_path, session_id, str(e)[:500])


def _finish_session(conn, db_path: str, session_id: int, status: str, session_type: str) -> None:
    """Mark a batch session as done and log the activity."""
    conn.execute(
        "UPDATE batch_score_sessions SET status = ?, finished_at = ? WHERE id = ?",
        (status, utc_now_iso(), session_id),
    )
    conn.commit()

    try:
        from job_finder.web.activity_tracker import ACTION_BATCH_SCORE, log_activity

        action = ACTION_BATCH_SCORE
        session_row = conn.execute(
            "SELECT scored, skipped, total FROM batch_score_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        log_activity(
            db_path,
            action,
            metadata={
                "session_type": session_type,
                "scored": session_row["scored"] if session_row else 0,
                "skipped": session_row["skipped"] if session_row else 0,
                "total": session_row["total"] if session_row else 0,
                "status": "success",
            },
        )
    except Exception:
        pass

    # Scoring rewrote classification/score and may have auto-dismissed excluded
    # rows — push live-update events so every subscribed widget (this tab's job
    # table already gets jobs-updated; other tabs/pages get these) refreshes.
    for _ev in (JOBS_CHANGED, COSTS_CHANGED, PIPELINE_CHANGED):
        publish_live(_ev)


def _mark_session_error(db_path: str, session_id: int, error_msg: str) -> None:
    """Mark a batch session as errored. Used for background thread import failures."""
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? WHERE id = ?",
                (error_msg, utc_now_iso(), session_id),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "Failed to mark session %s as error: %s", session_id, error_msg, exc_info=True
        )
