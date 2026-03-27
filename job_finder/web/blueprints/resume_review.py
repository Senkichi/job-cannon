"""Resume review blueprint -- PDF upload, conflict review, style extraction routes.

Routes:
    POST /profile/upload-pdf                    -- Accept PDF upload, extract text, archive
    GET  /profile/review/<upload_id>            -- Haiku conflict review of uploaded PDF vs profile
    POST /profile/save-conflicts/<upload_id>    -- Apply accepted conflict decisions to profile
    POST /profile/extract-style/<upload_id>     -- Sonnet style extraction from uploaded PDF
    POST /profile/save-style-guide              -- Save manually edited style guide
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import anthropic
try:
    import fitz  # PyMuPDF -- optional; guarded so app starts without it
except ImportError:
    fitz = None  # type: ignore[assignment]

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
from job_finder.web.model_provider import call_model
from job_finder.web.db_helpers import get_db
from job_finder.web.profile_schema import load_profile, save_profile

logger = logging.getLogger(__name__)

resume_review_bp = Blueprint("resume_review", __name__, url_prefix="/profile")

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


@resume_review_bp.route("/upload-pdf", methods=["POST"], strict_slashes=False)
def upload_pdf():
    """Accept a PDF file upload, extract text, reject scanned PDFs, archive file, insert DB row."""
    uploaded = request.files.get("pdf_file")
    if uploaded is None or uploaded.filename == "":
        flash("No file uploaded. Please select a PDF file.", "error")
        return redirect(url_for("profile.index"))

    pdf_bytes = uploaded.read()

    # Guard against fitz not being installed (ImportError at module level falls back to None).
    if fitz is None:
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

    return redirect(url_for("resume_review.conflict_review", upload_id=upload_id))


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

        result_obj = call_model(
            tier="haiku",
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=CONFLICT_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_conflict_review",
            config=config,
            max_tokens=2048,
            client=client,
        )
        return result_obj.data.get("conflicts", [])

    except Exception as e:
        logger.warning("_compare_conflicts: failed to compare conflicts: %s", e)
        return []


@resume_review_bp.route("/review/<int:upload_id>", strict_slashes=False)
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


@resume_review_bp.route("/save-conflicts/<int:upload_id>", methods=["POST"], strict_slashes=False)
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


@resume_review_bp.route("/extract-style/<int:upload_id>", methods=["POST"], strict_slashes=False)
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


@resume_review_bp.route("/save-style-guide", methods=["POST"], strict_slashes=False)
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
