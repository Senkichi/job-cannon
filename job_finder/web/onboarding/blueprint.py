"""Onboarding wizard blueprint (Phase 42, STRANGE-WIZ-02/05).

Eight routes, one screen each, numbered sequentially 1..8:
    welcome (1), provider_select (2), provider_credentials (3),
    resume_upload (4), profile_edit (5), imap_credentials (6),
    schedule (7), done (8).

Step indicator (D-21): each route unpacks _step("<route>") into render_template,
which supplies step_num + step_total + step_label to _base.html. The ordered
tuple _WIZARD_STEPS is the single source of truth for both the number and the
"of N" denominator — neither is hardcoded in the template. resume_upload (step 4)
is optional (D-05); skipping it advances to profile_edit (step 5) and leaves
every screen's fixed number unchanged.

POST sequencing (D-13): every POST calls state.write_wizard_data(slice) to stash
the form data, then redirects to the next step. Only /done writes to config.yaml
(plan 42-06 implements that body).

Security (T-42-03, T-42-07, T-42-08): resume_upload uses tempfile.NamedTemporaryFile
with the suffix from Path(uploaded.filename).suffix - the user-supplied filename
NEVER appears in any disk path. MAX_CONTENT_LENGTH bound at 10 MB.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

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
    DEFAULT_DAILY_BUDGET_USD,
    ConfigError,
    ConfigNotFoundError,
    load_config,
)
from job_finder.web import user_data_dirs
from job_finder.web.db_helpers import get_db, refresh_jf_config
from job_finder.web.onboarding import imap_test, resume_parser, state, system_check
from job_finder.web.providers.detection import detect_available_providers, get_detection_extras
from job_finder.web.scheduler import get_scheduler

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

# Issue #400: redirect already-completed users out of the wizard. The app-level
# gate_onboarding whitelists /onboarding/* (so an incomplete user is never
# trapped); this blueprint-level guard enforces the inverse — a completed user
# requesting ANY wizard route (welcome..done, GET or POST) is bounced to /jobs.
# Single point of enforcement at the blueprint boundary makes "completed user
# inside the wizard" unrepresentable; the done-POST overwrite guard remains as
# defense in depth.
onboarding_bp.before_request(state.gate_completed_onboarding)

# T-42-07 / V12: cap resume upload at 10 MB. Enforced by checking Content-Length manually
# in the resume_upload handler because Flask's MAX_CONTENT_LENGTH is app-level, not
# blueprint-level, and modifying app-level would break other blueprints' uploads.
_MAX_RESUME_BYTES: Final[int] = 10 * 1024 * 1024

# $0 CLIs that need no credentials at the provider_credentials step (D-04).
# "none" represents the "skip — configure later in Settings" path (Issue #288).
# "local_bundled" requires only a model_path in Settings, no API key.
_NO_CREDS_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"claude_code_cli", "gemini_cli", "ollama", "local_bundled", "none"}
)

# Server-side guard for the Gmail-address field (Issue #399). The template's
# type="email" + pattern only blocks the obvious cases in the browser; a malformed
# address that slips past (paste with trailing junk, scripted POST, autofill quirk)
# would otherwise reach check_imap and surface as a confusing auth/connection error
# instead of a clear "that's not a valid address". Conservative shape check only —
# the IMAP smoke test is the real proof the address works.
_EMAIL_RE: Final = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _is_valid_email(value: str) -> bool:
    """Return True when ``value`` has the basic ``local@domain.tld`` shape."""
    return bool(_EMAIL_RE.match(value or ""))


# Canonical ordered list of wizard screens (key, label) — the single source of
# truth for the step indicator (D-21). The "Step N of M" number and denominator
# AND the progress-bar width in onboarding/_base.html all derive from this tuple;
# nothing is hardcoded template-side. Adding/removing a screen here updates every
# screen's count automatically and keeps the numbering unique + sequential.
_WIZARD_STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("welcome", "Welcome"),
    ("provider_select", "AI provider"),
    ("provider_credentials", "Credentials"),
    ("resume_upload", "Resume"),
    ("profile_edit", "Profile"),
    ("imap_credentials", "Gmail"),
    ("schedule", "Schedule"),
    ("done", "Ready"),
)
_WIZARD_STEP_NUMS: Final[dict[str, int]] = {
    key: num for num, (key, _) in enumerate(_WIZARD_STEPS, start=1)
}
_WIZARD_STEP_LABELS: Final[dict[str, str]] = dict(_WIZARD_STEPS)


def _step(key: str) -> dict:
    """Step-indicator render context for a wizard route (D-21).

    Returns step_num / step_total / step_label sourced from _WIZARD_STEPS so the
    indicator and progress bar in _base.html stay collision-free and correctly
    denominated. Unpack into render_template, e.g. ``**_step("welcome")``. The
    optional resume_upload screen keeps its fixed number whether shown or skipped.
    """
    return {
        "step_num": _WIZARD_STEP_NUMS[key],
        "step_total": len(_WIZARD_STEPS),
        "step_label": _WIZARD_STEP_LABELS[key],
    }


# --- Helpers ---


def _db():
    return get_db()


def _wizard():
    """Read wizard_data from DB - convenience for handlers."""
    return state.read_wizard_data(_db())


def _move_secret_or_warn(config_slice: dict, path: tuple[str, ...], canonical: str) -> None:
    """Move a secret out of `config_slice` into the OS keyring.

    Walks `path`; if a non-empty string is at the leaf, writes it under
    `canonical` and clears the leaf to "" so the atomic config.yaml write
    persists no plaintext. On RuntimeError (no keyring backend) the leaf
    is left alone — plaintext flows through to config.yaml and the user
    is flashed a warning so degradation is visible, not silent.
    """
    node = config_slice
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


# --- Routes (8 total) ---


@onboarding_bp.route("/welcome", methods=["GET", "POST"], strict_slashes=False)
def welcome():
    """Step 1: system check + 'Get started' button."""
    if request.method == "POST":
        return redirect(url_for("onboarding.provider_select"))
    checks = system_check.run_all()
    return render_template(
        "onboarding/welcome.html",
        checks=checks,
        **_step("welcome"),
    )


@onboarding_bp.route("/provider_select", methods=["GET", "POST"], strict_slashes=False)
def provider_select():
    """Step 2: render detected providers + Re-detect button (D-01, D-02).

    Issue #288 additions:
    - "Skip — configure later" path writes provider.name="none" and skips
      provider_credentials entirely, advancing to resume_upload.  The done
      step omits providers.primary from config when name="none" so
      tier_has_configured_provider returns False and the dashboard warning
      fires on first login.
    - detection_extras passed to template for Ollama-no-model guidance.
    """
    if request.method == "POST":
        if request.form.get("redetect"):
            # D-02: refresh detection cache (also recomputes DetectionExtras)
            detect_available_providers(refresh=True)
            return redirect(url_for("onboarding.provider_select"))

        if request.form.get("skip_provider"):
            # Issue #288: explicit "configure later" escape hatch.
            state.write_wizard_data(_db(), {"provider": {"name": "none"}})
            # Skip provider_credentials entirely — no credentials needed
            return redirect(url_for("onboarding.resume_upload"))

        provider_name = request.form.get("provider_name", "").strip()
        if not provider_name:
            flash("Please choose a provider to continue.", "error")
            return redirect(url_for("onboarding.provider_select"))
        state.write_wizard_data(_db(), {"provider": {"name": provider_name}})
        return redirect(url_for("onboarding.provider_credentials"))

    providers = detect_available_providers()
    extras = get_detection_extras()
    return render_template(
        "onboarding/provider_select.html",
        providers=providers,
        ollama_no_model=extras.ollama_no_model,
        **_step("provider_select"),
    )


@onboarding_bp.route("/provider_credentials", methods=["GET", "POST"], strict_slashes=False)
def provider_credentials():
    """Step 3: conditional - $0 CLIs render confirmation card; Anthropic renders API-key form (D-04)."""
    data = _wizard()
    provider_name = (data.get("provider") or {}).get("name", "anthropic")
    needs_api_key = provider_name not in _NO_CREDS_PROVIDERS

    if request.method == "POST":
        slice_: dict = {"provider": {"name": provider_name}}
        if needs_api_key:
            api_key = request.form.get("api_key", "").strip()
            if not api_key:
                flash("API key is required for this provider.", "error")
                return redirect(url_for("onboarding.provider_credentials"))
            slice_["provider"]["api_key"] = api_key
        state.write_wizard_data(_db(), slice_)
        return redirect(url_for("onboarding.resume_upload"))

    return render_template(
        "onboarding/provider_credentials.html",
        provider_name=provider_name,
        needs_api_key=needs_api_key,
        **_step("provider_credentials"),
    )


@onboarding_bp.route("/resume_upload", methods=["GET", "POST"], strict_slashes=False)
def resume_upload():
    """Step 4a: optional PDF/DOCX upload (D-05). Parse + stash in wizard_data (D-07).

    Security (T-42-03, T-42-07):
    - Filename validation by extension + content-length check
    - Disk path uses tempfile.NamedTemporaryFile - user filename NEVER in disk path
    - File unlinked in finally block regardless of success/failure
    """
    if request.method == "POST":
        if request.form.get("skip"):
            # Skipping is a deliberate choice, not a parse failure — clear any stale
            # flag from a prior failed upload so profile_edit shows no notice.
            state.write_wizard_data(
                _db(),
                {"resume_profile": {}, "resume_parse_failed": False},
            )
            return redirect(url_for("onboarding.profile_edit"))

        uploaded = request.files.get("resume")
        if not uploaded or not uploaded.filename:
            return render_template(
                "onboarding/resume_upload.html",
                error="Please select a PDF or DOCX file, or click Skip.",
                **_step("resume_upload"),
            )

        # T-42-03: validate extension before any disk write
        filename = uploaded.filename
        ext = Path(filename).suffix.lower()
        if ext not in (".pdf", ".docx"):
            return render_template(
                "onboarding/resume_upload.html",
                error="Only .pdf and .docx files are supported.",
                **_step("resume_upload"),
            )

        # T-42-07: content-length check (10 MB cap)
        content_length = request.content_length
        if content_length is not None and content_length > _MAX_RESUME_BYTES:
            return render_template(
                "onboarding/resume_upload.html",
                error=f"File too large — maximum is {_MAX_RESUME_BYTES // (1024 * 1024)} MB.",
                **_step("resume_upload"),
            )

        # T-42-03: use Path(filename).suffix to inherit extension WITHOUT user-supplied basename
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name
                uploaded.save(tmp)
            # parse_resume returns dict; raises ValueError on unsupported extension (already screened)
            try:
                profile = resume_parser.parse_resume(
                    Path(tmp_path),
                    conn=_db(),
                    config=current_app.config.get("JF_CONFIG", {}),
                )
            except ValueError as e:
                logger.info(
                    "resume_parser rejected file (extension already screened?): %s",
                    type(e).__name__,
                )
                profile = {}
            except Exception as e:
                logger.warning("resume_parser failed: %s", type(e).__name__)
                profile = {}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("could not unlink resume temp file %s", tmp_path)

        # The user uploaded a file expecting autofill (the skills field promises
        # "auto-filled from your resume if uploaded"). If parsing yielded no skills,
        # flag it so profile_edit can surface a non-blocking notice instead of
        # silently rendering an empty form (Issue #397). The skip path writes an
        # empty profile too, but deliberately sets no flag — skipping is not a failure.
        resume_parse_failed = not (profile.get("skills") or [])
        state.write_wizard_data(
            _db(),
            {
                "resume_profile": profile,
                "resume_parse_failed": resume_parse_failed,
            },
        )
        return redirect(url_for("onboarding.profile_edit"))

    return render_template(
        "onboarding/resume_upload.html",
        **_step("resume_upload"),
    )


@onboarding_bp.route("/profile_edit", methods=["GET", "POST"], strict_slashes=False)
def profile_edit():
    """Step 4b: mirrors /profile form shape (D-06) - target_titles/target_locations/skills/min_salary only."""
    data = _wizard()
    existing_edit = data.get("profile_edit") or {}
    resume_profile = data.get("resume_profile") or {}

    if request.method == "POST":
        target_titles_raw = request.form.get("target_titles", "").strip()
        if not target_titles_raw:
            # Server-side guard: empty target_titles would produce a config.yaml that
            # fails validate_target_titles at every subsequent boot (Issue #299 fuse 2).
            return render_template(
                "onboarding/profile_edit.html",
                error="At least one target job title is required.",
                target_titles="",
                target_locations=request.form.get("target_locations", "").strip(),
                skills=request.form.get("skills", "").strip(),
                min_salary=request.form.get("min_salary", "").strip(),
                **_step("profile_edit"),
            )
        min_salary_raw = request.form.get("min_salary", "").strip()
        try:
            min_salary = int(min_salary_raw) if min_salary_raw else None
        except ValueError:
            min_salary = None
        slice_: dict = {
            "profile_edit": {
                "target_titles": target_titles_raw,
                "target_locations": request.form.get("target_locations", "").strip(),
                "skills": request.form.get("skills", "").strip(),
                "min_salary": min_salary,
            }
        }
        state.write_wizard_data(_db(), slice_)
        return redirect(url_for("onboarding.imap_credentials"))

    # Pre-fill skills from parsed resume if not already overridden by user (D-06)
    parsed_skills = resume_profile.get("skills") or []
    if isinstance(parsed_skills, list):
        parsed_skills_text = "\n".join(parsed_skills)
    else:
        parsed_skills_text = ""

    # If a resume was uploaded but parsing produced no skills, the autofill promise
    # went unmet — tell the user plainly rather than rendering an empty field with
    # no explanation (Issue #397). Suppress once the user has supplied their own
    # skills so the notice doesn't linger after they've moved on.
    resume_parse_failed = data.get("resume_parse_failed") and not (
        existing_edit.get("skills") or parsed_skills_text
    )
    notice = (
        "We couldn't read any skills from your resume — please enter them manually below."
        if resume_parse_failed
        else None
    )

    return render_template(
        "onboarding/profile_edit.html",
        target_titles=existing_edit.get("target_titles", ""),
        target_locations=existing_edit.get("target_locations", ""),
        skills=existing_edit.get("skills") or parsed_skills_text,
        min_salary=existing_edit.get("min_salary") or "",
        notice=notice,
        **_step("profile_edit"),
    )


@onboarding_bp.route("/imap_credentials", methods=["GET", "POST"], strict_slashes=False)
def imap_credentials():
    """Step 5: Gmail IMAP smoke test (D-08, D-09, Issue #289).

    Free portals (RemoteOK/Remotive/Himalayas) are always enabled — the toggle was
    removed from the wizard (Issue #402); the rare opt-out lives in Settings. This
    step persists ``sources.portal_search.enabled = True`` so the done step writes
    it into config regardless of the IMAP path taken. IMAP failure re-renders the
    page (HTTP 200) with error + preserved form.
    """
    data = _wizard()
    existing_imap = data.get("imap") or {}

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        app_password = request.form.get(
            "app_password", ""
        )  # do NOT strip - app passwords may include spaces
        skip = bool(request.form.get("skip"))

        # Free portals are on by default (Issue #402); persist regardless of the
        # IMAP path so the done step writes portal_search.enabled into config.
        state.write_wizard_data(
            _db(),
            {"sources": {"portal_search": {"enabled": True}}},
        )

        if skip:
            # D-08 escape hatch: user skipped IMAP setup — mark disabled so the
            # first ingest doesn't attempt to connect with empty/unverified credentials
            # and log spurious "sources.imap.email is required" errors (Issue #299 fuse 3).
            # Credentials are preserved in case the user wants to enable IMAP later
            # via Settings, but enabled is explicitly False until they do.
            state.write_wizard_data(
                _db(),
                {
                    "imap": {
                        "host": "imap.gmail.com",
                        "port": 993,
                        "email": email,
                        "app_password": app_password,
                        "folder": "INBOX",
                        "enabled": False,
                        "verified": False,
                    }
                },
            )
            return redirect(url_for("onboarding.schedule"))

        if not email or not app_password:
            return render_template(
                "onboarding/imap_credentials.html",
                error="Both Gmail address and app password are required.",
                email=email,
                **_step("imap_credentials"),
            )

        if not _is_valid_email(email):
            # Issue #399: reject a malformed address before the IMAP smoke test so
            # the user gets a clear format error instead of a cryptic connection one.
            return render_template(
                "onboarding/imap_credentials.html",
                error="That doesn't look like a valid email address.",
                email=email,
                **_step("imap_credentials"),
            )

        result = imap_test.check_imap(
            host="imap.gmail.com", port=993, email=email, app_password=app_password
        )
        if not result.ok:
            # D-08: re-render with error + preserved email; HTTP 200 (NOT 302)
            return render_template(
                "onboarding/imap_credentials.html",
                error=result.message,
                email=email,
                **_step("imap_credentials"),
            )

        state.write_wizard_data(
            _db(),
            {
                "imap": {
                    "host": "imap.gmail.com",
                    "port": 993,
                    "email": email,
                    "app_password": app_password,
                    "folder": "INBOX",
                    "enabled": True,
                    "verified": True,
                }
            },
        )
        return redirect(url_for("onboarding.schedule"))

    # Prefill the Gmail field: prefer an address the user already entered on this
    # step, otherwise fall back to the contact email lifted from their resume
    # (Issue #399). The IMAP address need not match the resume's, so this is only
    # a convenience default the user can overwrite.
    resume_profile = data.get("resume_profile") or {}
    prefill_email = existing_imap.get("email") or resume_profile.get("email", "")

    return render_template(
        "onboarding/imap_credentials.html",
        email=prefill_email,
        **_step("imap_credentials"),
    )


@onboarding_bp.route("/schedule", methods=["GET", "POST"], strict_slashes=False)
def schedule():
    """Step 6: cadence preset (D-12)."""
    if request.method == "POST":
        preset = request.form.get("cadence_preset", "standard")
        if preset not in ("light", "standard", "heavy"):
            preset = "standard"
        state.write_wizard_data(_db(), {"schedule": {"cadence_preset": preset}})
        return redirect(url_for("onboarding.done"))

    return render_template(
        "onboarding/schedule.html",
        **_step("schedule"),
    )


@onboarding_bp.route("/done", methods=["GET", "POST"], strict_slashes=False)
def done():
    """Step 7: review + Finish.

    POST executes FIVE side effects in strict sequence (D-16). Any failure at step N
    leaves steps N+1..5 un-run, producing a consistent 'not done' state:

        1. Atomic write config.yaml (provider + sources.imap + profile + scheduler.cadence_preset)
        2. Atomic write experience_profile.json
        3. mark_onboarding_complete (sets flag + clears wizard_data='{}')
        4. scheduler.add_job one-shot first ingest (D-17, id='wizard_first_ingest')
        5. flash banner + redirect to /jobs (T-42-05: internal endpoint only)
    """
    data = _wizard()

    if request.method == "POST":
        db = _db()

        # Re-entry guard: an already-completed onboarding submission (manual nav back
        # to /onboarding/welcome → walk wizard → POST /done, browser cache replay,
        # accidental refresh) would otherwise overwrite config.yaml with a slice
        # built from defaulted wizard_data. On 2026-05-18 a re-entry produced a
        # 16-line stub that wiped scoring/db/filters/output sections from a healthy
        # config; the only path to that exact stub state was an unguarded POST here.
        if state.is_onboarding_complete(db):
            flash(
                "Onboarding is already complete. Use the Settings page to change "
                "your configuration.",
                "warning",
            )
            return redirect(url_for("jobs.index"))

        wizard_data = state.read_wizard_data(db)

        # --- Build the config slice from wizard_data ---
        provider_block = wizard_data.get("provider") or {}
        imap_block = wizard_data.get("imap") or {}
        profile_edit = wizard_data.get("profile_edit") or {}
        schedule_block = wizard_data.get("schedule") or {"cadence_preset": "standard"}
        # Issue #289: portal_search toggle written by imap_credentials step.
        # Default True so a wizard completed entirely via skip paths still enables
        # free portals — the zero-key path works out-of-the-box.
        portal_search_block = (wizard_data.get("sources") or {}).get("portal_search") or {}
        portal_search_enabled = portal_search_block.get("enabled", True)

        # Profile fields are textareas separated by newlines (D-06)
        def _split_lines(s: str) -> list[str]:
            return [line.strip() for line in (s or "").splitlines() if line.strip()]

        # imap.enabled: prefer the value the IMAP step explicitly set (True on
        # successful test, False on Skip). Fall back to False — not True — so a
        # fresh-install wizard that never visited the IMAP step doesn't silently
        # attempt connections with empty credentials (Issue #299 fuse 3).
        imap_enabled = imap_block.get("enabled", False)

        # --- Side effect 1: atomic config.yaml write (D-15) ---
        # Load existing config BEFORE building the slice so we can gate the
        # scoring/db defaults on whether those sections are already present
        # (see Issue #299 fuse 1 below).
        #
        # When load_config raises ConfigError (e.g. validate_target_titles fails on
        # an existing-but-broken file), the previous except-Exception fallback set
        # existing_cfg = {} and the merge produced just the slice — wiping scoring,
        # db, filters, output, etc. Now: only fall back to {} when the file is
        # genuinely absent. If the file exists but failed validation, read raw YAML
        # so the merge preserves user data; an unvalidated merged write is far
        # better than a slice-only wipe.
        config_path = user_data_dirs.config_path()
        try:
            existing_cfg = load_config(str(config_path), allow_missing=True) or {}
        except ConfigNotFoundError:
            existing_cfg = {}
        except (ConfigError, ValueError) as e:
            logger.warning(
                "onboarding done: existing config failed validation (%s); "
                "reading raw YAML to preserve user data for merge",
                e,
            )
            try:
                with open(config_path, encoding="utf-8") as f:
                    existing_cfg = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                existing_cfg = {}

        # Issue #288: when the user chose the "skip — configure later" path,
        # provider.name is "none".  Omit providers.primary entirely so
        # tier_has_configured_provider returns False and the dashboard warning
        # fires — rather than pointing the cascade at "none" which would crash.
        _provider_name = provider_block.get("name", "anthropic")
        _providers_slice: dict = {}
        if _provider_name and _provider_name != "none":
            _providers_slice["primary"] = _provider_name

        config_slice: dict = {
            "providers": _providers_slice,
            "sources": {
                "imap": {
                    "enabled": imap_enabled,
                    "host": imap_block.get("host", "imap.gmail.com"),
                    "port": imap_block.get("port", 993),
                    "email": imap_block.get("email", ""),
                    "app_password": imap_block.get("app_password", ""),
                    "folder": imap_block.get("folder", "INBOX"),
                },
                # Issue #289: write portal toggle so the first ingest can fetch
                # RemoteOK/Remotive/Himalayas with no credentials.  Default True
                # when the user skipped the imap_credentials step entirely.
                "portal_search": {
                    "enabled": portal_search_enabled,
                },
            },
            "profile": {
                "target_titles": _split_lines(profile_edit.get("target_titles", "")),
                "target_locations": _split_lines(profile_edit.get("target_locations", "")),
                "skills": _split_lines(profile_edit.get("skills", "")),
            },
            "scheduler": {
                "cadence_preset": schedule_block.get("cadence_preset", "standard"),
            },
        }
        # Populate scoring and db defaults only when absent from the merge base
        # (fresh install, Issue #299 fuse 1).  Deep-merge replaces individual
        # scalar leaves, so unconditionally including them in the slice would
        # overwrite a user's existing settings (e.g. their custom daily_budget_usd).
        # By inserting only into a missing section we preserve every existing key
        # while still guaranteeing validate_required_sections passes on restart.
        if "scoring" not in existing_cfg:
            config_slice["scoring"] = {"daily_budget_usd": DEFAULT_DAILY_BUDGET_USD}
        if "db" not in existing_cfg:
            # Use the app's current DB_PATH so JOB_CANNON_USER_DATA_DIR overrides
            # and test fixtures aren't clobbered by a hardcoded "jobs.db" that
            # wouldn't match the running process's actual database.
            current_db_path = current_app.config.get("DB_PATH", "jobs.db")
            config_slice["db"] = {"path": str(current_db_path)}
        if profile_edit.get("min_salary"):
            config_slice["profile"]["min_salary"] = profile_edit["min_salary"]
        if provider_block.get("api_key"):
            # API key only attached for BYO-key providers (D-04)
            config_slice["providers"]["api_keys"] = {
                provider_block["name"]: provider_block["api_key"]
            }

        # --- Commit 3.6: route wizard secrets through the OS keyring ---
        # Per locked decision (Phase 1 #5a): plaintext lives in wizard_data
        # during the multi-step wizard; the keyring write only fires here, at
        # the final `done` step, atomically with the config.yaml side effect.
        # On RuntimeError (no keyring backend reachable) the plaintext flows
        # through to config.yaml as a fallback so the user doesn't lose the
        # secret — a flash warning informs them of the degradation.
        _move_secret_or_warn(
            config_slice,
            ("sources", "imap", "app_password"),
            "sources.imap.app_password",
        )
        provider_name = provider_block.get("name", "")
        if provider_name and provider_block.get("api_key"):
            canonical = f"providers.api_keys.{provider_name}"
            if canonical in jf_secrets.SECRET_ENV_VARS:
                _move_secret_or_warn(
                    config_slice,
                    ("providers", "api_keys", provider_name),
                    canonical,
                )

        merged_cfg = state._deep_merge(existing_cfg, config_slice)
        state._write_config(merged_cfg, config_path)  # atomic temp+rename (CLAUDE.md mandate)
        logger.info("onboarding done: wrote %s atomically", config_path)

        # --- Refresh the live in-memory config (Issue #300) ---
        # Boot sets JF_CONFIG = {} when no config.yaml exists.  Without this
        # refresh the one-shot first ingest (side effect 4) and every scheduler
        # job registered at boot would call get_config_snapshot() against the
        # empty dict, find no sources enabled, and ingest nothing — silently.
        # refresh_jf_config is the single point of enforcement shared with the
        # Settings save route so neither caller can diverge.
        app_obj = current_app._get_current_object()
        refresh_jf_config(app_obj, merged_cfg)

        # --- Side effect 2: atomic experience_profile.json write (D-16) ---
        resume_profile = wizard_data.get("resume_profile") or {}
        # Merge parsed positions/education/skills with user profile_edit overrides
        experience_profile: dict = dict(resume_profile)
        if profile_edit.get("target_titles"):
            experience_profile["target_titles"] = _split_lines(profile_edit["target_titles"])
        if profile_edit.get("target_locations"):
            experience_profile["target_locations"] = _split_lines(profile_edit["target_locations"])
        if profile_edit.get("skills"):
            # User-edited skills override parsed ones
            experience_profile["skills"] = _split_lines(profile_edit["skills"])
        if profile_edit.get("min_salary"):
            experience_profile["min_salary"] = profile_edit["min_salary"]

        profile_path = user_data_dirs.user_data_root() / "experience_profile.json"
        tmp_profile_path = profile_path.with_suffix(profile_path.suffix + ".tmp")
        try:
            with open(tmp_profile_path, "w", encoding="utf-8") as f:
                json.dump(experience_profile, f, indent=2, ensure_ascii=False)
            os.replace(tmp_profile_path, profile_path)
        except Exception:
            try:
                tmp_profile_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        logger.info("onboarding done: wrote %s atomically", profile_path)

        # --- Side effect 3: mark onboarding_complete + clear wizard_data (D-16) ---
        state.mark_onboarding_complete(db)
        logger.info("onboarding done: onboarding_complete=1, wizard_data cleared")

        # --- Side effect 4: one-shot first ingest via scheduler (D-17) ---
        # CRITICAL: APScheduler calls scheduled callables with NO arguments. run_ingestion's
        # signature is `run_ingestion(db_path, config, *, score=True)` — passing it directly
        # would crash with TypeError. Schedule a no-arg closure that mirrors the canonical
        # _jobs.py:register_ingestion.run_pipeline shape: capture `app` here (current_app
        # proxy expires after the response), open an app_context inside the closure, fetch
        # config + db_path, then invoke run_ingestion.
        # app_obj was already captured above (refresh_jf_config block) — reused here.
        scheduler = get_scheduler()
        if scheduler is not None:

            def _first_ingest():
                """One-shot wizard first-ingest closure (D-17).

                Mirrors job_finder/web/scheduler/_jobs.py:register_ingestion.run_pipeline.
                Deferred imports preserved per the project's scheduler idiom.
                """
                with app_obj.app_context():
                    from job_finder.web.db_helpers import get_config_snapshot
                    from job_finder.web.pipeline_runner import run_ingestion

                    cfg = get_config_snapshot(app_obj)
                    db_path = app_obj.config.get("DB_PATH", "jobs.db")
                    try:
                        run_ingestion(db_path, cfg)
                    except Exception as e:
                        logger.error("wizard_first_ingest run_ingestion failed: %s", e)

            try:
                scheduler.add_job(
                    _first_ingest,
                    trigger="date",
                    run_date=datetime.now() + timedelta(seconds=5),
                    id="wizard_first_ingest",
                    replace_existing=True,
                )
                logger.info("onboarding done: scheduled wizard_first_ingest (date+5s)")
            except Exception as e:
                # Do NOT fail the redirect if scheduling fails - user can manually trigger from Settings
                logger.warning("onboarding done: scheduler.add_job failed: %s", type(e).__name__)
        else:
            logger.info(
                "onboarding done: get_scheduler() returned None (likely TESTING=True) — skipping first ingest kickoff"
            )

        # --- Side effect 5: flash + redirect to /jobs (T-42-05: internal only) ---
        flash("First ingest in progress — check back in a minute.", "success")
        return redirect(url_for("jobs.index"))

    # GET handler (unchanged from plan 42-05)
    summary = {
        "provider": (data.get("provider") or {}).get("name", "—"),
        "email": (data.get("imap") or {}).get("email", "—"),
        "cadence": (data.get("schedule") or {}).get("cadence_preset", "—"),
        "target_titles": (data.get("profile_edit") or {}).get("target_titles", "—"),
    }
    return render_template(
        "onboarding/done.html",
        summary=summary,
        **_step("done"),
    )
