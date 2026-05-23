"""Sync blueprint -- Gmail sync start, status, dismiss routes."""

import logging
import threading
from datetime import UTC, datetime

from flask import Blueprint, current_app, render_template, url_for

from job_finder.json_utils import utc_now_iso
from job_finder.web.activity_tracker import ACTION_SYNC, log_activity
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

sync_bp = Blueprint("sync", __name__, url_prefix="/dashboard")


@sync_bp.route("/sync/start", methods=["POST"], strict_slashes=False)
def sync_start():
    """Start async sync — returns HTMX polling fragment.

    Inserts a batch_score_sessions row with session_type='sync' and spawns a
    background thread (skipped in TESTING mode). Returns a progress fragment
    that polls every 2s. Rejects duplicate clicks if a sync is already running.
    """
    db_path = current_app.config["DB_PATH"]
    testing = current_app.config.get("TESTING", False)

    with standalone_connection(db_path) as conn:
        # Duplicate guard: reject if a non-terminal sync session already exists
        existing = conn.execute(
            "SELECT id FROM batch_score_sessions "
            "WHERE session_type='sync' AND status NOT IN ('done', 'error', 'cancelled')"
        ).fetchone()
        if existing:
            return render_template(
                "dashboard/_sync_done.html",
                status="already_running",
                error_msg=None,
                total=0,
                scored=0,
                skipped=0,
            )

        now = utc_now_iso()
        cursor = conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('sync', 'running', 0, 0, ?)",
            (now,),
        )
        conn.commit()
        session_id = cursor.lastrowid

    if not testing:
        t = threading.Thread(
            target=_run_sync_bg,
            args=(db_path, session_id, current_app._get_current_object()),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_sync_progress.html",
        session_id=session_id,
        phase_label="Starting...",
    )


@sync_bp.route("/sync/status/<int:session_id>", strict_slashes=False)
def sync_status(session_id):
    """Poll route for sync progress.

    Returns _sync_progress.html (WITH hx-trigger) when still running.
    Returns _sync_done.html (WITHOUT polling hx-trigger) when done/error.
    Uses own sqlite3 connection — safe for HTMX polling outside request context.
    """
    db_path = current_app.config["DB_PATH"]

    with standalone_connection(db_path) as conn:
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(
            "dashboard/_sync_done.html",
            status="error",
            error_msg="Session not found",
            total=0,
            scored=0,
            skipped=0,
        )

    status = session["status"]

    # Timeout safety net: if session has been running for >30 minutes, auto-mark as error
    if status not in ("done", "error", "cancelled") and session["started_at"]:
        try:
            started = datetime.fromisoformat(session["started_at"])
            elapsed_minutes = (
                datetime.now(UTC).replace(tzinfo=None) - started
            ).total_seconds() / 60
            if elapsed_minutes > 30:
                logger.warning(
                    "Sync session %s timed out after %.1f minutes", session_id, elapsed_minutes
                )
                with standalone_connection(db_path) as timeout_conn:
                    timeout_conn.execute(
                        "UPDATE batch_score_sessions SET status='error', error_msg=?, finished_at=? "
                        "WHERE id=? AND status NOT IN ('done', 'error', 'cancelled')",
                        ("Session timed out (>30 min)", utc_now_iso(), session_id),
                    )
                    timeout_conn.commit()
                return render_template(
                    "dashboard/_sync_done.html",
                    status="error",
                    error_msg="Session timed out (>30 min)",
                    total=session["total"],
                    scored=session["scored"],
                    skipped=session["skipped"],
                )
        except (ValueError, TypeError):
            logger.debug("Sync timeout check failed for session %s", session_id, exc_info=True)

    # Terminal states: done, error, cancelled — return done fragment (NO polling hx-trigger)
    if status in ("done", "error", "cancelled"):
        return render_template(
            "dashboard/_sync_done.html",
            status=status,
            error_msg=session["error_msg"] if status == "error" else None,
            total=session["total"],
            scored=session["scored"],
            skipped=session["skipped"],
        )

    # Still running — determine phase label from status field
    phase_labels = {
        "running": "Starting...",
        "gmail": "Syncing...",
    }
    phase_label = phase_labels.get(status, "Syncing...")

    return render_template(
        "dashboard/_sync_progress.html",
        session_id=session_id,
        phase_label=phase_label,
    )


@sync_bp.route("/sync/dismiss", strict_slashes=False)
def sync_dismiss():
    """Return the original Sync Now button so it reappears after auto-dismiss."""
    start_url = url_for("sync.sync_start")
    return (
        '<div id="sync-status">'
        f'<form hx-post="{start_url}" hx-target="#sync-status" hx-swap="outerHTML">'
        '<button type="submit" class="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 '
        'text-white text-sm font-medium rounded-lg transition-colors">Sync Now</button>'
        "</form></div>"
    )


def _run_sync_bg(db_path: str, session_id: int, app) -> None:
    """Background thread: run full sync pipeline and update session row.

    Updates status to 'gmail' (fetching) then 'done' or 'error' when complete.
    Stores results in scored (jobs_new), total (total_fetched), skipped (error_count).
    Logs activity via log_activity().

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        app: Flask application instance (use _get_current_object() before thread spawn).
    """
    from job_finder.web.scheduler import run_sync_now

    try:
        with standalone_connection(db_path) as conn:
            # Mark as fetching phase
            conn.execute(
                "UPDATE batch_score_sessions SET status='gmail' WHERE id=?",
                (session_id,),
            )
            conn.commit()

        # Run the full sync pipeline synchronously in this background thread
        with app.app_context():
            summary = run_sync_now(app)

        # Store results: scored=jobs_new, total=total_fetched, skipped=error_count
        jobs_new = summary.get("jobs_new", 0)
        total_fetched = (
            summary.get("gmail_fetched", 0)
            + summary.get("serpapi_fetched", 0)
            + summary.get("thordata_fetched", 0)
        )
        error_count = (
            len(summary.get("gmail_errors", []))
            + len(summary.get("serpapi_errors", []))
            + len(summary.get("thordata_errors", []))
        )

        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE batch_score_sessions SET status='done', scored=?, total=?, skipped=?, finished_at=? "
                "WHERE id=?",
                (jobs_new, total_fetched, error_count, utc_now_iso(), session_id),
            )
            conn.commit()

        try:
            metadata = {
                "jobs_new": jobs_new,
                "gmail_fetched": summary.get("gmail_fetched", 0),
                "imap_fetched": summary.get("imap_fetched", 0),
                "serpapi_fetched": summary.get("serpapi_fetched", 0),
                "thordata_fetched": summary.get("thordata_fetched", 0),
                "dataforseo_fetched": summary.get("dataforseo_fetched", 0),
                "portal_search_fetched": summary.get("portal_search_fetched", 0),
                "portal_search_used_fallback_keywords": summary.get(
                    "portal_search_used_fallback_keywords", False
                ),
                "duration_seconds": summary.get("duration_seconds", 0.0),
                "status": "success",
            }
            # Per-portal breakdown (Stage 7.9 follow-up). ingestion_runner's
            # _fetch_portal_search injects portal_<name>_fetched keys for any
            # portal that returned at least one job. Scoop them up dynamically
            # so adding a new portal doesn't require touching this dict.
            for key, value in summary.items():
                if (
                    key.startswith("portal_")
                    and key.endswith("_fetched")
                    and key not in metadata
                ):
                    metadata[key] = value
            log_activity(
                db_path,
                ACTION_SYNC,
                metadata=metadata,
            )
        except Exception:
            pass

    except Exception as e:
        logger.error("Async sync background thread failed: %s", e)
        try:
            with standalone_connection(db_path) as conn:
                conn.execute(
                    "UPDATE batch_score_sessions SET status='error', error_msg=?, finished_at=? WHERE id=?",
                    (str(e), utc_now_iso(), session_id),
                )
                conn.commit()
        except Exception:
            pass
        try:
            log_activity(
                db_path,
                ACTION_SYNC,
                metadata={"status": "failed", "error": type(e).__name__},
            )
        except Exception:
            pass
