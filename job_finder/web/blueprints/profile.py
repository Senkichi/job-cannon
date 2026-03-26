"""Profile blueprint — Profile Editor routes.

Routes:
    GET  /profile            -- Load profile, run validation, render editor
    POST /profile/save       -- Accept JSON body, save profile to disk
    POST /profile/import     -- Accept .md file upload, extract via Opus
    POST /profile/upload-pdf -- Accept PDF file upload, extract text, archive
    POST /profile/reorder-positions -- HTMX: reorder positions and save
    POST /profile/reorder-skills    -- HTMX: reorder top-level skills and save
    GET  /profile/recommendation    -- Haiku-generated fix guidance for a single warning
    POST /profile/recommendations-all -- Batch Haiku recommendations for all warnings
    POST /profile/apply-fix         -- Apply a structured fix (add_skill, update_field)
"""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.web.activity_tracker import (
    log_activity,
    ACTION_UPLOAD_RESUME_PDF,
    ACTION_CONFLICT_REVIEW,
    ACTION_SAVE_CONFLICTS,
    ACTION_EXTRACT_STYLE,
)
from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude
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

CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "profile_version": {"type": "string"},
                    "pdf_version": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "position_company": {"type": "string"},
                },
                "required": ["type", "pdf_version", "suggestion"],
            },
        }
    },
    "required": ["conflicts"],
    "additionalProperties": False,
}

RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "guidance": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "value": {"type": "string"},
                                "field": {"type": "string"},
                            },
                            "required": ["type", "value"],
                        },
                    },
                },
                "required": ["field", "guidance", "actions"],
            },
        }
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}

# Safety allowlists for apply-fix endpoint
_SAFE_ACTION_TYPES = {"add_skill", "update_field"}
_SAFE_EDITABLE_FIELDS = {
    "skills",                          # Top-level skills list
    "resume_preferences.summary_style",
    "resume_preferences.emphasis",
}


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
    """Profile Editor — display experience_profile.json in editable form."""
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
                pass  # File doesn't exist yet — no conflict possible

        save_profile(data, _PROFILE_PATH, force=True)
        flash("Profile saved successfully.", "success")

        # HTMX requests get a lightweight redirect header; normal forms get a redirect
        if request.headers.get("HX-Request"):
            response = current_app.response_class("", status=204)
            response.headers["HX-Redirect"] = url_for("profile.index")
            return response

        return redirect(url_for("profile.index"))

    except (ValueError, KeyError) as exc:
        flash(f"Error saving profile: {exc}", "error")
        return redirect(url_for("profile.index"))


@profile_bp.route("/import", methods=["POST"], strict_slashes=False)
def import_markdown():
    """Accept .md file upload, extract structured profile via Claude Opus."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        flash(
            "ANTHROPIC_API_KEY is not set. "
            "Set it in your environment to use Markdown import.",
            "error",
        )
        return redirect(url_for("profile.index"))

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


@profile_bp.route("/upload-pdf", methods=["POST"], strict_slashes=False)
def upload_pdf():
    """Accept a PDF file upload, extract text, reject scanned PDFs, archive file, insert DB row."""
    uploaded = request.files.get("pdf_file")
    if uploaded is None or uploaded.filename == "":
        flash("No file uploaded. Please select a PDF file.", "error")
        return redirect(url_for("profile.index"))

    pdf_bytes = uploaded.read()

    # Lazy import: PyMuPDF (fitz) is optional — app should start even if not installed.
    try:
        import fitz  # noqa: PLC0415
    except ImportError:
        flash("PyMuPDF is not installed. PDF upload is unavailable.", "error")
        return redirect(url_for("profile.index"))

    # Extract text with PyMuPDF
    try:
        doc = fitz.open("pdf", pdf_bytes)
        try:
            text = "".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception:
        flash(
            "Could not read this PDF. Please ensure it is a valid, non-corrupted PDF file.",
            "error",
        )
        return redirect(url_for("profile.index"))

    # Scanned/image-only guard
    if len(text.strip()) < 200:
        flash(
            "This PDF appears to be scanned/image-only. Please upload a text-based PDF.",
            "error",
        )
        return redirect(url_for("profile.index"))

    # Archive raw file
    upload_dir = Path(_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = upload_dir / f"{timestamp}_{uploaded.filename}"
    archive_path.write_bytes(pdf_bytes)

    # Insert into DB
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = current_app.config.get("DB_PATH", "jobs.db")
    conn = get_db(db_path)
    cursor = conn.execute(
        "INSERT INTO resume_upload_reviews (filename, raw_text, uploaded_at, review_status) "
        "VALUES (?, ?, ?, 'pending')",
        (uploaded.filename, text, now_iso),
    )
    conn.commit()
    upload_id = cursor.lastrowid

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_UPLOAD_RESUME_PDF,
            entity_id=uploaded.filename,
            metadata={"upload_id": upload_id, "filename": uploaded.filename},
        )
    except Exception:
        pass

    return redirect(url_for("profile.conflict_review", upload_id=upload_id))


def _compare_conflicts(raw_text: str, profile: dict, conn, config: dict) -> list:
    """Call Haiku to compare PDF resume text against the profile and return conflict list.

    Args:
        raw_text: Raw text extracted from the PDF.
        profile: Current experience profile dict.
        conn: Open SQLite connection for cost recording.
        config: Application config dict (reads scoring.models.haiku).

    Returns:
        List of conflict dicts matching CONFLICT_SCHEMA["properties"]["conflicts"]["items"].
        Returns [] on error.
    """
    try:
        client = anthropic.Anthropic()
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("haiku", DEFAULT_MODEL_HAIKU)
        )

        system = (
            "You are a resume conflict analyzer. Compare a PDF resume against a structured "
            "experience profile and identify meaningful differences. Focus on: "
            "(1) achievements in the PDF that differ from or extend the profile for matching positions, "
            "(2) positions in the PDF not present in the profile, "
            "(3) skills in the PDF not in the profile's top-level skills list. "
            "Skip trivial formatting differences (date formatting, capitalization, company name casing). "
            "Return at most 20 conflicts. Each conflict needs type, pdf_version, and suggestion."
        )

        profile_json = json.dumps(profile, indent=2)
        user_message = (
            f"## Current Experience Profile (JSON)\n\n"
            f"```json\n{profile_json}\n```\n\n"
            f"---\n\n"
            f"## PDF Resume Text\n\n"
            f"{raw_text[:8000]}\n\n"
            f"Identify conflicts (differences/additions) between the PDF and the profile."
        )

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=CONFLICT_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_conflict_review",
            config=config,
            max_tokens=2048,
        )
        return result.get("conflicts", [])

    except Exception as e:
        logger.warning("_compare_conflicts: failed to compare conflicts: %s", e)
        return []


@profile_bp.route("/review/<int:upload_id>", strict_slashes=False)
def conflict_review(upload_id: int):
    """Conflict review page: Haiku compares PDF text against profile.

    GET /profile/review/<upload_id>
    """
    db_path = current_app.config.get("DB_PATH", "jobs.db")
    conn = get_db(db_path)

    row = conn.execute(
        "SELECT * FROM resume_upload_reviews WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return "Upload not found", 404

    row = dict(row)

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_CONFLICT_REVIEW,
            entity_id=str(upload_id),
            metadata={"upload_id": upload_id},
        )
    except Exception:
        pass

    profile = load_profile(_PROFILE_PATH)
    config = current_app.config.get("JF_CONFIG", {})

    conflicts = _compare_conflicts(row["raw_text"], profile, conn, config)

    return render_template(
        "profile/conflict_review.html",
        conflicts=conflicts,
        upload_id=upload_id,
        upload=row,
    )


@profile_bp.route("/save-conflicts/<int:upload_id>", methods=["POST"], strict_slashes=False)
def save_conflicts(upload_id: int):
    """Apply accepted conflict decisions to experience_profile.json.

    POST /profile/save-conflicts/<upload_id>
    Body JSON: {
        "decisions": [{"conflict_index": 0, "action": "accept"|"edit"|"skip", "custom_text": "..."}],
        "conflicts": [...]  -- original conflicts list from the review page
    }
    """
    db_path = current_app.config.get("DB_PATH", "jobs.db")
    conn = get_db(db_path)

    row = conn.execute(
        "SELECT id FROM resume_upload_reviews WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return "Upload not found", 404

    data = request.get_json(force=True, silent=True) or {}
    decisions = data.get("decisions", [])
    conflicts = data.get("conflicts", [])

    profile = load_profile(_PROFILE_PATH)

    for decision in decisions:
        action = decision.get("action", "skip")
        if action == "skip":
            continue

        conflict_index = decision.get("conflict_index", 0)
        if conflict_index >= len(conflicts):
            continue

        conflict = conflicts[conflict_index]
        conflict_type = conflict.get("type", "")
        apply_text = decision.get("custom_text") if action == "edit" else conflict.get("pdf_version", "")

        if not apply_text:
            continue

        if conflict_type == "new_skill":
            if "skills" not in profile:
                profile["skills"] = []
            if apply_text not in profile["skills"]:
                profile["skills"].append(apply_text)

        elif conflict_type == "new_position":
            if "positions" not in profile:
                profile["positions"] = []
            profile["positions"].append({
                "title": apply_text[:100],
                "company": conflict.get("position_company", ""),
                "start_date": "",
                "end_date": None,
                "achievements": [],
                "skills": [],
            })

        elif conflict_type == "achievement_diff":
            target_company = conflict.get("position_company", "")
            for pos in profile.get("positions", []):
                if pos.get("company", "").lower() == target_company.lower():
                    if "achievements" not in pos:
                        pos["achievements"] = []
                    if apply_text not in pos["achievements"]:
                        pos["achievements"].append(apply_text)
                    break

    save_profile(profile, _PROFILE_PATH, force=True)

    conn.execute(
        "UPDATE resume_upload_reviews SET review_status='reviewed' WHERE id=?",
        (upload_id,),
    )
    conn.commit()

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_SAVE_CONFLICTS,
            entity_id=str(upload_id),
            metadata={"decisions_count": len(decisions)},
        )
    except Exception:
        pass

    flash("Changes saved successfully.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/extract-style/<int:upload_id>", methods=["POST"], strict_slashes=False)
def extract_style(upload_id: int):
    """Trigger Sonnet style extraction from an uploaded resume PDF.

    POST /profile/extract-style/<upload_id>
    """
    db_path = current_app.config.get("DB_PATH", "jobs.db")
    conn = get_db(db_path)

    row = conn.execute(
        "SELECT * FROM resume_upload_reviews WHERE id = ?", (upload_id,)
    ).fetchone()
    if row is None:
        return "Upload not found", 404

    row = dict(row)
    config = current_app.config.get("JF_CONFIG", {})

    from job_finder.web.resume_style_guide import extract_style_guide, load_style_guide, save_style_guide

    existing_guide = load_style_guide()
    new_guide = extract_style_guide(row["raw_text"], existing_guide, conn, config)

    if new_guide is None:
        flash("Style extraction failed. Check your Anthropic API key and credit balance.", "error")
        return redirect(url_for("profile.index"))

    save_style_guide(new_guide)

    conn.execute(
        "UPDATE resume_upload_reviews SET review_status='style_extracted' WHERE id=?",
        (upload_id,),
    )
    conn.commit()

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_EXTRACT_STYLE,
            entity_id=str(upload_id),
            metadata={"upload_id": upload_id},
        )
    except Exception:
        pass

    flash("Style guide extracted successfully.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/save-style-guide", methods=["POST"], strict_slashes=False)
def save_style_guide_route():
    """Save manually edited style guide from profile page.

    POST /profile/save-style-guide
    Body JSON: style guide fields dict
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        flash("No style guide data received.", "error")
        return redirect(url_for("profile.index"))

    # Convert section_order string to list if needed
    if "section_order" in data and isinstance(data["section_order"], str):
        data["section_order"] = [s.strip() for s in data["section_order"].split(",") if s.strip()]

    from job_finder.web.resume_style_guide import save_style_guide
    save_style_guide(data)

    flash("Style guide updated.", "success")
    return redirect(url_for("profile.index"))


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


@profile_bp.route("/recommendation", strict_slashes=False)
def recommendation():
    """GET /profile/recommendation — Haiku-generated fix guidance for a single warning.

    Query params:
        field (str): Warning field identifier.
        message (str): Warning message text.

    Returns:
        Rendered _recommendation.html fragment.
    """
    field = request.args.get("field", "")
    message = request.args.get("message", "")

    if not field or not message:
        return render_template(
            "profile/_recommendation.html",
            field=field,
            guidance="Missing warning context.",
            actions=[],
            error=True,
        )

    try:
        profile = load_profile(_PROFILE_PATH)
        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config.get("DB_PATH", "jobs.db")
        conn = get_db(db_path)
        client = anthropic.Anthropic()
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("haiku", DEFAULT_MODEL_HAIKU)
        )

        system = (
            "You are a profile improvement advisor. Given a profile validation warning "
            "and the relevant profile data, suggest how to fix the warning. Return structured "
            "actions where possible (add_skill to add a missing skill, update_field to change a "
            "profile field). For complex fixes that need human judgment, return guidance text only "
            "with an empty actions array. Be specific and actionable."
        )

        # Extract relevant profile section for context
        profile_json = json.dumps(profile, indent=2)
        user_message = (
            f"## Profile Validation Warning\n\n"
            f"**Field:** {field}\n"
            f"**Message:** {message}\n\n"
            f"---\n\n"
            f"## Current Profile\n\n"
            f"```json\n{profile_json[:4000]}\n```\n\n"
            f"Suggest how to fix this warning."
        )

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RECOMMENDATION_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="profile_recommendation",
            config=config,
            max_tokens=512,
        )

        recs = result.get("recommendations", [])
        rec = recs[0] if recs else {}

        return render_template(
            "profile/_recommendation.html",
            field=rec.get("field", field),
            guidance=rec.get("guidance", "No recommendation available."),
            actions=rec.get("actions", []),
            error=False,
        )

    except Exception as exc:
        logger.warning("recommendation route: failed to generate recommendation: %s", exc)
        return render_template(
            "profile/_recommendation.html",
            field=field,
            guidance="Could not generate recommendation. Please try again.",
            actions=[],
            error=True,
        )


@profile_bp.route("/recommendations-all", methods=["POST"], strict_slashes=False)
def recommendations_all():
    """POST /profile/recommendations-all — Batch Haiku recommendations for all current warnings.

    Returns:
        Rendered _recommendations_all.html fragment.
    """
    try:
        profile = load_profile(_PROFILE_PATH)
        warnings = validate_profile(profile)

        if not warnings:
            return render_template(
                "profile/_recommendations_all.html",
                recommendations=[],
                warnings=[],
            )

        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config.get("DB_PATH", "jobs.db")
        conn = get_db(db_path)
        client = anthropic.Anthropic()
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("haiku", DEFAULT_MODEL_HAIKU)
        )

        system = (
            "You are a profile improvement advisor. Given a set of profile validation warnings "
            "and the relevant profile data, suggest how to fix each warning. Return structured "
            "actions where possible (add_skill to add a missing skill, update_field to change a "
            "profile field). For complex fixes that need human judgment, return guidance text only "
            "with an empty actions array. Be specific and actionable. "
            "Return one recommendation per warning in the same order."
        )

        warnings_text = "\n".join(
            f"- Field: {w['field']}, Message: {w['message']}"
            for w in warnings
        )
        profile_json = json.dumps(profile, indent=2)
        user_message = (
            f"## Profile Validation Warnings\n\n"
            f"{warnings_text}\n\n"
            f"---\n\n"
            f"## Current Profile\n\n"
            f"```json\n{profile_json[:6000]}\n```\n\n"
            f"Provide a recommendation for each warning above."
        )

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RECOMMENDATION_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="profile_recommendations_batch",
            config=config,
            max_tokens=1024,
        )

        recommendations = result.get("recommendations", [])

        return render_template(
            "profile/_recommendations_all.html",
            recommendations=recommendations,
            warnings=warnings,
        )

    except Exception as exc:
        logger.warning("recommendations_all route: failed to generate recommendations: %s", exc)
        return render_template(
            "profile/_recommendations_all.html",
            recommendations=[],
            warnings=[],
        )


@profile_bp.route("/apply-fix", methods=["POST"], strict_slashes=False)
def apply_fix():
    """POST /profile/apply-fix — Apply a structured one-click fix to the profile.

    Form fields:
        action_type (str): "add_skill" or "update_field"
        field (str): Target field identifier
        value (str): Value to apply

    Returns:
        HX-Redirect to profile page (200) on success, or 400 on validation failure.
    """
    action_type = request.form.get("action_type", "")
    field = request.form.get("field", "")
    value = request.form.get("value", "")

    # Validate action type
    if action_type not in _SAFE_ACTION_TYPES:
        return f"Invalid action type: {action_type!r}. Allowed: {', '.join(_SAFE_ACTION_TYPES)}", 400

    # Validate editable field for update_field actions
    if action_type == "update_field" and field not in _SAFE_EDITABLE_FIELDS:
        return f"Field not editable: {field!r}. Allowed fields: {', '.join(sorted(_SAFE_EDITABLE_FIELDS))}", 400

    try:
        profile = load_profile(_PROFILE_PATH)

        # Backup profile before modification — EAFP pattern avoids TOCTOU race.
        try:
            shutil.copy2(_PROFILE_PATH, _PROFILE_PATH + ".bak")
        except FileNotFoundError:
            pass  # Profile doesn't exist yet — backup not needed

        # Apply the fix
        if action_type == "add_skill":
            if "skills" not in profile:
                profile["skills"] = []
            if value and value not in profile["skills"]:
                profile["skills"].append(value)

        elif action_type == "update_field":
            if field == "skills":
                # Treat comma-separated values as a list; single value appended
                if "," in value:
                    profile["skills"] = [s.strip() for s in value.split(",") if s.strip()]
                else:
                    if "skills" not in profile:
                        profile["skills"] = []
                    if value and value not in profile["skills"]:
                        profile["skills"].append(value)
            elif field.startswith("resume_preferences."):
                parts = field.split(".", 1)
                subkey = parts[1]
                if "resume_preferences" not in profile:
                    profile["resume_preferences"] = {}
                profile["resume_preferences"][subkey] = value

        save_profile(profile, _PROFILE_PATH, force=True)

        # Return HX-Redirect to refresh the page with updated validation state
        response = current_app.response_class("", status=200)
        response.headers["HX-Redirect"] = url_for("profile.index")
        return response

    except Exception as exc:
        logger.warning("apply_fix route: failed to apply fix: %s", exc)
        return f"Failed to apply fix: {exc}", 500
