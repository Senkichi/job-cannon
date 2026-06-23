"""Profile blueprint -- Profile Editor routes.

Routes:
    GET  /profile            -- Load profile, run validation, render editor
    POST /profile/save       -- Accept JSON body, save profile to disk
    POST /profile/reorder-positions -- HTMX: reorder positions and save
    POST /profile/reorder-skills    -- HTMX: reorder top-level skills and save
"""

import json
import logging
import os

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.web import user_data_dirs
from job_finder.web.profile_schema import (
    load_profile,
    save_profile,
    validate_profile,
)

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")


def _profile_path() -> str:
    """Resolve the experience_profile.json path fresh per call.

    Resolved per-request (NOT frozen at import) so JOB_CANNON_USER_DATA_DIR
    redirects — including each test's temp dir — always win, mirroring
    settings._config_path(). The onboarding wizard (which writes the profile)
    and the scorer both resolve the same user_data_dirs.profile_path(), so all
    three agree on one location instead of the editor/scorer reading a bare
    CWD-relative file that onboarding never wrote to.
    """
    return str(user_data_dirs.profile_path())


def _get_all_skills(profile: dict) -> list:
    """Gather all unique skills from positions for autocomplete."""
    seen = set()
    top_skills = profile.get("skills", [])
    all_skills = list(top_skills)
    seen.update(top_skills)

    for pos in profile.get("positions", []):
        for skill in pos.get("skills", []):
            if skill and skill not in seen:
                all_skills.append(skill)
                seen.add(skill)

    return all_skills


def _load_profile_page_extras() -> dict:
    """Load supplementary variables required by profile/index.html."""
    profile_mtime = 0
    try:
        profile_mtime = os.path.getmtime(_profile_path())
    except OSError:
        pass

    return {"profile_mtime": profile_mtime}


@profile_bp.route("/", strict_slashes=False)
def index():
    """Profile Editor -- display experience_profile.json in editable form."""
    profile = load_profile(_profile_path())
    warnings = validate_profile(profile)
    all_skills = _get_all_skills(profile)
    extras = _load_profile_page_extras()

    return render_template(
        "profile/index.html",
        profile=profile,
        warnings=warnings,
        all_skills=all_skills,
        **extras,
    )


@profile_bp.route("/save", methods=["POST"], strict_slashes=False)
def save():
    """Save profile from JSON body posted by the form."""
    try:
        data = request.get_json(force=True, silent=True)
        if data is None:
            # Fallback: try form data with a 'profile_json' field
            raw = request.form.get("profile_json", "")
            data = json.loads(raw) if raw else None

        if data is None:
            flash("No profile data received.", "error")
            return redirect(url_for("profile.index"))

        # Stale-form detection: reject if file was modified since page load
        submitted_mtime = data.pop("_mtime", None)
        if submitted_mtime is not None:
            try:
                current_mtime = os.path.getmtime(_profile_path())
                if abs(float(submitted_mtime) - current_mtime) > 0.01:
                    return "Profile was modified externally. Reload the page and try again.", 409
            except OSError:
                pass  # File doesn't exist yet -- no conflict possible

        save_profile(data, _profile_path(), force=True)
        flash("Profile saved successfully.", "success")

        # HTMX requests get a lightweight redirect header; normal forms get a redirect
        if request.headers.get("HX-Request"):
            response = current_app.response_class("", status=200)
            response.headers["HX-Redirect"] = url_for("profile.index")
            return response

        return redirect(url_for("profile.index"))

    except (ValueError, KeyError) as exc:
        flash(f"Error saving profile: {exc}", "error")
        return redirect(url_for("profile.index"))


@profile_bp.route("/reorder-positions", methods=["POST"], strict_slashes=False)
def reorder_positions():
    """HTMX endpoint: reorder positions by new index list and save."""
    try:
        indices = (
            request.json if request.is_json else json.loads(request.form.get("indices", "[]"))
        )
        profile = load_profile(_profile_path())
        positions = profile.get("positions", [])

        if isinstance(indices, list) and all(isinstance(i, int) for i in indices):
            reordered = [positions[i] for i in indices if 0 <= i < len(positions)]
            # If not all indices present, fall back to original order
            if len(reordered) == len(positions):
                profile["positions"] = reordered
                save_profile(profile, _profile_path(), force=True)

        warnings = validate_profile(profile)
        all_skills = _get_all_skills(profile)
        extras = _load_profile_page_extras()
        return render_template(
            "profile/index.html",
            profile=profile,
            warnings=warnings,
            all_skills=all_skills,
            **extras,
        )
    except (ValueError, KeyError, IndexError) as exc:
        return str(exc), 400


@profile_bp.route("/reorder-skills", methods=["POST"], strict_slashes=False)
def reorder_skills():
    """HTMX endpoint: reorder top-level skills and save."""
    try:
        new_order = (
            request.json if request.is_json else json.loads(request.form.get("skills", "[]"))
        )
        profile = load_profile(_profile_path())

        if isinstance(new_order, list):
            profile["skills"] = [s for s in new_order if s]
            save_profile(profile, _profile_path(), force=True)

        warnings = validate_profile(profile)
        all_skills = _get_all_skills(profile)
        extras = _load_profile_page_extras()
        return render_template(
            "profile/index.html",
            profile=profile,
            warnings=warnings,
            all_skills=all_skills,
            **extras,
        )
    except (ValueError, KeyError) as exc:
        return str(exc), 400
