"""Feedback blueprint — Resume Feedback routes.

Routes:
    GET  /feedback                    -- Feedback page listing detected preferences
    POST /feedback/<id>/toggle        -- Toggle accept/reject for a preference
    POST /feedback/consolidate        -- Manual trigger for preference consolidation
"""

import sqlite3

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    url_for,
)

from job_finder.web.db_helpers import get_db

feedback_bp = Blueprint("feedback", __name__, url_prefix="/feedback")

def _query_preferences(conn):
    """Query all preferences with job context, grouped by job."""
    rows = conn.execute(
        """
        SELECT
            rpd.*,
            j.title AS job_title,
            j.company AS job_company
        FROM resume_preferences_detected rpd
        LEFT JOIN jobs j ON rpd.job_id = j.dedup_key
        ORDER BY rpd.detected_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]

def _get_stats(conn, preferences):
    """Compute summary stats for the feedback page."""
    total = len(preferences)
    accepted = sum(1 for p in preferences if p["accepted"])
    pending_consolidation = sum(
        1 for p in preferences if p["accepted"] == 1 and p["applied_at"] is None
    )
    return {
        "total": total,
        "accepted": accepted,
        "pending_consolidation": pending_consolidation,
    }

@feedback_bp.route("/", strict_slashes=False)
def index():
    """Feedback page — display all detected resume preferences."""
    conn = get_db(current_app.config["DB_PATH"])

    try:
        preferences = _query_preferences(conn)
    except sqlite3.OperationalError:
        # Table may not exist on fresh installs before Migration 5
        preferences = []

    stats = _get_stats(conn, preferences)

    # Group preferences by job for display
    jobs_map = {}
    for pref in preferences:
        job_id = pref["job_id"]
        if job_id not in jobs_map:
            jobs_map[job_id] = {
                "job_id": job_id,
                "job_title": pref.get("job_title") or "Unknown Job",
                "job_company": pref.get("job_company") or "Unknown Company",
                "preferences": [],
            }
        jobs_map[job_id]["preferences"].append(pref)

    job_groups = list(jobs_map.values())

    return render_template(
        "feedback/index.html",
        job_groups=job_groups,
        preferences=preferences,
        stats=stats,
    )

@feedback_bp.route("/<int:pref_id>/toggle", methods=["POST"], strict_slashes=False)
def toggle(pref_id: int):
    """Toggle accept/reject for a specific preference.

    HTMX: returns updated preference row partial via outerHTML swap.
    """
    conn = get_db(current_app.config["DB_PATH"])

    # Toggle accepted column
    row = conn.execute(
        "SELECT * FROM resume_preferences_detected WHERE id=?", (pref_id,)
    ).fetchone()

    if row is None:
        return "", 404

    new_accepted = 0 if row["accepted"] else 1
    conn.execute(
        "UPDATE resume_preferences_detected SET accepted=? WHERE id=?",
        (new_accepted, pref_id),
    )
    conn.commit()

    # Re-fetch updated row with job context
    updated = conn.execute(
        """
        SELECT rpd.*, j.title AS job_title, j.company AS job_company
        FROM resume_preferences_detected rpd
        LEFT JOIN jobs j ON rpd.job_id = j.dedup_key
        WHERE rpd.id=?
        """,
        (pref_id,),
    ).fetchone()

    return render_template(
        "feedback/_preference_row.html",
        pref=dict(updated),
    ), 200

@feedback_bp.route("/consolidate", methods=["POST"], strict_slashes=False)
def consolidate():
    """Manual trigger for preference consolidation."""
    config = current_app.config.get("JF_CONFIG", {})
    db_path = current_app.config.get("DB_PATH", "jobs.db")

    try:
        from job_finder.web.resume_feedback import run_preference_consolidation
        result = run_preference_consolidation(db_path, config)
        if result.get("consolidated"):
            flash(
                f"Consolidated {result['original_count']} preferences into "
                f"{result['consolidated_count']} canonical rules.",
                "success",
            )
        elif result.get("budget_exceeded"):
            flash("Budget exceeded — consolidation skipped.", "warning")
        else:
            flash(
                f"Not enough preferences to consolidate yet ({result.get('count', 0)} total, need >10).",
                "info",
            )
    except Exception as exc:
        flash(f"Consolidation failed: {exc}", "error")

    return redirect(url_for("feedback.index"))
