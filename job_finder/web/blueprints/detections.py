"""Detections blueprint — HTMX routes for pipeline detection confirm/dismiss actions."""

import logging

from flask import Blueprint, make_response, render_template

from job_finder.db import get_dashboard_stats, resolve_detection, update_pipeline_status
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

detections_bp = Blueprint("detections", __name__, url_prefix="/detections")

# Map detection_type -> pipeline_status applied when user confirms
_DETECTION_TYPE_TO_STATUS = {
    "rejection": "rejected",
    "interview": "phone_screen",
    "confirmation": "applied",
}


def _render_pipeline_review_header_oob(conn) -> str:
    """Render the pipeline-review h2 + badge as an OOB swap so the dashboard
    counter stays in sync after a confirm/dismiss action.

    Without this, the badge in the page header keeps showing the pre-action
    count even after the card fades out — the badge is rendered once on full
    page load and the HTMX swap that removes the card doesn't touch it.
    """
    try:
        stats = get_dashboard_stats(conn)
        pending_count = stats.get("pending_detections", 0)
    except Exception:
        # If the count query fails, skip the OOB update rather than break the
        # primary card swap. The badge will be stale until the next page load.
        return ""
    return render_template(
        "dashboard/_pipeline_review_header.html",
        pending_count=pending_count,
        oob=True,
    )


def _with_dashboard_refresh(html: str):
    """Wrap an HTMX fragment in a response that fires the ``dashboard-refresh`` event.

    The OOB swap in ``_render_pipeline_review_header_oob`` only refreshes the
    in-section h2 badge. The top-right "Pending Review" stat card lives inside
    ``#dashboard-stats``, which re-fetches on ``dashboard-refresh from:body`` —
    the same event sync/batch-scoring emit on completion. Without this header
    the stat card stays frozen at its page-load value until a full reload.
    """
    resp = make_response(html)
    resp.headers["HX-Trigger"] = "dashboard-refresh"
    return resp


@detections_bp.route("/<int:detection_id>/confirm", methods=["POST"], strict_slashes=False)
def confirm(detection_id: int):
    """Confirm a pipeline detection: apply the status change and resolve the record.

    Returns an HTMX partial (_detection_confirmed.html) that auto-removes after 3 seconds.
    """
    conn = get_db()

    # Fetch the detection record
    row = conn.execute(
        """SELECT pd.*, j.company AS job_company
           FROM pipeline_detections pd
           LEFT JOIN jobs j ON pd.job_id = j.dedup_key
           WHERE pd.id = ?""",
        (detection_id,),
    ).fetchone()

    if row is None:
        return "", 404

    detection = dict(row)
    detection_type = detection.get("detection_type", "")
    if detection_type not in _DETECTION_TYPE_TO_STATUS:
        logger.warning(
            "Detection %d has unknown detection_type %r; defaulting to 'reviewing'",
            detection_id,
            detection_type,
        )
    new_status = _DETECTION_TYPE_TO_STATUS.get(detection_type, "reviewing")
    job_id = detection.get("job_id")
    company = detection.get("job_company") or "Unknown Company"

    # Apply the pipeline status change
    if job_id:
        try:
            update_pipeline_status(conn, job_id, new_status, source="email")
        except Exception as e:
            logger.error("Failed to update pipeline status for %s: %s", job_id, e)
            return f"Error: could not update pipeline status — {e}", 500

    # Resolve the detection as confirmed
    try:
        resolve_detection(conn, detection_id, "confirmed")
    except Exception as e:
        logger.error("Failed to resolve detection %d: %s", detection_id, e)
        return f"Error: could not resolve detection — {e}", 500

    logger.info(
        "Detection %d confirmed: %s -> %s (job: %s)",
        detection_id,
        detection_type,
        new_status,
        job_id,
    )

    primary = render_template(
        "dashboard/_detection_confirmed.html",
        detection_id=detection_id,
        company=company,
        new_status=new_status,
    )
    return _with_dashboard_refresh(primary + _render_pipeline_review_header_oob(conn))


@detections_bp.route("/<int:detection_id>/dismiss", methods=["POST"], strict_slashes=False)
def dismiss(detection_id: int):
    """Dismiss a pipeline detection: resolve as dismissed, remove card silently.

    Returns an empty string so HTMX outerHTML swap removes the card from the DOM.
    """
    conn = get_db()

    # Verify detection exists
    row = conn.execute(
        "SELECT id FROM pipeline_detections WHERE id = ?",
        (detection_id,),
    ).fetchone()

    if row is None:
        return "", 404

    # Resolve the detection as dismissed (pipeline_status unchanged)
    try:
        resolve_detection(conn, detection_id, "dismissed")
    except Exception as e:
        logger.error("Failed to dismiss detection %d: %s", detection_id, e)

    logger.info("Detection %d dismissed", detection_id)
    # Empty primary swap removes the card; OOB header re-renders the badge
    # with the decremented count so the dashboard counter doesn't lie. The
    # dashboard-refresh trigger also re-fetches the top-right stat card.
    return _with_dashboard_refresh(_render_pipeline_review_header_oob(conn))
