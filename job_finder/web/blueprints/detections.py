"""Detections blueprint — HTMX routes for pipeline detection confirm/dismiss actions."""

import logging

from flask import Blueprint, render_template

from job_finder.db import resolve_detection, update_pipeline_status
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

detections_bp = Blueprint("detections", __name__, url_prefix="/detections")

# Map detection_type -> pipeline_status applied when user confirms
_DETECTION_TYPE_TO_STATUS = {
    "rejection": "rejected",
    "interview": "phone_screen",
    "confirmation": "applied",
}


@detections_bp.route("/<int:detection_id>/confirm", methods=["POST"], strict_slashes=False)
def confirm(detection_id: int):
    """Confirm a pipeline detection: apply the status change and resolve the record.

    Returns an HTMX partial (_detection_confirmed.html) that auto-removes after 3 seconds.
    """
    from flask import current_app

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

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

    return render_template(
        "dashboard/_detection_confirmed.html",
        detection_id=detection_id,
        company=company,
        new_status=new_status,
    )


@detections_bp.route("/<int:detection_id>/dismiss", methods=["POST"], strict_slashes=False)
def dismiss(detection_id: int):
    """Dismiss a pipeline detection: resolve as dismissed, remove card silently.

    Returns an empty string so HTMX outerHTML swap removes the card from the DOM.
    """
    from flask import current_app

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

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
    return "", 200
