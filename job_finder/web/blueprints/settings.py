"""Settings blueprint — Settings page routes.

Routes:
    GET  /settings       -- Load config.yaml, render settings form
    POST /settings/save  -- Read form data, write back to config.yaml, update running config
    POST /settings/preview-guidelines-merge -- Preview field-by-field diff before applying
    POST /settings/apply-guidelines-merge   -- Apply stashed merged result without second API call
"""

import json
import logging
import os
from pathlib import Path

import anthropic
import yaml
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.config import (
    DEFAULT_HAIKU_THRESHOLD,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    DEFAULT_MONTHLY_BUDGET_USD,
    DEFAULT_MULTI_VERSION_THRESHOLD,
    DEFAULT_MODEL_SONNET,
    load_config,
)
from job_finder.web.drive_status import get_drive_status
from job_finder.web.resume_style_guide import (
    load_style_guide,
    migrate_style_guide,
    save_style_guide,
    _merge_guidelines_into_guide,
    STYLE_GUIDE_SCHEMA,
    _FIELD_LABELS,
)
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

_CONFIG_PATH = "config.yaml"


@settings_bp.route("/", strict_slashes=False)
def index():
    """Settings page — display config.yaml values in editable form."""
    try:
        config = load_config(_CONFIG_PATH)
    except FileNotFoundError:
        # Fall back to the in-memory config from app context
        config = current_app.config.get("JF_CONFIG", {})

    # Ensure ATS section has defaults for new installs
    if "ats" not in config:
        config = dict(config)
        config["ats"] = {
            "scan_enabled": True,
            "scan_days": "mon,wed",
            "scan_hour": 7,
        }

    drive_status = get_drive_status(config)

    config_mtime = 0
    try:
        config_mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        pass

    style_guide = load_style_guide()
    new_field_names = [
        "summary_formula", "skills_format", "bullet_formula", "bullet_counts",
        "confidentiality_rules", "typography_rules", "jd_mirroring_rules",
        "anti_patterns", "role_archetype",
    ]
    new_fields_present = sum(1 for f in new_field_names if style_guide.get(f))
    new_fields_available = len(new_field_names) - new_fields_present

    guidelines_text = ""
    try:
        guidelines_path = Path(__file__).resolve().parent.parent.parent / "docs" / "resume_generation_guidelines.md"
        guidelines_text = guidelines_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass

    return render_template(
        "settings/index.html",
        config=config,
        drive_status=drive_status,
        config_mtime=config_mtime,
        style_guide=style_guide,
        new_fields_present=new_fields_present,
        new_fields_available=new_fields_available,
        guidelines_text=guidelines_text,
    )


@settings_bp.route("/save", methods=["POST"], strict_slashes=False)
def save():
    """Save settings form data to config.yaml and update running app config."""
    try:
        # Load existing config first so we preserve keys not in the form
        try:
            existing = load_config(_CONFIG_PATH)
        except FileNotFoundError:
            existing = {}

        # Stale-form detection: reject if config was modified since page load
        submitted_mtime = request.form.get("_config_mtime", "")
        if submitted_mtime:
            try:
                current_mtime = os.path.getmtime(_CONFIG_PATH)
                if abs(float(submitted_mtime) - current_mtime) > 0.01:
                    flash("Settings were modified externally. Page reloaded with latest values.", "warning")
                    return redirect(url_for("settings.index"))
            except (OSError, ValueError):
                pass

        form_config = _parse_form_to_config(request.form)
        config = _deep_merge(existing, form_config)

        # Guard: block saves that wipe critical profile fields
        existing_profile = existing.get("profile", {})
        merged_profile = config.get("profile", {})
        existing_titles = existing_profile.get("target_titles", [])
        merged_titles = merged_profile.get("target_titles", [])
        existing_skills = existing_profile.get("skills", [])
        merged_skills = merged_profile.get("skills", [])

        if (existing_titles and not merged_titles) or (existing_skills and not merged_skills):
            wiped = []
            if existing_titles and not merged_titles:
                wiped.append(f"target_titles ({len(existing_titles)} items)")
            if existing_skills and not merged_skills:
                wiped.append(f"skills ({len(existing_skills)} items)")
            logger.debug("settings save: blocked wipe of %s", ", ".join(wiped))
            flash(f"Save blocked: would wipe {', '.join(wiped)}. Check form and try again.", "error")
            return redirect(url_for("settings.index"))

        _write_config(config, _CONFIG_PATH)

        # Update running app config so changes take effect without restart
        current_app.config["JF_CONFIG"] = config
        if "db" in config:
            current_app.config["DB_PATH"] = config["db"].get("path", current_app.config.get("DB_PATH"))

        flash("Settings saved successfully.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Error saving settings: {exc}", "error")

    return redirect(url_for("settings.index"))


@settings_bp.route("/migrate-style-guide", methods=["POST"], strict_slashes=False)
def migrate_style_guide_route():
    """Run Sonnet-powered migration of the style guide to populate new guideline fields."""
    try:
        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config["DB_PATH"]
        conn = get_db(db_path)
        result = migrate_style_guide(config, conn)
        if result:
            field_count = sum(1 for v in result.values() if v)
            return (
                f'<div id="style-guide-migrate-section" class="text-xs text-emerald-400">'
                f'Style guide migrated. {field_count} fields now populated.'
                f'</div>',
                200,
            )
        else:
            return (
                '<div id="style-guide-migrate-section" class="text-xs text-red-400">'
                'Migration failed: Sonnet returned no result. Check logs for details.'
                '<button type="button" hx-post="/settings/migrate-style-guide" '
                'hx-target="#style-guide-migrate-section" hx-swap="outerHTML" '
                'class="ml-2 text-xs text-violet-400 hover:text-violet-300">Retry</button>'
                '</div>',
                200,
            )
    except Exception as exc:
        logger.warning("migrate_style_guide_route: %s", exc)
        return (
            f'<div id="style-guide-migrate-section" class="text-xs text-red-400">'
            f'Migration failed: {exc}. Check logs for details.'
            f'<button type="button" hx-post="/settings/migrate-style-guide" '
            f'hx-target="#style-guide-migrate-section" hx-swap="outerHTML" '
            f'class="ml-2 text-xs text-violet-400 hover:text-violet-300">Retry</button>'
            f'</div>',
            200,
        )


@settings_bp.route("/preview-guidelines-merge", methods=["POST"], strict_slashes=False)
def preview_guidelines_merge():
    """Preview a field-by-field diff of merging updated guidelines into the style guide.

    Calls Sonnet once with mode="merge_updates" and returns a diff fragment
    with the merged result stashed as a hidden input. Applying uses the stash
    without a second API call.
    """
    try:
        guidelines_text = request.form.get("guidelines_text", "").strip()
        if not guidelines_text:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Please enter guidelines text."
                "</div>",
                200,
            )

        existing_guide = load_style_guide()
        config = current_app.config.get("JF_CONFIG", {})
        db_path = current_app.config["DB_PATH"]
        conn = get_db(db_path)
        client = anthropic.Anthropic()
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        result = _merge_guidelines_into_guide(
            guidelines_text=guidelines_text,
            existing_guide=existing_guide,
            client=client,
            model=model,
            conn=conn,
            config=config,
            mode="merge_updates",
        )

        if result is None:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Preview failed: Sonnet returned no result. Check logs for details."
                "</div>",
                200,
            )

        # Build field-by-field diff HTML
        diff_rows = []
        for field in STYLE_GUIDE_SCHEMA["properties"]:
            old_val = existing_guide.get(field, "")
            new_val = result.get(field, "")
            label = _FIELD_LABELS.get(field, field)

            # Stringify lists/dicts for comparison
            old_str = json.dumps(old_val, ensure_ascii=False) if isinstance(old_val, (list, dict)) else str(old_val) if old_val else ""
            new_str = json.dumps(new_val, ensure_ascii=False) if isinstance(new_val, (list, dict)) else str(new_val) if new_val else ""

            if old_str == new_str:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-500 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-slate-500 text-xs italic">No change</span>'
                    f"</div>"
                )
            elif not old_str and new_str:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-300 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-slate-500 italic mr-2">(empty)</span>'
                    f'<span class="text-slate-400 mr-2">&rarr;</span>'
                    f'<span class="text-emerald-400">{new_str}</span>'
                    f"</div>"
                )
            else:
                diff_rows.append(
                    f'<div class="flex items-start gap-2 py-1 border-b border-slate-700/50">'
                    f'<span class="text-slate-300 w-40 flex-shrink-0 font-medium">{label}</span>'
                    f'<span class="text-red-400 line-through mr-2">{old_str}</span>'
                    f'<span class="text-slate-400 mr-2">&rarr;</span>'
                    f'<span class="text-emerald-400">{new_str}</span>'
                    f"</div>"
                )

        merged_json = json.dumps(result, ensure_ascii=False)
        import html as html_module
        escaped_json = html_module.escape(merged_json, quote=True)

        diff_html = "\n".join(diff_rows)
        apply_button = (
            f'<div class="mt-4 flex items-center gap-3">'
            f'<input type="hidden" name="merged_guide_json" value="{escaped_json}">'
            f'<input type="hidden" name="guidelines_text" id="stashed-guidelines-text" value="{html_module.escape(guidelines_text, quote=True)}">'
            f'<button type="button"'
            f' hx-post="/settings/apply-guidelines-merge"'
            f' hx-target="#guidelines-diff-container"'
            f' hx-swap="innerHTML"'
            f' hx-include="[name=merged_guide_json],[name=guidelines_text]"'
            f' hx-disabled-elt="this"'
            f' class="px-3 py-1 bg-emerald-700 hover:bg-emerald-600 text-white rounded text-sm transition-colors">'
            f"Apply Changes"
            f"</button>"
            f'<span class="text-xs text-slate-500">No extra API cost — uses the cached preview result.</span>'
            f"</div>"
        )

        fragment = (
            f'<div id="guidelines-diff-container" class="mt-2">'
            f'<h4 class="text-xs font-semibold text-slate-300 mb-2">Field-by-field preview</h4>'
            f'<div class="text-xs font-mono space-y-0">{diff_html}</div>'
            f"{apply_button}"
            f"</div>"
        )

        return fragment, 200

    except Exception as exc:
        logger.warning("preview_guidelines_merge: %s", exc, exc_info=True)
        return (
            f'<div id="guidelines-diff-container" class="text-xs text-red-400">'
            f"Preview failed: {exc}. Check logs for details."
            f"</div>",
            200,
        )


@settings_bp.route("/apply-guidelines-merge", methods=["POST"], strict_slashes=False)
def apply_guidelines_merge():
    """Apply the stashed merged style guide result (from preview) without a second Sonnet call."""
    try:
        merged_json = request.form.get("merged_guide_json", "").strip()
        if not merged_json:
            return (
                '<div id="guidelines-diff-container" class="text-xs text-red-400">'
                "Apply failed: no merged guide data found. Please preview again."
                "</div>",
                200,
            )

        merged_guide = json.loads(merged_json)

        save_style_guide(merged_guide)

        # Also persist the guidelines text to docs/resume_generation_guidelines.md if provided
        guidelines_text = request.form.get("guidelines_text", "").strip()
        if guidelines_text:
            guidelines_path = Path(__file__).resolve().parent.parent.parent / "docs" / "resume_generation_guidelines.md"
            guidelines_path.write_text(guidelines_text, encoding="utf-8")
            logger.info("apply_guidelines_merge: updated resume_generation_guidelines.md")

        logger.info("apply_guidelines_merge: saved style guide with %d fields", len(merged_guide))
        return (
            '<div id="guidelines-diff-container" class="text-xs text-emerald-400">'
            "Guidelines applied successfully. Style guide updated."
            "</div>",
            200,
        )

    except json.JSONDecodeError as exc:
        logger.warning("apply_guidelines_merge: invalid JSON: %s", exc)
        return (
            '<div id="guidelines-diff-container" class="text-xs text-red-400">'
            "Apply failed: invalid merged guide data. Please preview again."
            "</div>",
            200,
        )
    except Exception as exc:
        logger.warning("apply_guidelines_merge: %s", exc, exc_info=True)
        return (
            f'<div id="guidelines-diff-container" class="text-xs text-red-400">'
            f"Apply failed: {exc}. Check logs for details."
            f"</div>",
            200,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into *base*, returning a new dict.

    - Dict values are merged recursively.
    - All other values in *overrides* replace the corresponding *base* value.
    - Keys in *base* that are absent from *overrides* are preserved.
    """
    merged = dict(base)
    for key, value in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_form_to_config(form) -> dict:
    """Convert flat form fields back to nested config dict.

    Only includes fields that were actually submitted in the form.
    Fields absent from the form are omitted so _deep_merge preserves
    existing config values — preventing blank overwrites when a field
    has no corresponding HTML form element or the form is incomplete.

    Checkbox fields use hidden companion inputs in the template so that
    unchecked = empty string (present in form) vs not rendered = absent.
    """

    def lines_to_list(text: str) -> list:
        """Split textarea lines into a list, stripping blanks."""
        return [line.strip() for line in text.splitlines() if line.strip()]

    def safe_float(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def safe_int(value, default=0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _has(key):
        """Check if a form field was actually submitted."""
        return key in form

    config = {}

    # --- Profile ---
    profile = {}
    if _has("target_titles"):
        profile["target_titles"] = lines_to_list(form["target_titles"])
    if _has("target_locations"):
        profile["target_locations"] = lines_to_list(form["target_locations"])
    if _has("min_salary"):
        profile["min_salary"] = safe_int(form["min_salary"])
    if _has("industries"):
        profile["industries"] = lines_to_list(form["industries"])
    exclusions = {}
    if _has("exclusion_title_keywords"):
        exclusions["title_keywords"] = lines_to_list(form["exclusion_title_keywords"])
    if _has("exclusion_companies"):
        exclusions["companies"] = lines_to_list(form["exclusion_companies"])
    if exclusions:
        profile["exclusions"] = exclusions
    if _has("profile_skills"):
        profile["skills"] = lines_to_list(form["profile_skills"])
    if profile:
        config["profile"] = profile

    # --- Sources: Gmail ---
    gmail = {}
    if _has("gmail_enabled"):
        gmail["enabled"] = form["gmail_enabled"] == "on"
    if _has("gmail_lookback_days"):
        gmail["lookback_days"] = safe_int(form["gmail_lookback_days"], DEFAULT_LOOKBACK_DAYS)
    senders = {}
    for sender_key in ("linkedin_alerts", "linkedin_jobs", "glassdoor", "indeed", "ziprecruiter"):
        fk = f"gmail_sender_{sender_key}"
        if _has(fk):
            senders[sender_key] = form[fk]
    if senders:
        gmail["senders"] = senders
    if gmail:
        config.setdefault("sources", {})["gmail"] = gmail

    # --- Sources: SerpAPI ---
    serpapi = {}
    if _has("serpapi_enabled"):
        serpapi["enabled"] = form["serpapi_enabled"] == "on"
    if _has("serpapi_api_key"):
        serpapi["api_key"] = form["serpapi_api_key"]
    # Sentinel hidden input marks that the queries section was rendered;
    # if present we parse queries (possibly []), otherwise preserve existing.
    if _has("_serpapi_queries_present"):
        serpapi["queries"] = _parse_serpapi_queries(form)
    if serpapi:
        config.setdefault("sources", {})["serpapi"] = serpapi

    # --- Sources: JSearch ---
    jsearch = {}
    if _has("jsearch_enabled"):
        jsearch["enabled"] = form["jsearch_enabled"] == "on"
    if _has("jsearch_rapidapi_key"):
        jsearch["rapidapi_key"] = form["jsearch_rapidapi_key"]
    if jsearch:
        config.setdefault("sources", {})["jsearch"] = jsearch

    # --- Scoring ---
    scoring = {}
    weights = {}
    for wk in ("title_match", "seniority_alignment", "location_fit",
                "salary_range", "industry_relevance", "company_signals", "recency"):
        fk = f"weight_{wk}"
        if _has(fk):
            weights[wk] = safe_float(form[fk])
    if weights:
        scoring["weights"] = weights
    if _has("min_score_threshold"):
        scoring["min_score_threshold"] = safe_int(form["min_score_threshold"], DEFAULT_MIN_SCORE_THRESHOLD)
    if _has("monthly_budget_usd"):
        scoring["monthly_budget_usd"] = safe_float(form["monthly_budget_usd"], DEFAULT_MONTHLY_BUDGET_USD)
    if _has("haiku_threshold"):
        scoring["haiku_threshold"] = safe_int(form["haiku_threshold"], DEFAULT_HAIKU_THRESHOLD)
    models = {}
    if _has("model_haiku"):
        models["haiku"] = form["model_haiku"]
    if _has("model_sonnet"):
        models["sonnet"] = form["model_sonnet"]
    if models:
        scoring["models"] = models
    if _has("multi_version_threshold") and form["multi_version_threshold"]:
        scoring["multi_version_threshold"] = safe_int(form["multi_version_threshold"], DEFAULT_MULTI_VERSION_THRESHOLD)
    if scoring:
        config["scoring"] = scoring

    # --- Output ---
    output = {}
    if _has("output_default_format"):
        output["default_format"] = form["output_default_format"]
    if _has("output_markdown_path"):
        output["markdown_path"] = form["output_markdown_path"]
    if _has("output_max_results"):
        output["max_results"] = safe_int(form["output_max_results"], DEFAULT_MAX_RESULTS)
    if output:
        config["output"] = output

    # --- Database ---
    if _has("db_path"):
        config["db"] = {"path": form["db_path"]}

    # --- Drive ---
    drive = {}
    if _has("drive_folder_id"):
        drive["folder_id"] = form["drive_folder_id"]
    if _has("drive_convert_to_gdoc"):
        drive["convert_to_gdoc"] = form["drive_convert_to_gdoc"] in ("on", "true", True)
    if drive:
        config["drive"] = drive

    # --- Notifications (checkboxes with hidden companion inputs) ---
    notifications = {}
    if _has("notification_high_score"):
        notifications["high_score"] = form["notification_high_score"] == "on"
    if _has("notification_pipeline_change"):
        notifications["pipeline_change"] = form["notification_pipeline_change"] == "on"
    if _has("notification_budget_alert"):
        notifications["budget_alert"] = form["notification_budget_alert"] == "on"
    if notifications:
        config["notifications"] = notifications

    # --- ATS ---
    ats = {}
    if _has("ats_scan_enabled"):
        ats["scan_enabled"] = form["ats_scan_enabled"] == "on"
    if _has("ats_scan_days"):
        ats["scan_days"] = form["ats_scan_days"]
    if _has("ats_scan_hour"):
        ats["scan_hour"] = safe_int(form["ats_scan_hour"], 7)
    if ats:
        config["ats"] = ats

    return config


def _parse_serpapi_queries(form) -> list:
    """Extract SerpAPI queries from form fields (variable number of rows)."""
    queries = []
    i = 0
    while True:
        query = form.get(f"serpapi_query_{i}", "").strip()
        location = form.get(f"serpapi_location_{i}", "").strip()
        if not query and not location:
            break
        if query or location:
            queries.append({"query": query, "location": location})
        i += 1
        if i > 50:  # safety limit
            break
    return queries


def _write_config(config: dict, config_path: str = _CONFIG_PATH) -> None:
    """Write config dict to YAML file."""
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
