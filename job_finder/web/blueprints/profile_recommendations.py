"""Profile recommendations blueprint -- AI-powered profile improvement suggestions.

Routes:
    GET  /profile/recommendation        -- Haiku-generated fix guidance for a single warning
    POST /profile/recommendations-all   -- Batch Haiku recommendations for all warnings
    POST /profile/apply-fix             -- Apply a structured fix (add_skill, update_field)
"""

import json
import logging
import shutil

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    url_for,
)

from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude
from job_finder.web.db_helpers import get_db
from job_finder.web.profile_schema import load_profile, save_profile, validate_profile

logger = logging.getLogger(__name__)

profile_recs_bp = Blueprint("profile_recommendations", __name__, url_prefix="/profile")

_PROFILE_PATH = "experience_profile.json"

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
    "skills",  # Top-level skills list
    "resume_preferences.summary_style",
    "resume_preferences.emphasis",
}


@profile_recs_bp.route("/recommendation", strict_slashes=False)
def recommendation():
    """GET /profile/recommendation -- Haiku-generated fix guidance for a single warning.

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
        model = config.get("scoring", {}).get("models", {}).get("haiku", DEFAULT_MODEL_HAIKU)

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


@profile_recs_bp.route("/recommendations-all", methods=["POST"], strict_slashes=False)
def recommendations_all():
    """POST /profile/recommendations-all -- Batch Haiku recommendations for all current warnings.

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
        model = config.get("scoring", {}).get("models", {}).get("haiku", DEFAULT_MODEL_HAIKU)

        system = (
            "You are a profile improvement advisor. Given a set of profile validation warnings "
            "and the relevant profile data, suggest how to fix each warning. Return structured "
            "actions where possible (add_skill to add a missing skill, update_field to change a "
            "profile field). For complex fixes that need human judgment, return guidance text only "
            "with an empty actions array. Be specific and actionable. "
            "Return one recommendation per warning in the same order."
        )

        warnings_text = "\n".join(
            f"- Field: {w['field']}, Message: {w['message']}" for w in warnings
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


@profile_recs_bp.route("/apply-fix", methods=["POST"], strict_slashes=False)
def apply_fix():
    """POST /profile/apply-fix -- Apply a structured one-click fix to the profile.

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
        return (
            f"Invalid action type: {action_type!r}. Allowed: {', '.join(_SAFE_ACTION_TYPES)}",
            400,
        )

    # Validate editable field for update_field actions
    if action_type == "update_field" and field not in _SAFE_EDITABLE_FIELDS:
        return (
            f"Field not editable: {field!r}. Allowed fields: {', '.join(sorted(_SAFE_EDITABLE_FIELDS))}",
            400,
        )

    try:
        profile = load_profile(_PROFILE_PATH)

        # Backup profile before modification -- EAFP pattern avoids TOCTOU race.
        try:
            shutil.copy2(_PROFILE_PATH, _PROFILE_PATH + ".bak")
        except FileNotFoundError:
            pass  # Profile doesn't exist yet -- backup not needed

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
