"""Dashboard blueprint — overview stats, activity feed, pipeline summary."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from job_finder.db import (
    _JOBS_ALL_COLUMNS,
    get_dashboard_stats,
    get_pending_detections,
    get_pipeline_summary,
    get_recent_activity,
    get_recent_pipeline_events,
    get_recent_runs,
)
from job_finder.config import DEFAULT_HAIKU_THRESHOLD, DEFAULT_MONTHLY_BUDGET_USD
from job_finder.json_utils import utc_now_iso
from job_finder.web.activity_tracker import log_activity, ACTION_SYNC
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.claude_client import get_cost_stats
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _get_ats_context(conn):
    """Query ATS scan stat card data for the Dashboard.

    Returns dict with last_scan info, company counts.
    Handles missing companies table gracefully (pre-migration or error cases).
    """
    try:
        # Most recent scan summary
        last_scan = conn.execute(
            """SELECT scanned_at, SUM(jobs_found) as total_found, COUNT(*) as companies_scanned
               FROM company_scan_log
               WHERE scanned_at = (SELECT MAX(scanned_at) FROM company_scan_log)
               GROUP BY scanned_at"""
        ).fetchone()

        # Company counts
        counts = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN ats_probe_status='hit' THEN 1 ELSE 0 END) as ats_tracked
               FROM companies"""
        ).fetchone()

    except Exception:
        last_scan = None
        counts = None

    return {
        "last_scan": last_scan,
        "company_count": (counts["total"] or 0) if counts else 0,
        "ats_tracked_count": (counts["ats_tracked"] or 0) if counts else 0,
    }


def _get_rejection_context(conn):
    """Query rejection insights context for the Dashboard.

    Returns dict with latest_report (sqlite3.Row or None) and
    unreviewed_rejection_count (int).
    Handles missing table gracefully (pre-migration or error cases).
    """
    try:
        latest_report = conn.execute(
            "SELECT id, report_text, rejections_analyzed, generated_at, cost_usd "
            "FROM rejection_reports ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        unreviewed_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status='rejected' AND rejection_reviewed=0"
        ).fetchone()[0]
    except Exception:
        latest_report = None
        unreviewed_count = 0
    return {
        "latest_report": latest_report,
        "unreviewed_rejection_count": unreviewed_count,
    }


@dashboard_bp.route("/", strict_slashes=False)
def index():
    """Dashboard landing page — stat cards, activity feed, pipeline summary."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    stats = get_dashboard_stats(conn)
    recent_runs = get_recent_runs(conn, limit=10)
    user_activity = get_recent_activity(conn, limit=15)
    pipeline_summary = get_pipeline_summary(conn)
    pending_detections = get_pending_detections(conn)
    pipeline_events = get_recent_pipeline_events(conn, limit=10)
    config = current_app.config.get("JF_CONFIG", {})
    budget_cap = config.get("scoring", {}).get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)
    pending_count = stats.get("pending_detections", 0)
    rejection_ctx = _get_rejection_context(conn)
    ats_ctx = _get_ats_context(conn)

    # Count jobs eligible for Sonnet evaluation (haiku_score >= threshold, no sonnet_score)
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    try:
        sonnet_eligible_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
            "AND sonnet_score IS NULL AND jd_full IS NOT NULL",
            (threshold,),
        ).fetchone()[0]
    except Exception:
        sonnet_eligible_count = 0

    return render_template(
        "dashboard/index.html",
        stats=stats,
        recent_runs=recent_runs,
        user_activity=user_activity,
        pipeline_summary=pipeline_summary,
        cost_stats=cost_stats,
        budget_cap=budget_cap,
        pending_detections=pending_detections,
        pending_count=pending_count,
        pipeline_events=pipeline_events,
        latest_report=rejection_ctx["latest_report"],
        unreviewed_rejection_count=rejection_ctx["unreviewed_rejection_count"],
        sonnet_eligible_count=sonnet_eligible_count,
        ats_last_scan=ats_ctx["last_scan"],
        company_count=ats_ctx["company_count"],
        ats_tracked_count=ats_ctx["ats_tracked_count"],
    )


@dashboard_bp.route("/cost-detail", strict_slashes=False)
def cost_detail():
    """HTMX partial — returns cost breakdown panel."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    config = current_app.config.get("JF_CONFIG", {})
    budget_cap = config.get("scoring", {}).get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)

    return render_template(
        "dashboard/_cost_detail.html",
        cost_stats=cost_stats,
        budget_cap=budget_cap,
    )


@dashboard_bp.route("/batch-score/haiku/start", methods=["POST"], strict_slashes=False)
def batch_score_haiku_start():
    """Start async Haiku batch scoring — returns HTMX polling fragment.

    Counts unscored jobs and either returns a done fragment immediately
    (nothing to score) or inserts a batch_score_sessions row and starts a
    daemon thread, returning a progress fragment that polls every 2s.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL"
        ).fetchone()[0]

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Haiku",
                scored=0,
                skipped=0,
                status="done",
                message="All jobs already scored — nothing to do.",
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
    finally:
        conn.close()

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
        cancelling=False,
    )


@dashboard_bp.route("/batch-score/sonnet/start", methods=["POST"], strict_slashes=False)
def batch_score_sonnet_start():
    """Start async Sonnet batch evaluation — returns HTMX polling fragment.

    Counts jobs qualifying for Sonnet (haiku_score >= threshold, no sonnet_score,
    jd_full present). Returns done fragment if none qualify.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
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
    finally:
        conn.close()

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
        cancelling=False,
    )


@dashboard_bp.route("/batch-score/status/<int:session_id>", strict_slashes=False)
def batch_score_status(session_id):
    """Poll route for batch scoring progress.

    Returns _batch_score_progress.html (WITH hx-trigger) when still running.
    Returns _batch_score_done.html (WITHOUT hx-trigger) when done/error/cancelled.
    Uses own sqlite3 connection — safe for HTMX polling outside request context.
    """
    db_path = current_app.config["DB_PATH"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

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
                timeout_conn = sqlite3.connect(db_path)
                try:
                    timeout_conn.execute(
                        "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? "
                        "WHERE id = ? AND status IN ('running', 'cancelling')",
                        ("Session timed out (>30 min)", utc_now_iso(), session_id),
                    )
                    timeout_conn.commit()
                finally:
                    timeout_conn.close()
                return render_template(
                    "dashboard/_batch_score_done.html",
                    label=label,
                    scored=session["scored"],
                    skipped=session["skipped"],
                    status="error",
                    message=None,
                    error_msg="Session timed out (>30 min)",
                )
        except (ValueError, TypeError):
            pass

    # Terminal states: done, error, cancelled — return done fragment (NO hx-trigger)
    if status in ("done", "error", "cancelled"):
        return render_template(
            "dashboard/_batch_score_done.html",
            label=label,
            scored=session["scored"],
            skipped=session["skipped"],
            status=status,
            message=None,
            error_msg=session["error_msg"] if status == "error" else None,
        )

    # Still running (running or cancelling) — return progress fragment (WITH hx-trigger)
    return render_template(
        "dashboard/_batch_score_progress.html",
        label=label,
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        cancelling=(status == "cancelling"),
    )


@dashboard_bp.route("/batch-score/cancel/<int:session_id>", methods=["POST"], strict_slashes=False)
def batch_score_cancel(session_id):
    """Cancel a running batch score session.

    Sets status='cancelling' in DB. The background thread checks status
    before each job and will set status='cancelled' when it sees 'cancelling'.
    Returns a progress fragment that keeps polling until the thread finishes.
    """
    db_path = current_app.config["DB_PATH"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "UPDATE batch_score_sessions SET status = 'cancelling' WHERE id = ? AND status = 'running'",
            (session_id,),
        )
        conn.commit()
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

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
        import anthropic

        from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_haiku

        profile = load_scoring_profile(config)
        client = anthropic.Anthropic()
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            f"SELECT {_JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NULL ORDER BY score DESC"
        ).fetchall()

        for row in rows:
            # Check cancellation status before each job
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

            job_row = dict(row)

            # Pre-Haiku exclusion filter
            exclusions = config.get("profile", {}).get("exclusions", {})
            profile_min_salary = config.get("profile", {}).get("min_salary")
            excluded, reason = should_exclude(job_row, exclusions, profile_min_salary)
            if excluded:
                logger.info("Batch Haiku: excluded '%s': %s", job_row.get("dedup_key"), reason)
                _update_session_counter(conn, session_id, "skipped")
                continue

            try:
                result = score_and_persist_haiku(conn, job_row, config, client, profile)
                _update_session_counter(conn, session_id, "scored" if result is not None else "skipped")
            except Exception as e:
                logger.warning(
                    "Batch Haiku: error scoring job '%s': %s -- continuing",
                    job_row.get("dedup_key"), e,
                )
                _update_session_counter(conn, session_id, "skipped")

        # All jobs processed — mark done
        _finish_session(conn, db_path, session_id, "done", "haiku")

    except Exception as e:
        logger.error("Batch Haiku background thread failed: %s", e)
        _fail_session(conn, db_path, session_id, e, "haiku")
    finally:
        conn.close()


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
        import anthropic

        from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_sonnet
    except ImportError as e:
        _mark_session_error(db_path, session_id, f"Import error: {e}")
        return

    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    profile = load_scoring_profile(config)
    client = anthropic.Anthropic()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            f"SELECT {_JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
            "AND sonnet_score IS NULL AND jd_full IS NOT NULL ORDER BY haiku_score DESC",
            (threshold,),
        ).fetchall()

        for row in rows:
            # Check cancellation status before each job
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

            job_row = dict(row)
            try:
                result = score_and_persist_sonnet(conn, job_row, config, client, profile)
                _update_session_counter(conn, session_id, "scored" if result is not None else "skipped")
            except Exception as e:
                logger.warning(
                    "Batch Sonnet: error evaluating job '%s': %s -- continuing",
                    job_row.get("dedup_key"), e,
                )
                _update_session_counter(conn, session_id, "skipped")

        # All jobs processed — mark done
        _finish_session(conn, db_path, session_id, "done", "sonnet")

    except Exception as e:
        logger.error("Batch Sonnet background thread failed: %s", e)
        _fail_session(conn, db_path, session_id, e, "sonnet")
    finally:
        conn.close()


def _update_session_counter(conn, session_id: int, counter: str) -> None:
    """Increment a batch session counter ('scored' or 'skipped') and commit."""
    if counter not in ("scored", "skipped"):
        raise ValueError(f"Invalid counter name: {counter!r}")
    conn.execute(
        f"UPDATE batch_score_sessions SET {counter} = {counter} + 1 WHERE id = ?",
        (session_id,),
    )
    conn.commit()


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


def _fail_session(conn, db_path: str, session_id: int, error: Exception, session_type: str) -> None:
    """Mark a batch session as errored and log the failure."""
    try:
        conn.execute(
            "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? WHERE id = ?",
            (str(error), utc_now_iso(), session_id),
        )
        conn.commit()
    except Exception:
        pass
    try:
        from job_finder.web.activity_tracker import (
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            log_activity,
        )
        action = ACTION_BATCH_SCORE_HAIKU if session_type == "haiku" else ACTION_BATCH_SCORE_SONNET
        log_activity(
            db_path,
            action,
            metadata={"session_type": session_type, "status": "failed", "error": type(error).__name__},
        )
    except Exception:
        pass


def _mark_session_error(db_path: str, session_id: int, error_msg: str) -> None:
    """Mark a batch session as errored. Used for background thread import failures."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? WHERE id = ?",
                (error_msg, utc_now_iso(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to mark session %s as error: %s", session_id, error_msg, exc_info=True)


@dashboard_bp.route("/sync", methods=["POST"], strict_slashes=False)
def sync():
    """Quick action: Sync Now — triggers immediate ingestion via pipeline runner."""
    db_path = current_app.config["DB_PATH"]
    try:
        from job_finder.web.scheduler import trigger_sync

        summary = trigger_sync(current_app._get_current_object())

        jobs_new = summary.get("jobs_new", 0)
        gmail_fetched = summary.get("gmail_fetched", 0)
        serpapi_fetched = summary.get("serpapi_fetched", 0)
        total_fetched = gmail_fetched + serpapi_fetched
        errors = summary.get("gmail_errors", []) + summary.get("serpapi_errors", [])

        detection_auto_updated = summary.get("detection_auto_updated", 0)
        detection_queued = summary.get("detection_queued", 0)
        pipeline_msg = (
            f" Pipeline: {detection_auto_updated} auto-updated, {detection_queued} queued."
        )

        try:
            log_activity(
                db_path,
                ACTION_SYNC,
                metadata={
                    "jobs_new": jobs_new,
                    "gmail_fetched": gmail_fetched,
                    "serpapi_fetched": serpapi_fetched,
                    "duration_seconds": summary.get("duration_seconds", 0.0),
                    "status": "success",
                },
            )
        except Exception:
            pass

        if errors:
            error_msgs = "; ".join(str(e) for e in errors[:3])
            flash(
                f"Sync completed with errors: {error_msgs}. "
                f"Fetched {total_fetched} jobs, {jobs_new} new.{pipeline_msg}",
                "warning",
            )
        else:
            flash(
                f"Sync complete: fetched {total_fetched} jobs "
                f"({gmail_fetched} Gmail, {serpapi_fetched} SerpAPI), {jobs_new} new."
                f"{pipeline_msg}",
                "success",
            )

    except Exception as e:
        flash(f"Sync failed: {e}", "error")
        try:
            log_activity(
                db_path,
                ACTION_SYNC,
                metadata={"status": "failed", "error": type(e).__name__},
            )
        except Exception:
            pass

    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/rejection-analysis", methods=["POST"], strict_slashes=False)
def rejection_analysis():
    """On-demand rejection analysis trigger.

    Calls run_rejection_analysis synchronously and flashes a result message.
    Redirects back to the Dashboard index.
    """
    from job_finder.web.rejection_analyzer import run_rejection_analysis

    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    try:
        result = run_rejection_analysis(db_path, config)
        count = result.get("rejections_analyzed", 0)
        if result.get("budget_exceeded"):
            flash("Rejection analysis skipped: monthly budget cap reached.", "warning")
        elif count == 0:
            flash("No unreviewed rejections to analyze.", "info")
        else:
            flash(f"Rejection analysis complete: {count} rejections analyzed.", "success")
    except Exception as e:
        logger.error("On-demand rejection analysis failed: %s", e)
        flash(f"Rejection analysis failed: {e}", "error")
    return redirect(url_for("dashboard.index"))
