"""Batch scoring blueprint — unified batch scoring routes (v3.0 Phase 34 Plan 3 Commit B).

v3.0 merges the previous Haiku/Sonnet two-route split into a single
`batch_score_start` route + `_run_batch_bg` worker. The session_type enum
collapses from {haiku, sonnet, sync} to {scoring, sync}. Old
/batch-score/haiku/start and /batch-score/sonnet/start URLs are retained
as thin wrappers that delegate to the unified route so existing HTMX
templates keep working until Commit D migrates them; Plan 4 removes the
wrappers entirely.
"""

import logging
import threading
from datetime import datetime, timezone

import json

from flask import Blueprint, current_app, make_response, render_template

from job_finder.db import JOBS_ALL_COLUMNS, update_pipeline_status
from job_finder.json_utils import utc_now_iso
from job_finder.web.exclusion_filter import count_scorable, should_exclude
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

batch_scoring_bp = Blueprint("batch_scoring", __name__, url_prefix="/dashboard")

# v3.0 session_type for the unified scoring route. Plan 4 drops the old
# "haiku"/"sonnet" values entirely. Until then, the status route renders
# them with a generic "Scoring" label.
_SESSION_TYPE_SCORING = "scoring"


def _render_scoring_done(scored: int = 0, skipped: int = 0, status: str = "done",
                         message: str | None = None, error_msg: str | None = None):
    """Render the batch-score done fragment with the v3 'Scoring' label."""
    return render_template(
        "dashboard/_batch_score_done.html",
        label="Scoring",
        scored=scored,
        skipped=skipped,
        status=status,
        message=message,
        error_msg=error_msg,
    )


def _start_batch_session(label: str = "Scoring"):
    """Shared core for /batch-score/start and the back-compat haiku/sonnet wrappers.

    The `label` arg controls the user-visible text in the returned fragment AND
    the id= prefix of the surrounding div (`batch-score-<label.lower()>-status`).
    The unified route passes "Scoring"; the legacy wrappers pass "Haiku"/"Sonnet"
    so existing HTMX hx-target selectors in the pre-Commit-D dashboard templates
    still line up. Plan 34-03 Commit D replaces the templates with a single
    Scoring region; Plan 4 removes the wrappers entirely.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    with standalone_connection(db_path) as conn:
        total = count_scorable(conn, config)

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label=label,
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
        label=label,
        session_id=session_id,
        total=total,
        scored=0,
        skipped=0,
        cancelling=False,
    )


@batch_scoring_bp.route("/batch-score/start", methods=["POST"], strict_slashes=False)
def batch_score_start():
    """Start async unified batch scoring — returns HTMX polling fragment.

    v3.0 (Phase 34 Plan 3 Commit B): replaces batch_score_haiku_start +
    batch_score_sonnet_start. Counts jobs with classification IS NULL
    (i.e. not yet processed by the unified scorer) and kicks off a
    daemon thread that routes each row through score_and_persist_job.
    """
    return _start_batch_session(label="Scoring")


# Back-compat wrappers — template buttons still POST to /batch-score/haiku/start
# and /batch-score/sonnet/start. Commit D migrates the templates; Plan 4 removes
# these wrappers entirely. Delegating with the original label argument preserves
# HTMX id= selectors in the pre-Commit-D templates. PLAN-4-REMOVE
@batch_scoring_bp.route("/batch-score/haiku/start", methods=["POST"], strict_slashes=False)
def batch_score_haiku_start():
    """DEPRECATED — delegates to the unified scorer. Plan 4 removes."""
    return _start_batch_session(label="Haiku")


@batch_scoring_bp.route("/batch-score/sonnet/start", methods=["POST"], strict_slashes=False)
def batch_score_sonnet_start():
    """DEPRECATED — delegates to the unified scorer. Plan 4 removes."""
    return _start_batch_session(label="Sonnet")


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

    label = _label_for_session(session["session_type"])
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

    label = _label_for_session(session["session_type"])

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


def _label_for_session(session_type: str) -> str:
    """Map a session_type enum value to the user-visible label.

    v3.0 normalizes "haiku"/"sonnet"/"scoring" all to "Scoring" so the
    running UI reflects the unified pipeline regardless of which route
    created the session (plans 3 and 4 progressively remove the legacy
    session_type values).
    """
    if session_type == "sync":
        return "Sync"
    return "Scoring"


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
        from job_finder.web.scoring_orchestrator import (
            load_scoring_profile,
            score_and_persist_job,
        )

        profile = load_scoring_profile(config)
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return

    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE classification IS NULL "
                "AND pipeline_status NOT IN ('dismissed', 'archived') "
                "ORDER BY score DESC"
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
                        job_row.get("dedup_key"), reason,
                    )
                    dedup_key = job_row.get("dedup_key")
                    if dedup_key and job_row.get("pipeline_status") == "discovered":
                        update_pipeline_status(
                            conn, dedup_key, "dismissed",
                            source="exclusion_filter", evidence=reason,
                        )
                    continue

                try:
                    result = score_and_persist_job(job_row, conn, config)
                    if result is not None:
                        scored_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logger.warning(
                        "Batch scoring: error scoring job '%s': %s -- continuing",
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
        from job_finder.web.activity_tracker import (
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            log_activity,
        )
        # v3.0 keeps the existing activity constants for continuity with
        # dashboard Recent Activity records; Plan 4 consolidates them into
        # a single ACTION_BATCH_SCORE. For now the unified session_type
        # maps to BATCH_SCORE_HAIKU to avoid breaking the dashboard query.
        if session_type == "sonnet":
            action = ACTION_BATCH_SCORE_SONNET
        else:
            action = ACTION_BATCH_SCORE_HAIKU
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
