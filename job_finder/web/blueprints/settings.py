"""Settings blueprint — Settings page routes.

Routes:
    GET  /settings       -- Load config.yaml, render settings form
    POST /settings/save  -- Read form data, write back to config.yaml, update running config
"""

import logging
import os
from pathlib import Path

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

from job_finder import secrets as jf_secrets
from job_finder.config import (
    DEFAULT_CANDIDATE_SCORE_THRESHOLD,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    load_config,
)

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

    config_mtime = 0
    try:
        config_mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        pass

    # Commit 3.5: render (set)/(not set) placeholders for password inputs
    # instead of leaking the plaintext value to the HTML. Only checks env +
    # keyring — a plaintext-only value would still pass the read, but showing
    # "(set)" would mislead the user into thinking the migration ran.
    secret_set = {
        name: jf_secrets.get_secret(name) is not None
        for name in ("sources.serpapi.api_key", "sources.thordata.api_key")
    }

    return render_template(
        "settings/index.html",
        config=config,
        config_mtime=config_mtime,
        secret_set=secret_set,
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
                    flash(
                        "Settings were modified externally. Page reloaded with latest values.",
                        "warning",
                    )
                    return redirect(url_for("settings.index"))
            except (OSError, ValueError):
                pass

        form_config = _parse_form_to_config(request.form)

        # Commit 3.5: route freshly-submitted secrets through the keyring
        # stack. On success the form_config plaintext is cleared so the
        # deep-merge below wipes the legacy value from config.yaml. On
        # backend-missing (RuntimeError) the plaintext stays and we flash
        # a warning so the user knows storage degraded gracefully.
        _move_secret_to_keyring(
            form_config, ("sources", "serpapi", "api_key"), "sources.serpapi.api_key"
        )
        _move_secret_to_keyring(
            form_config, ("sources", "jsearch", "rapidapi_key"), "sources.jsearch.rapidapi_key"
        )

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
            flash(
                f"Save blocked: would wipe {', '.join(wiped)}. Check form and try again.", "error"
            )
            return redirect(url_for("settings.index"))

        _write_config(config, _CONFIG_PATH)

        # Update running app config so changes take effect without restart.
        # Thread-safety: APScheduler and batch background threads MUST snapshot
        # JF_CONFIG at job-start time (i.e. read once into a local variable before
        # any await/sleep) rather than reading individual keys across multiple
        # statements. This replacement is atomic at the Python dict level but
        # readers may observe the old dict between the two assignments below.
        current_app.config["JF_CONFIG"] = config
        if "db" in config:
            current_app.config["DB_PATH"] = config["db"].get(
                "path", current_app.config.get("DB_PATH")
            )

        flash("Settings saved successfully.", "success")
    except Exception as exc:
        flash(f"Error saving settings: {exc}", "error")

    return redirect(url_for("settings.index"))


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
    # Commit 3.5: only include api_key when the user typed something. The
    # password input now renders with value="" + a (set)/(not set) placeholder,
    # so an empty submission means "leave existing secret alone" — including
    # it as "" would clobber the keyring-or-plaintext value on every save.
    if _has("serpapi_api_key") and form["serpapi_api_key"]:
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
    if _has("jsearch_rapidapi_key") and form["jsearch_rapidapi_key"]:
        jsearch["rapidapi_key"] = form["jsearch_rapidapi_key"]
    if jsearch:
        config.setdefault("sources", {})["jsearch"] = jsearch

    # --- Scoring ---
    scoring = {}
    weights = {}
    for wk in (
        "title_match",
        "seniority_alignment",
        "location_fit",
        "salary_range",
        "industry_relevance",
        "company_signals",
        "recency",
    ):
        fk = f"weight_{wk}"
        if _has(fk):
            weights[wk] = safe_float(form[fk])
    if weights:
        scoring["weights"] = weights
    if _has("min_score_threshold"):
        scoring["min_score_threshold"] = safe_int(
            form["min_score_threshold"], DEFAULT_MIN_SCORE_THRESHOLD
        )
    if _has("candidate_score_threshold"):
        scoring["candidate_score_threshold"] = safe_int(
            form["candidate_score_threshold"], DEFAULT_CANDIDATE_SCORE_THRESHOLD
        )
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


def _move_secret_to_keyring(
    form_config: dict, path: tuple[str, ...], canonical: str
) -> None:
    """Move a freshly-submitted secret from form_config into the OS keyring.

    Walks `path` through `form_config`; if a non-empty string is at the
    leaf, writes it under `canonical` and clears the leaf so the deep-merge
    in save() wipes the legacy plaintext from config.yaml.

    On RuntimeError (no keyring backend) the leaf is left alone — the
    plaintext value still flows through to config.yaml so the user doesn't
    lose their secret. A flash warning informs them of the degradation.
    """
    node = form_config
    for part in path[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    leaf = path[-1]
    if not isinstance(node, dict) or leaf not in node:
        return
    value = node[leaf]
    if not isinstance(value, str) or not value:
        return
    try:
        jf_secrets.set_secret(canonical, value)
        node[leaf] = ""
    except RuntimeError:
        flash(
            "Couldn't write secret to OS keyring — saved to config.yaml as "
            "plaintext fallback. See SECURITY.md.",
            "warning",
        )


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
    """Write config dict to YAML file atomically.

    Writes to a sibling temp file first, then uses os.replace() for an atomic
    rename so a crash or OS error mid-write cannot produce a partial/empty file.

    On POSIX, chmods the destination to 0600 after the replace so an IMAP app
    password / provider API key sitting in plaintext at rest is at least not
    world-readable. Windows uses ACLs not POSIX modes; the default
    home-directory ACL is already user-only there (M-4, 2026-05-20).
    """
    config_path_obj = Path(config_path)
    tmp_path = config_path_obj.with_suffix(".yaml.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, config_path)
        if os.name != "nt":
            try:
                os.chmod(config_path, 0o600)
            except OSError as exc:
                logger.warning(
                    "could not chmod 0600 on %s; secrets may be world-readable: %s",
                    config_path, exc,
                )
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
