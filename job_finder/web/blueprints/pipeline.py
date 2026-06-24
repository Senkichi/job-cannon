"""Pipeline blueprint — Kanban board with drag-and-drop status management."""

from flask import (
    Blueprint,
    render_template,
    request,
)

from job_finder.db import get_jobs_by_status, update_pipeline_status
from job_finder.web._htmx import htmx_fragment
from job_finder.web.blueprints import PIPELINE_STATUSES
from job_finder.web.db_helpers import get_db

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/pipeline")

# Columns always visible regardless of whether they have jobs.
CORE_COLUMNS = ["discovered", "reviewing", "applied", "phone_screen", "rejected"]

# Columns that only appear when they have jobs.
DYNAMIC_COLUMNS = ["technical", "onsite", "offer", "accepted"]


def _build_columns(conn) -> list[dict]:
    """Build the ordered Kanban column list.

    Core columns are always present (even if empty); dynamic columns appear
    only when populated. Shared by the full-page render and the SSE board
    fragment so the two never drift.
    """
    jobs_by_status = get_jobs_by_status(conn)

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
    return columns


@pipeline_bp.route("/", strict_slashes=False)
def index():
    """Kanban pipeline view — jobs grouped by pipeline stage."""
    conn = get_db()
    return render_template("pipeline/index.html", columns=_build_columns(conn))


@pipeline_bp.route("/board", strict_slashes=False)
@htmx_fragment("pipeline.index")
def board_fragment():
    """HTMX fragment — the inner Kanban columns.

    Refetched on ``sse:pipeline-changed`` / ``sse:jobs-changed`` so cards that
    move via the background pipeline-detection job (or get archived by the
    nightly staleness check) re-flow live. The page re-initialises SortableJS
    on ``htmx:afterSettle`` (see pipeline/index.html); a drag-in-progress guard
    on the trigger keeps a background refresh from yanking a card mid-drag.
    """
    conn = get_db()
    return render_template("pipeline/_board_columns.html", columns=_build_columns(conn))


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

    conn = get_db()
    update_pipeline_status(conn, job_id, new_status, source="manual")

    return ("", 200)
