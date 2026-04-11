"""Profile blueprint -- Profile Editor routes.

Routes:
    GET  /profile            -- Load profile, run validation, render editor
    POST /profile/save       -- Accept JSON body, save profile to disk
    POST /profile/import     -- Accept .md file upload, extract via Opus
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

from job_finder.web.db_helpers import get_db

from job_finder.web.profile_schema import (
    extract_profile_from_markdown,
    load_profile,
    save_profile,
    validate_profile,
)

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")

_PROFILE_PATH = "experience_profile.json"
_UPLOAD_DIR = "data/resume_uploads"

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
    """Load supplementary variables required by profile/index.html.

    Returns a dict with keys: resume_preferences, uploads, style_guide, profile_mtime.
    All DB queries degrade gracefully if tables are absent (pre-migration or error).
    """
    db_path = current_app.config.get("DB_PATH", "jobs.db")
    conn = get_db(db_path)

    resume_preferences = []
    try:
        rows = conn.execute(
            "SELECT * FROM resume_preferences_detected "
            "WHERE accepted=1 AND applied_at IS NULL "
            "ORDER BY preference_type, detected_at DESC"
        ).fetchall()
        resume_preferences = [dict(row) for row in rows]
    except Exception:
        pass

    upload_rows = []
    try:
        rows = conn.execute(
            "SELECT id, filename, uploaded_at, review_status "
            "FROM resume_upload_reviews ORDER BY uploaded_at DESC"
        ).fetchall()
        upload_rows = [dict(row) for row in rows]
    except Exception:
        pass

    from job_finder.web.resume_style_guide import load_style_guide
    style_guide = load_style_guide()

    profile_mtime = 0
    try:
        profile_mtime = os.path.getmtime(_PROFILE_PATH)
    except OSError:
        pass

    return {
        "resume_preferences": resume_preferences,
        "uploads": upload_rows,
        "style_guide": style_guide,
        "profile_mtime": profile_mtime,
    }

@profile_bp.route("/", strict_slashes=False)
def index():
    """Profile Editor -- display experience_profile.json in editable form."""
    profile = load_profile(_PROFILE_PATH)
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
                current_mtime = os.path.getmtime(_PROFILE_PATH)
                if abs(float(submitted_mtime) - current_mtime) > 0.01:
                    return "Profile was modified externally. Reload the page and try again.", 409
            except OSError:
                pass  # File doesn't exist yet -- no conflict possible

        save_profile(data, _PROFILE_PATH, force=True)
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

@profile_bp.route("/import", methods=["POST"], strict_slashes=False)
def import_markdown():
    """Accept .md file upload, extract structured profile via Claude Opus."""
    uploaded = request.files.get("markdown_file")
    if uploaded is None or uploaded.filename == "":
        flash("No file uploaded. Please select a .md file.", "error")
        return redirect(url_for("profile.index"))

    try:
        markdown_text = uploaded.read().decode("utf-8")
    except UnicodeDecodeError:
        flash("Could not read file as UTF-8 text. Please upload a .md file.", "error")
        return redirect(url_for("profile.index"))

    extracted = extract_profile_from_markdown(markdown_text)

    if "error" in extracted and not extracted.get("positions") and not extracted.get("skills"):
        flash(f"Extraction failed: {extracted['error']}", "error")
        return redirect(url_for("profile.index"))

    warnings = validate_profile(extracted)
    all_skills = _get_all_skills(extracted)
    extras = _load_profile_page_extras()

    return render_template(
        "profile/index.html",
        profile=extracted,
        warnings=warnings,
        all_skills=all_skills,
        import_success=True,
        **extras,
    )

@profile_bp.route("/reorder-positions", methods=["POST"], strict_slashes=False)
def reorder_positions():
    """HTMX endpoint: reorder positions by new index list and save."""
    try:
        indices = request.json if request.is_json else json.loads(request.form.get("indices", "[]"))
        profile = load_profile(_PROFILE_PATH)
        positions = profile.get("positions", [])

        if isinstance(indices, list) and all(isinstance(i, int) for i in indices):
            reordered = [positions[i] for i in indices if 0 <= i < len(positions)]
            # If not all indices present, fall back to original order
            if len(reordered) == len(positions):
                profile["positions"] = reordered
                save_profile(profile, _PROFILE_PATH, force=True)

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
        new_order = request.json if request.is_json else json.loads(request.form.get("skills", "[]"))
        profile = load_profile(_PROFILE_PATH)

        if isinstance(new_order, list):
            profile["skills"] = [s for s in new_order if s]
            save_profile(profile, _PROFILE_PATH, force=True)

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
