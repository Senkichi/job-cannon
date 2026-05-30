"""Updates blueprint — update-check banner dismiss endpoint.

Routes:
    POST /updates/dismiss/<version>  -- Append version to dismissed_versions
                                        in update_check.json. HTMX-only.
"""

import logging

from flask import Blueprint, redirect, request, url_for

from job_finder.web.update_check import append_dismissed_version

logger = logging.getLogger(__name__)

updates_bp = Blueprint("updates", __name__, url_prefix="/updates")

_MAX_VERSION_LEN = 64


@updates_bp.route("/dismiss/<version>", methods=["POST"], strict_slashes=False)
def dismiss(version: str):
    """HTMX POST — append version to update_check.json.dismissed_versions.

    Returns 200 with empty body so HTMX hx-swap="delete" removes the banner.
    Non-HTMX direct-browser hits redirect to the dashboard.
    """
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))

    # Shape check — defuse hostile clients POSTing arbitrary version strings.
    # Whitelist alphanumeric, dot, hyphen, plus, leading v. Match the same
    # filter applied in update_check._fetch_and_persist for parity.
    if not version or len(version) > _MAX_VERSION_LEN:
        return ("Invalid version", 400)
    if not all(c.isalnum() or c in ".-+v" for c in version):
        return ("Invalid version", 400)

    try:
        append_dismissed_version(version)
    except Exception as e:
        logger.info("Failed to persist dismissal of %s: %s", version, e)
        # Still return 200 — banner should still disappear visually (D-06 UX primacy).

    return ("", 200)
