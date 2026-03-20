"""Pipeline blueprint — Kanban board with drag-and-drop status management."""

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
)

from job_finder.db import get_jobs_by_status, update_pipeline_status
from job_finder.web.blueprints import PIPELINE_STATUSES, trigger_interview_prep_if_applied
from job_finder.web.db_helpers import get_db

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/pipeline")

# Columns always visible regardless of whether they have jobs.
CORE_COLUMNS = ["discovered", "reviewing", "applied", "phone_screen", "rejected"]

# Columns that only appear when they have jobs.
DYNAMIC_COLUMNS = ["technical", "onsite", "offer", "accepted"]

# Columns always hidden in the Kanban (they clutter the board).
HIDDEN_COLUMNS = ["archived", "withdrawn"]


@pipeline_bp.route("/", strict_slashes=False)
def index():
    """Kanban pipeline view — jobs grouped by pipeline stage."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    jobs_by_status = get_jobs_by_status(conn)

    # Build the ordered column list for the template.
    # Core columns always present (even if empty); dynamic columns only when populated.
    columns = []
    for status in CORE_COLUMNS:
        columns.append(
            {
                "status": status,
                "label": status.replace("_", " ").title(),
                "jobs": jobs_by_status.get(status, []),
                "always_visible": True,
                "collapsed": status == "rejected",  # Rejected starts collapsed
            }
        )
    for status in DYNAMIC_COLUMNS:
        status_jobs = jobs_by_status.get(status, [])
        if status_jobs:
            columns.append(
                {
                    "status": status,
                    "label": status.replace("_", " ").title(),
                    "jobs": status_jobs,
                    "always_visible": False,
                    "collapsed": False,
                }
            )

    return render_template(
        "pipeline/index.html",
        columns=columns,
    )


@pipeline_bp.route("/move", methods=["POST"], strict_slashes=False)
def move():
    """Accept drag-and-drop status move from SortableJS + HTMX.

    Expects form fields: job_id, new_status.
    Returns 200 with empty body on success (hx-swap="none").
    Returns 400 if job_id or new_status missing/invalid.
    """
    job_id = request.form.get("job_id", "").strip()
    new_status = request.form.get("new_status", "").strip()

    if not job_id or not new_status:
        return ("Missing job_id or new_status", 400)

    if new_status not in PIPELINE_STATUSES:
        return (f"Invalid status: {new_status}", 400)

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    update_pipeline_status(conn, job_id, new_status, source="manual")

    # Trigger interview prep generation in background when dragged to "applied"
    trigger_interview_prep_if_applied(
        job_id,
        new_status,
        db_path,
        current_app.config.get("JF_CONFIG", {}),
        testing=current_app.config.get("TESTING", False),
    )

    return ("", 200)
