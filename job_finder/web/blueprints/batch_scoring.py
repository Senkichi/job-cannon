"""Batch scoring blueprint -- Haiku/Sonnet batch scoring start, status, cancel routes."""

import logging
import threading
from datetime import datetime, timezone

import json

from flask import Blueprint, current_app, make_response, render_template

from job_finder.db import JOBS_ALL_COLUMNS, update_pipeline_status
from job_finder.config import DEFAULT_HAIKU_THRESHOLD
from job_finder.json_utils import utc_now_iso
from job_finder.web.exclusion_filter import count_haiku_scorable, should_exclude
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

batch_scoring_bp = Blueprint("batch_scoring", __name__, url_prefix="/dashboard")

@batch_scoring_bp.route("/batch-score/haiku/start", methods=["POST"], strict_slashes=False)
def batch_score_haiku_start():
    """Start async Haiku batch scoring — returns HTMX polling fragment.

    Counts unscored jobs and either returns a done fragment immediately
    (nothing to score) or inserts a batch_score_sessions row and starts a
    daemon thread, returning a progress fragment that polls every 2s.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    with standalone_connection(db_path) as conn:
        total = count_haiku_scorable(conn, config)

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Haiku",
                scored=0,
                skipped=0,
                status="done",
                message="No scorable jobs — all unscored jobs are dismissed or already scored.",
                error_msg=None,
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'running', ?, 0, ?)",
            (total, now),
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if not testing:
        t = threading.Thread(
            target=_run_batch_haiku_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_batch_score_progress.html",
        label="Haiku",
        session_id=session_id,
        total=total,
        scored=0,
        skipped=0,
        cancelling=False,
    )

@batch_scoring_bp.route("/batch-score/sonnet/start", methods=["POST"], strict_slashes=False)
def batch_score_sonnet_start():
    """Start async Sonnet batch evaluation — returns HTMX polling fragment.

    Counts jobs qualifying for Sonnet (haiku_score >= threshold, no sonnet_score,
    jd_full present). Returns done fragment if none qualify.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)

    with standalone_connection(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
            "AND sonnet_score IS NULL AND jd_full IS NOT NULL",
            (threshold,),
        ).fetchone()[0]

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Sonnet",
                scored=0,
                skipped=0,
                status="done",
                message="No qualifying jobs for Sonnet evaluation.",
                error_msg=None,
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('sonnet', 'running', ?, 0, ?)",
            (total, now),
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if not testing:
        t = threading.Thread(
            target=_run_batch_sonnet_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_batch_score_progress.html",
        label="Sonnet",
        session_id=session_id,
        total=total,
        scored=0,
        skipped=0,
        cancelling=False,
    )

@batch_scoring_bp.route("/batch-score/status/<int:session_id>", strict_slashes=False)
def batch_score_status(session_id):
    """Poll route for batch scoring progress.

    Returns _batch_score_progress.html (WITH hx-trigger) when still running.
    Returns _batch_score_done.html (WITHOUT hx-trigger) when done/error/cancelled.
    Uses own sqlite3 connection — safe for HTMX polling outside request context.
    """
    db_path = current_app.config["DB_PATH"]

    with standalone_connection(db_path) as conn:
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(
            "dashboard/_batch_score_done.html",
            label="Unknown",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg="Session not found.",
        )

    label = "Haiku" if session["session_type"] == "haiku" else "Sonnet"
    status = session["status"]

    # Timeout safety net: if session has been running for >30 minutes, auto-mark as error
    if status in ("running", "cancelling") and session["started_at"]:
        try:
            started = datetime.fromisoformat(session["started_at"])
            elapsed_minutes = (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds() / 60
            if elapsed_minutes > 30:
                logger.warning("Batch session %s timed out after %.1f minutes", session_id, elapsed_minutes)
                with standalone_connection(db_path) as timeout_conn:
                    timeout_conn.execute(
                        "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? "
                        "WHERE id = ? AND status IN ('running', 'cancelling')",
                        ("Session timed out (>30 min)", utc_now_iso(), session_id),
                    )
                    timeout_conn.commit()
                resp = make_response(render_template(
                    "dashboard/_batch_score_done.html",
                    label=label,
                    scored=session["scored"],
                    skipped=session["skipped"],
                    status="error",
                    message=None,
                    error_msg="Session timed out (>30 min)",
                ))
                resp.headers["HX-Trigger-After-Settle"] = json.dumps(
                    {"dashboard-refresh": None, "jobs-updated": None}
                )
                return resp
        except (ValueError, TypeError):
            pass

    # Terminal states: done, error, cancelled — return done fragment (NO hx-trigger)
    if status in ("done", "error", "cancelled"):
        resp = make_response(render_template(
            "dashboard/_batch_score_done.html",
            label=label,
            scored=session["scored"],
            skipped=session["skipped"],
            status=status,
            message=None,
            error_msg=session["error_msg"] if status == "error" else None,
        ))
        # Trigger dashboard stats refresh + jobs table refresh (if on jobs page)
        resp.headers["HX-Trigger-After-Settle"] = json.dumps(
            {"dashboard-refresh": None, "jobs-updated": None}
        )
        return resp

    # Still running (running or cancelling) — return progress fragment (WITH hx-trigger)
    return render_template(
        "dashboard/_batch_score_progress.html",
        label=label,
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        skipped=session["skipped"],
        cancelling=(status == "cancelling"),
    )

@batch_scoring_bp.route("/batch-score/cancel/<int:session_id>", methods=["POST"], strict_slashes=False)
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
            label="Unknown",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg="Session not found.",
        )

    label = "Haiku" if session["session_type"] == "haiku" else "Sonnet"

    # Return progress fragment with cancelling=True — polling continues until
    # the background thread sets status='cancelled'
    return render_template(
        "dashboard/_batch_score_progress.html",
        label=label,
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        skipped=session["skipped"],
        cancelling=True,
    )

def _run_batch_haiku_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run Haiku scoring for all unscored jobs.

    Delegates per-job scoring + persistence to scoring_orchestrator.score_and_persist_haiku.
    This function handles thread-own DB connection, cancellation checks, exclusion filtering,
    session progress tracking, and activity logging.

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        config: Application config dict.
    """
    try:

        from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_haiku

        profile = load_scoring_profile(config)
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return

    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NULL "
                "AND pipeline_status NOT IN ('dismissed', 'archived') ORDER BY score DESC"
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

                # Pre-Haiku exclusion filter — auto-dismiss excluded jobs silently.
                # Excluded jobs are NOT counted against scored/skipped: total was
                # computed by count_haiku_scorable() which already excludes them.
                exclusions = config.get("profile", {}).get("exclusions", {})
                profile_min_salary = config.get("profile", {}).get("min_salary")
                excluded, reason = should_exclude(job_row, exclusions, profile_min_salary, config=config)
                if excluded:
                    logger.info("Batch Haiku: excluded '%s': %s", job_row.get("dedup_key"), reason)
                    dedup_key = job_row.get("dedup_key")
                    if dedup_key and job_row.get("pipeline_status") == "discovered":
                        update_pipeline_status(
                            conn, dedup_key, "dismissed",
                            source="exclusion_filter", evidence=reason,
                        )
                    continue

                try:
                    result = score_and_persist_haiku(conn, job_row, config, profile)
                    if result is not None:
                        scored_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logger.warning(
                        "Batch Haiku: error scoring job '%s': %s -- continuing",
                        job_row.get("dedup_key"), e,
                    )
                    skipped_count += 1

                # Flush progress counters periodically so the polling endpoint sees updates
                processed = scored_count + skipped_count
                if processed % 5 == 0:
                    conn.execute(
                        "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                        (scored_count, skipped_count, session_id),
                    )
                    conn.commit()

            # Final flush before finishing
            conn.execute(
                "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                (scored_count, skipped_count, session_id),
            )
            conn.commit()

            # All jobs processed — mark done
            _finish_session(conn, db_path, session_id, "done", "haiku")

    except Exception as e:
        logger.error("Batch Haiku background thread failed: %s", e)
        _mark_session_error(db_path, session_id, str(e)[:500])

def _run_batch_sonnet_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run Sonnet evaluation for qualifying jobs.

    Delegates per-job scoring + persistence to scoring_orchestrator.score_and_persist_sonnet.
    This function handles thread-own DB connection, cancellation checks, session progress
    tracking, and activity logging.

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        config: Application config dict.
    """
    try:

        from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_sonnet
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return

    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    profile = load_scoring_profile(config)

    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
                "AND sonnet_score IS NULL AND jd_full IS NOT NULL ORDER BY haiku_score DESC",
                (threshold,),
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
                try:
                    result = score_and_persist_sonnet(conn, job_row, config, profile)
                    if result is not None:
                        scored_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logger.warning(
                        "Batch Sonnet: error evaluating job '%s': %s -- continuing",
                        job_row.get("dedup_key"), e,
                    )
                    skipped_count += 1

                # Flush progress counters periodically so the polling endpoint sees updates
                processed = scored_count + skipped_count
                if processed % 5 == 0:
                    conn.execute(
                        "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                        (scored_count, skipped_count, session_id),
                    )
                    conn.commit()

            # Final flush before finishing
            conn.execute(
                "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                (scored_count, skipped_count, session_id),
            )
            conn.commit()

            # All jobs processed — mark done
            _finish_session(conn, db_path, session_id, "done", "sonnet")

    except Exception as e:
        logger.error("Batch Sonnet background thread failed: %s", e)
        _mark_session_error(db_path, session_id, str(e)[:500])

def _finish_session(conn, db_path: str, session_id: int, status: str, session_type: str) -> None:
    """Mark a batch session as done and log the activity."""
    conn.execute(
        "UPDATE batch_score_sessions SET status = ?, finished_at = ? WHERE id = ?",
        (status, utc_now_iso(), session_id),
    )
    conn.commit()

    try:
        from job_finder.web.activity_tracker import (
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            log_activity,
        )
        action = ACTION_BATCH_SCORE_HAIKU if session_type == "haiku" else ACTION_BATCH_SCORE_SONNET
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
        logger.warning("Failed to mark session %s as error: %s", session_id, error_msg, exc_info=True)
