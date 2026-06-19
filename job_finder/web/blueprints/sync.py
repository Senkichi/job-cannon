"""Sync blueprint -- Gmail sync start, status, dismiss routes."""

import json
import logging
import threading

from flask import Blueprint, current_app, render_template, url_for

from job_finder.json_utils import utc_now_iso
from job_finder.web.activity_tracker import ACTION_SYNC, log_activity
from job_finder.web.db_helpers import (
    PollingSessionConfig,
    render_polling_status,
    standalone_connection,
)
from job_finder.web.live_events import COSTS_CHANGED, JOBS_CHANGED, PIPELINE_CHANGED
from job_finder.web.live_events import publish as publish_live

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
                error_details=[],
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


_SYNC_PHASE_LABELS = {"running": "Starting...", "gmail": "Syncing..."}


def _sync_done_ctx(session, status: str, error_msg: str | None) -> dict:
    raw = session["error_details"] if "error_details" in session.keys() else None  # noqa: SIM118
    error_details: list[str] = []
    if raw:
        try:
            error_details = json.loads(raw)
        except (ValueError, TypeError):
            pass
    return {
        "status": status,
        "error_msg": error_msg,
        "total": session["total"],
        "scored": session["scored"],
        "skipped": session["skipped"],
        "error_details": error_details,
    }


def _sync_progress_ctx(session) -> dict:
    return {
        "session_id": session["id"],
        "phase_label": _SYNC_PHASE_LABELS.get(session["status"], "Syncing..."),
    }


@sync_bp.route("/sync/status/<int:session_id>", strict_slashes=False)
def sync_status(session_id):
    """Poll route for sync progress (delegates to ``render_polling_status``).

    Returns _sync_progress.html (WITH hx-trigger) when still running.
    Returns _sync_done.html (WITHOUT polling hx-trigger) when done/error.
    """
    return render_polling_status(
        current_app.config["DB_PATH"],
        session_id,
        PollingSessionConfig(
            progress_template="dashboard/_sync_progress.html",
            done_template="dashboard/_sync_done.html",
            progress_ctx=_sync_progress_ctx,
            done_ctx=_sync_done_ctx,
            not_found_ctx={
                "status": "error",
                "error_msg": "Session not found",
                "total": 0,
                "scored": 0,
                "skipped": 0,
                "error_details": [],
            },
            session_label="Sync",
        ),
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
            + summary.get("imap_fetched", 0)
            + summary.get("serpapi_fetched", 0)
            + summary.get("dataforseo_fetched", 0)
            + summary.get("portal_search_fetched", 0)
        )
        # Aggregate error count across ALL sources dynamically so a new source
        # can never silently drop out of the error tally.
        all_error_messages: list[str] = []
        for key, val in summary.items():
            if key.endswith("_errors") and isinstance(val, list):
                source_label = key[: -len("_errors")]
                for msg in val:
                    all_error_messages.append(f"{source_label}: {msg}")
        error_count = len(all_error_messages)
        error_details_json = json.dumps(all_error_messages) if all_error_messages else None

        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE batch_score_sessions "
                "SET status='done', scored=?, total=?, skipped=?, error_details=?, finished_at=? "
                "WHERE id=?",
                (
                    jobs_new,
                    total_fetched,
                    error_count,
                    error_details_json,
                    utc_now_iso(),
                    session_id,
                ),
            )
            conn.commit()

        # Push live-update events so pages OTHER than the one polling this sync
        # (which already gets HX-Trigger: dashboard-refresh/jobs-updated) also
        # reflect the new rows / scores / pipeline churn.
        for _ev in (JOBS_CHANGED, COSTS_CHANGED, PIPELINE_CHANGED):
            publish_live(_ev)

        try:
            metadata = {
                "jobs_new": jobs_new,
                "gmail_fetched": summary.get("gmail_fetched", 0),
                "imap_fetched": summary.get("imap_fetched", 0),
                "serpapi_fetched": summary.get("serpapi_fetched", 0),
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
                if key.startswith("portal_") and key.endswith("_fetched") and key not in metadata:
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
