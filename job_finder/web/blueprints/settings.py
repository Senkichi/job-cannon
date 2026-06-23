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
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    load_config,
    normalize_profile_work_arrangement,
)
from job_finder.web import user_data_dirs
from job_finder.web._htmx import htmx_fragment
from job_finder.web.autoheal.health_monitor import sources_needing_attention
from job_finder.web.db_helpers import get_db, refresh_jf_config
from job_finder.web.model_provider import is_supported_provider_name
from job_finder.web.onboarding.inbox_check import run_inbox_check

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# Optional test override only — production leaves this None and resolves the
# config.yaml path FRESH per-request via _config_path().
#
# This used to be `str(user_data_dirs.config_path())` evaluated AT IMPORT. That
# froze the path to whatever $JOB_CANNON_USER_DATA_DIR pointed at when the module
# was first imported (during test collection, the dev machine's repo root). Any
# test that hit /settings/save WITHOUT pinning this global then wrote its
# example-seeded config to the REAL config.yaml — silently resetting
# target_titles from the user's curated list to the 2 example defaults (the
# 2026-06-18 wipe; same import-frozen-path class as the PR #504 live-DB leak).
# Resolving per-request lets the per-test JOB_CANNON_USER_DATA_DIR redirect win.
_CONFIG_PATH: str | None = None


def _config_path() -> str:
    """Resolve the active config.yaml path, honoring a test override.

    Production: ``_CONFIG_PATH`` is None → resolve fresh from
    ``user_data_dirs.config_path()`` on every call, so the env-var redirect
    (including each test's temp user-data dir) always wins. Tests may set
    ``settings._CONFIG_PATH`` to pin an explicit path.
    """
    return _CONFIG_PATH or str(user_data_dirs.config_path())


# AI cascade providers the Settings form can select as providers.primary. Curated
# subset of model_provider._PROVIDER_DEFAULTS (every entry resolves to a real
# model, so none raise "no model for workload"); the submitted value is still
# validated against model_provider.is_supported_provider_name on save. (key, label).
_SELECTABLE_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("ollama", "Ollama — $0 local inference"),
    ("claude_code_cli", "Claude Code CLI — $0 (Claude.ai subscription)"),
    ("gemini_cli", "Gemini CLI — $0 (Google AI Studio free tier)"),
    ("gemini", "Gemini — BYO API key (free tier)"),
    ("groq", "Groq — BYO API key"),
    ("cerebras", "Cerebras — BYO API key"),
    ("anthropic", "Anthropic — $0 (Claude.ai OAuth transport)"),
    ("anthropic_api", "Anthropic API — paid, per-token"),
    ("local_bundled", "Local bundled GGUF — $0 (requires [local-ai] extra)"),
)

# BYO-key providers whose API key the Settings form can set / rotate. Each maps to
# the canonical SECRET_ENV_VARS name providers.api_keys.<name> and is routed to the
# OS keyring in save() (mirrors the source-secret pattern). (key, label).
_PROVIDER_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("anthropic", "Anthropic API key"),
    ("gemini", "Gemini API key"),
    ("groq", "Groq API key"),
    ("cerebras", "Cerebras API key"),
    ("openrouter", "OpenRouter API key"),
)


@settings_bp.route("/", strict_slashes=False)
def index():
    """Settings page — display config.yaml values in editable form."""
    try:
        config = load_config(_config_path())
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
        config_mtime = os.path.getmtime(_config_path())
    except OSError:
        pass

    # Commit 3.5: render (set)/(not set) placeholders for password inputs
    # instead of leaking the plaintext value to the HTML. Only checks env +
    # keyring — a plaintext-only value would still pass the read, but showing
    # "(set)" would mislead the user into thinking the migration ran.
    #
    # The portal_search.* entries cover the Stage 7.1 USAJobs/Adzuna/Jooble
    # credentials. They were previously omitted, which meant the settings UI
    # placeholder check (template uses `config.get(...).get('app_id')`)
    # always showed "(not set)" after a save — because the save flow moves
    # those values to the keyring and writes an empty string back to
    # config.yaml. Users hit "save" repeatedly thinking it was broken.
    secret_set = {
        name: jf_secrets.get_secret(name) is not None
        for name in (
            "sources.imap.app_password",
            "sources.serpapi.api_key",
            "sources.dataforseo.api_key",
            "sources.google_cse.api_key",
            "sources.google_cse.cse_id",
            "sources.portal_search.usajobs.user_agent_email",
            "sources.portal_search.usajobs.authorization_key",
            "sources.portal_search.adzuna.app_id",
            "sources.portal_search.adzuna.app_key",
            "sources.portal_search.jooble.api_key",
            "providers.api_keys.anthropic",
            "providers.api_keys.gemini",
            "providers.api_keys.groq",
            "providers.api_keys.cerebras",
            "providers.api_keys.openrouter",
        )
    }

    # F1: inbox-wiring system check — auth probe + email_parse_log activity window.
    inbox_status = _safe_run_inbox_check(config)

    return render_template(
        "settings/index.html",
        config=config,
        config_mtime=config_mtime,
        secret_set=secret_set,
        inbox_status=inbox_status,
        source_attention=_safe_source_attention(),
        selectable_providers=_SELECTABLE_PROVIDERS,
        provider_key_fields=_PROVIDER_KEY_FIELDS,
    )


@settings_bp.route("/inbox-check", strict_slashes=False)
@htmx_fragment("settings.index")
def inbox_check_fragment():
    """HTMX fragment — re-run the inbox-wiring check on demand.

    Returns the same tile rendered standalone so HTMX can swap it in place.
    """
    try:
        config = load_config(_config_path())
    except FileNotFoundError:
        config = current_app.config.get("JF_CONFIG", {})
    inbox_status = _safe_run_inbox_check(config)
    return render_template(
        "settings/_inbox_status_tile.html",
        inbox_status=inbox_status,
    )


@settings_bp.route("/source-health", strict_slashes=False)
@htmx_fragment("settings.index")
def source_health_fragment():
    """HTMX fragment — the source credential/degraded banner, swapped in place.

    Non-HTMX direct hits redirect to the Settings index so the banner is never
    rendered as a bare standalone page (mirrors dashboard.degraded_sources_fragment).
    """
    return render_template(
        "settings/_source_health_banner.html",
        source_attention=_safe_source_attention(),
    )


def _safe_run_inbox_check(config: dict):
    """Run `run_inbox_check` with the request-scoped DB connection.

    Catches and logs any failure so the Settings page never 500s because the
    check raised. Returns None if a connection isn't available (e.g. in tests
    without a configured DB_PATH).
    """
    try:
        db_path = current_app.config.get("DB_PATH")
        if not db_path:
            return None
        conn = get_db(db_path)
        return run_inbox_check(config, conn)
    except Exception as exc:
        logger.warning("inbox_check failed in settings.index: %s", type(exc).__name__)
        return None


def _safe_source_attention() -> list[dict]:
    """Read sources needing attention with the configured DB; never 500s the page.

    Returns [] when no DB is available (e.g. tests without DB_PATH) or the read
    raises — the banner simply renders nothing. Mirrors `_safe_run_inbox_check`.
    """
    try:
        db_path = current_app.config.get("DB_PATH")
        if not db_path:
            return []
        conn = get_db(db_path)
        return sources_needing_attention(conn)
    except Exception as exc:
        logger.warning("source attention check failed in settings: %s", type(exc).__name__)
        return []


@settings_bp.route("/save", methods=["POST"], strict_slashes=False)
def save():
    """Save settings form data to config.yaml and update running app config."""
    try:
        # Load existing config first so we preserve keys not in the form
        try:
            existing = load_config(_config_path())
        except FileNotFoundError:
            existing = {}

        # Stale-form detection: reject if config was modified since page load
        submitted_mtime = request.form.get("_config_mtime", "")
        if submitted_mtime:
            try:
                current_mtime = os.path.getmtime(_config_path())
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
            form_config, ("sources", "dataforseo", "api_key"), "sources.dataforseo.api_key"
        )
        _move_secret_to_keyring(
            form_config, ("sources", "google_cse", "api_key"), "sources.google_cse.api_key"
        )
        _move_secret_to_keyring(
            form_config, ("sources", "google_cse", "cse_id"), "sources.google_cse.cse_id"
        )
        # Stage 7.2: route IMAP app_password to keyring (same canonical name
        # the onboarding wizard writes through).
        _move_secret_to_keyring(
            form_config, ("sources", "imap", "app_password"), "sources.imap.app_password"
        )
        # Stage 7.1: route USAJobs/Adzuna/Jooble portal_search creds to keyring.
        # Canonical names mirror the nested config tree (see secrets.py).
        _move_secret_to_keyring(
            form_config,
            ("sources", "portal_search", "usajobs", "user_agent_email"),
            "sources.portal_search.usajobs.user_agent_email",
        )
        _move_secret_to_keyring(
            form_config,
            ("sources", "portal_search", "usajobs", "authorization_key"),
            "sources.portal_search.usajobs.authorization_key",
        )
        _move_secret_to_keyring(
            form_config,
            ("sources", "portal_search", "adzuna", "app_id"),
            "sources.portal_search.adzuna.app_id",
        )
        _move_secret_to_keyring(
            form_config,
            ("sources", "portal_search", "adzuna", "app_key"),
            "sources.portal_search.adzuna.app_key",
        )
        _move_secret_to_keyring(
            form_config,
            ("sources", "portal_search", "jooble", "api_key"),
            "sources.portal_search.jooble.api_key",
        )
        # Provider BYO API keys -> keyring (canonical providers.api_keys.<name>),
        # so a user can rotate/correct a provider key from Settings (previously
        # onboarding-only). Same keyring-or-plaintext-fallback semantics as above.
        for _pkey, _plabel in _PROVIDER_KEY_FIELDS:
            _move_secret_to_keyring(
                form_config,
                ("providers", "api_keys", _pkey),
                f"providers.api_keys.{_pkey}",
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

        # Heal the legacy "Remote" sentinel BEFORE persisting: a user who types
        # "Remote" into target_locations would otherwise have it written to disk
        # but stripped only in-memory by load_config on the next read — the value
        # appears to "not save". Enforcing the invariant (no remote sentinel in
        # target_locations; arrangement lives in work_arrangement) at the write
        # boundary keeps disk + the refreshed in-memory config consistent and
        # self-heals any already-stored sentinel on the next save. Idempotent.
        config = normalize_profile_work_arrangement(config)

        _write_config(config, _config_path())

        # Refresh the live in-memory config so changes take effect without restart.
        refresh_jf_config(current_app._get_current_object(), config)

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

    def _has_value(key):
        """True iff a field was submitted AND is non-blank.

        For numeric fields, a blank submission means "leave the existing value
        alone" (omit from form_config so _deep_merge preserves it) rather than
        silently substituting a non-empty default. Clearing the budget cap used
        to snap it back to $10 — re-enabling paid spend — and clearing the score
        thresholds reset them to 40, all under a "saved successfully" flash. An
        explicit "0" still strips truthy and is written through.
        """
        return key in form and form[key].strip() != ""

    def _checked(key):
        """True iff a checkbox is checked in the submitted form.

        Form templates emit a hidden empty input AND a real checkbox under the
        same name (so an absent checkbox still posts the field). Werkzeug's
        ``form[key]`` returns the first matching value — the hidden's empty
        string — which made the legacy ``form[key] == "on"`` always False even
        when the box was checked. Use this helper instead.
        """
        return "on" in form.getlist(key)

    config = {}

    # --- Profile ---
    profile = {}
    if _has("target_titles"):
        profile["target_titles"] = lines_to_list(form["target_titles"])
    if _has("target_locations"):
        profile["target_locations"] = lines_to_list(form["target_locations"])
    if _has("work_arrangement"):
        # Canonical remote/hybrid/on-site preference (consumed by location_fit +
        # scoring_orchestrator). Mirrors the onboarding wizard's toggle; values
        # validated against the same vocabulary (location_fit.VALID_WORK_ARRANGEMENTS).
        # Anything unexpected falls back to "remote" — the wizard's default.
        wa = form["work_arrangement"].strip().lower()
        profile["work_arrangement"] = wa if wa in ("remote", "hybrid", "on-site") else "remote"
    if _has("home_country"):
        # ISO 3166-1 alpha-2 country code, compared (case-insensitively) against
        # each job's country_code by location_fit. Stored upper-cased; blank
        # clears the preference (location_fit's †-rows fire only when present).
        profile["home_country"] = form["home_country"].strip().upper()
    if _has_value("min_salary"):
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
        gmail["enabled"] = _checked("gmail_enabled")
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

    # --- Sources: IMAP (Stage 7.2) ---
    # Settings-side counterpart to the onboarding imap_credentials step.
    # app_password is routed through _move_secret_to_keyring in save(); the
    # "only if non-empty" guard on the password field is the same pattern
    # serpapi/dataforseo/etc. use to avoid clobbering on re-saves with the
    # field blank.
    imap = {}
    if _has("imap_enabled"):
        imap["enabled"] = _checked("imap_enabled")
    if _has("imap_email"):
        imap["email"] = form["imap_email"].strip()
    if _has("imap_app_password") and form["imap_app_password"]:
        # Preserve any leading/trailing spaces (app passwords may include them).
        imap["app_password"] = form["imap_app_password"]
    if _has("imap_host"):
        host = form["imap_host"].strip()
        if host:
            imap["host"] = host
    if _has("imap_port"):
        imap["port"] = safe_int(form["imap_port"], 993)
    if _has("imap_folder"):
        folder = form["imap_folder"].strip()
        if folder:
            imap["folder"] = folder
    if imap:
        config.setdefault("sources", {})["imap"] = imap

    # --- Sources: SerpAPI ---
    serpapi = {}
    if _has("serpapi_enabled"):
        serpapi["enabled"] = _checked("serpapi_enabled")
    # Commit 3.5: only include api_key when the user typed something. The
    # password input now renders with value="" + a (set)/(not set) placeholder,
    # so an empty submission means "leave existing secret alone" — including
    # it as "" would clobber the keyring-or-plaintext value on every save.
    if _has("serpapi_api_key") and form["serpapi_api_key"]:
        serpapi["api_key"] = form["serpapi_api_key"]
    # Sentinel hidden input marks that the queries section was rendered;
    # if present we parse queries (possibly []), otherwise preserve existing.
    if _has("_serpapi_queries_present"):
        serpapi["queries"] = _parse_query_rows(form, "serpapi")
    if serpapi:
        config.setdefault("sources", {})["serpapi"] = serpapi

    # --- Sources: DataForSEO (Stage 6 — NEW tile) ---
    dataforseo = {}
    if _has("dataforseo_enabled"):
        dataforseo["enabled"] = _checked("dataforseo_enabled")
    if _has("dataforseo_api_key") and form["dataforseo_api_key"]:
        dataforseo["api_key"] = form["dataforseo_api_key"]
    if _has("dataforseo_max_age_days"):
        dataforseo["max_age_days"] = safe_int(form["dataforseo_max_age_days"], 7)
    if _has("dataforseo_depth"):
        depth = safe_int(form["dataforseo_depth"], 200)
        # Clamp to DataForSEO's documented bounds (10–200, multiples of 10)
        dataforseo["depth"] = max(10, min(200, depth))
    if _has("dataforseo_priority"):
        # 1 = normal, 2 = high; anything else falls back to 1
        priority = safe_int(form["dataforseo_priority"], 1)
        dataforseo["priority"] = priority if priority in (1, 2) else 1
    if _has("_dataforseo_queries_present"):
        dataforseo["queries"] = _parse_query_rows(form, "dataforseo")
    if dataforseo:
        config.setdefault("sources", {})["dataforseo"] = dataforseo

    # --- Sources: Google CSE (Stage 6 — NEW tile) ---
    google_cse = {}
    if _has("google_cse_enabled"):
        google_cse["enabled"] = _checked("google_cse_enabled")
    if _has("google_cse_api_key") and form["google_cse_api_key"]:
        google_cse["api_key"] = form["google_cse_api_key"]
    if _has("google_cse_cse_id") and form["google_cse_cse_id"]:
        google_cse["cse_id"] = form["google_cse_cse_id"]
    if google_cse:
        config.setdefault("sources", {})["google_cse"] = google_cse

    # --- Sources: portal_search (Stage 7 — NEW tile) ---
    # Master switch + keywords + sub-portal toggles. Secret credentials for
    # USAJobs/Adzuna/Jooble are routed through _move_secret_to_keyring in save().
    portal_search = {}
    if _has("portal_search_enabled"):
        portal_search["enabled"] = _checked("portal_search_enabled")
    if _has("portal_search_keywords"):
        portal_search["keywords"] = lines_to_list(form["portal_search_keywords"])
    if _has("portal_search_max_serp_queries"):
        portal_search["max_serp_queries"] = safe_int(form["portal_search_max_serp_queries"], 30)
    # Keyless sub-portals
    if _has("portal_search_jobicy_enabled"):
        portal_search["jobicy"] = {"enabled": _checked("portal_search_jobicy_enabled")}
    if _has("portal_search_yc_enabled"):
        portal_search["yc_workatastartup"] = {"enabled": _checked("portal_search_yc_enabled")}
    # USAJobs (toggle + email + auth key)
    usajobs = {}
    if _has("portal_search_usajobs_enabled"):
        usajobs["enabled"] = _checked("portal_search_usajobs_enabled")
    if (
        _has("portal_search_usajobs_user_agent_email")
        and form["portal_search_usajobs_user_agent_email"]
    ):
        usajobs["user_agent_email"] = form["portal_search_usajobs_user_agent_email"]
    if (
        _has("portal_search_usajobs_authorization_key")
        and form["portal_search_usajobs_authorization_key"]
    ):
        usajobs["authorization_key"] = form["portal_search_usajobs_authorization_key"]
    if usajobs:
        portal_search["usajobs"] = usajobs
    # Adzuna (toggle + app_id + app_key + country)
    adzuna = {}
    if _has("portal_search_adzuna_enabled"):
        adzuna["enabled"] = _checked("portal_search_adzuna_enabled")
    if _has("portal_search_adzuna_app_id") and form["portal_search_adzuna_app_id"]:
        adzuna["app_id"] = form["portal_search_adzuna_app_id"]
    if _has("portal_search_adzuna_app_key") and form["portal_search_adzuna_app_key"]:
        adzuna["app_key"] = form["portal_search_adzuna_app_key"]
    if _has("portal_search_adzuna_country"):
        country = form["portal_search_adzuna_country"].strip().lower()
        if country:
            adzuna["country"] = country
    if adzuna:
        portal_search["adzuna"] = adzuna
    # Jooble (toggle + api_key)
    jooble = {}
    if _has("portal_search_jooble_enabled"):
        jooble["enabled"] = _checked("portal_search_jooble_enabled")
    if _has("portal_search_jooble_api_key") and form["portal_search_jooble_api_key"]:
        jooble["api_key"] = form["portal_search_jooble_api_key"]
    if jooble:
        portal_search["jooble"] = jooble
    if portal_search:
        config.setdefault("sources", {})["portal_search"] = portal_search

    # --- Providers (AI cascade) ---
    # providers.primary selects the head of the scoring/quick cascade; the
    # per-provider BYO api_keys are routed to the OS keyring in save() (same
    # (set)/(not set) placeholder + no-clobber-on-empty pattern as source
    # secrets). fallback_chain / overrides / limits are preserved by _deep_merge
    # — this block only writes the keys the form actually owns.
    if _has("provider_primary"):
        pname = form["provider_primary"].strip()
        # Validate against the canonical registry; ignore anything unknown
        # rather than writing a primary the cascade can't route.
        if is_supported_provider_name(pname):
            config.setdefault("providers", {})["primary"] = pname
    for pkey, _label in _PROVIDER_KEY_FIELDS:
        fk = f"provider_key_{pkey}"
        # Only include when the user typed something — an empty submission means
        # "leave the existing secret alone" (mirrors the source-key guard).
        if _has(fk) and form[fk]:
            config.setdefault("providers", {}).setdefault("api_keys", {})[pkey] = form[fk]

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
    if _has_value("min_score_threshold"):
        scoring["min_score_threshold"] = safe_int(
            form["min_score_threshold"], DEFAULT_MIN_SCORE_THRESHOLD
        )
    if _has_value("candidate_score_threshold"):
        scoring["candidate_score_threshold"] = safe_int(
            form["candidate_score_threshold"], DEFAULT_CANDIDATE_SCORE_THRESHOLD
        )
    if _has_value("daily_budget_usd"):
        scoring["daily_budget_usd"] = safe_float(
            form["daily_budget_usd"], DEFAULT_DAILY_BUDGET_USD
        )
    # Apply-verdict tuning (consumed by db._assessment_writer / _classification).
    # Blank = preserve existing (same _has_value rationale as the thresholds above).
    if _has_value("low_signal_jd_chars"):
        scoring["low_signal_jd_chars"] = safe_int(form["low_signal_jd_chars"], 1500)
    if _has_value("apply_mean_floor"):
        scoring["apply_mean_floor"] = safe_float(form["apply_mean_floor"], 3.5)
    if _has_value("apply_min_strong_axes"):
        scoring["apply_min_strong_axes"] = safe_int(form["apply_min_strong_axes"], 3)
    if scoring:
        config["scoring"] = scoring

    # --- Scheduler ---
    # cadence_preset resizes ingestion + enrichment/backfill frequency. Cron jobs
    # are registered at startup, so a change here only takes effect on the next app
    # restart (refresh_jf_config updates the live dict but does not re-register
    # jobs); the template hint says so.
    if _has("cadence_preset"):
        preset = form["cadence_preset"].strip().lower()
        if preset in ("light", "standard", "heavy"):
            config.setdefault("scheduler", {})["cadence_preset"] = preset

    # --- Database ---
    if _has("db_path"):
        config["db"] = {"path": form["db_path"]}

    # --- ATS ---
    ats = {}
    if _has("ats_scan_enabled"):
        ats["scan_enabled"] = _checked("ats_scan_enabled")
    if _has("ats_scan_days"):
        ats["scan_days"] = form["ats_scan_days"]
    if _has("ats_scan_hour"):
        ats["scan_hour"] = safe_int(form["ats_scan_hour"], 7)
    if ats:
        config["ats"] = ats

    return config


def _move_secret_to_keyring(form_config: dict, path: tuple[str, ...], canonical: str) -> None:
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


def _parse_query_rows(form, prefix: str) -> list:
    """Extract {query, location} rows from a form section indexed by `prefix`.

    Form fields are expected to be named `{prefix}_query_{i}` and
    `{prefix}_location_{i}` for i = 0, 1, 2, ... Iteration stops at the first
    row where both query and location are empty, or after 50 rows (safety
    limit against malicious or runaway form submissions).

    Replaces the per-source `_parse_serpapi_queries` helper as of Stage 6
    (2026-05-22) when DataForSEO + Thordata gained their own query rows.
    """
    queries = []
    i = 0
    while True:
        query = form.get(f"{prefix}_query_{i}", "").strip()
        location = form.get(f"{prefix}_location_{i}", "").strip()
        if not query and not location:
            break
        if query or location:
            queries.append({"query": query, "location": location})
        i += 1
        if i > 50:  # safety limit
            break
    return queries


def _write_config(config: dict, config_path: str | None = None) -> None:
    """Write config dict to YAML file atomically.

    ``config_path`` defaults to the per-request-resolved active config path
    (never an import-frozen value — see ``_config_path``).

    Writes to a sibling temp file first, then uses os.replace() for an atomic
    rename so a crash or OS error mid-write cannot produce a partial/empty file.

    On POSIX, chmods the destination to 0600 after the replace so an IMAP app
    password / provider API key sitting in plaintext at rest is at least not
    world-readable. Windows uses ACLs not POSIX modes; the default
    home-directory ACL is already user-only there (M-4, 2026-05-20).
    """
    config_path = config_path or _config_path()
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
                    config_path,
                    exc,
                )
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
