"""Onboarding wizard blueprint (Phase 42, STRANGE-WIZ-02/05).

Eight routes covering the 7 conceptual wizard steps:
    welcome (1), provider_select (2), provider_credentials (3),
    resume_upload + profile_edit (4), imap_credentials (5),
    schedule (6), done (7).

Step indicator (D-21): each route passes step_num + step_label to _base.html.

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

from job_finder.config import ConfigError, ConfigNotFoundError, load_config
from job_finder.web import user_data_dirs
from job_finder.web.db_helpers import get_db
from job_finder.web.onboarding import imap_test, resume_parser, state, system_check
from job_finder.web.providers.detection import detect_available_providers
from job_finder.web.scheduler import get_scheduler

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

# T-42-07 / V12: cap resume upload at 10 MB. Enforced by checking Content-Length manually
# in the resume_upload handler because Flask's MAX_CONTENT_LENGTH is app-level, not
# blueprint-level, and modifying app-level would break other blueprints' uploads.
_MAX_RESUME_BYTES: Final[int] = 10 * 1024 * 1024

# $0 CLIs that need no credentials at the provider_credentials step (D-04)
_NO_CREDS_PROVIDERS: Final[frozenset[str]] = frozenset({"claude_code_cli", "gemini_cli", "ollama"})


# --- Helpers ---

def _db():
    return get_db(current_app.config["DB_PATH"])


def _wizard():
    """Read wizard_data from DB - convenience for handlers."""
    return state.read_wizard_data(_db())


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
        step_num=1,
        step_label="Welcome",
    )


@onboarding_bp.route("/provider_select", methods=["GET", "POST"], strict_slashes=False)
def provider_select():
    """Step 2: render detected providers + Re-detect button (D-01, D-02)."""
    if request.method == "POST":
        if request.form.get("redetect"):
            # D-02: refresh detection cache
            detect_available_providers(refresh=True)
            return redirect(url_for("onboarding.provider_select"))
        provider_name = request.form.get("provider_name", "").strip()
        if not provider_name:
            flash("Please choose a provider to continue.", "error")
            return redirect(url_for("onboarding.provider_select"))
        state.write_wizard_data(_db(), {"provider": {"name": provider_name}})
        return redirect(url_for("onboarding.provider_credentials"))

    providers = detect_available_providers()
    return render_template(
        "onboarding/provider_select.html",
        providers=providers,
        step_num=2,
        step_label="AI provider",
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
        step_num=3,
        step_label="Credentials",
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
            state.write_wizard_data(_db(), {"resume_profile": {}})
            return redirect(url_for("onboarding.profile_edit"))

        uploaded = request.files.get("resume")
        if not uploaded or not uploaded.filename:
            return render_template(
                "onboarding/resume_upload.html",
                error="Please select a PDF or DOCX file, or click Skip.",
                step_num=4,
                step_label="Resume",
            )

        # T-42-03: validate extension before any disk write
        filename = uploaded.filename
        ext = Path(filename).suffix.lower()
        if ext not in (".pdf", ".docx"):
            return render_template(
                "onboarding/resume_upload.html",
                error="Only .pdf and .docx files are supported.",
                step_num=4,
                step_label="Resume",
            )

        # T-42-07: content-length check (10 MB cap)
        content_length = request.content_length
        if content_length is not None and content_length > _MAX_RESUME_BYTES:
            return render_template(
                "onboarding/resume_upload.html",
                error=f"File too large — maximum is {_MAX_RESUME_BYTES // (1024 * 1024)} MB.",
                step_num=4,
                step_label="Resume",
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
                logger.info("resume_parser rejected file (extension already screened?): %s", type(e).__name__)
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

        state.write_wizard_data(_db(), {"resume_profile": profile})
        return redirect(url_for("onboarding.profile_edit"))

    return render_template(
        "onboarding/resume_upload.html",
        step_num=4,
        step_label="Resume",
    )


@onboarding_bp.route("/profile_edit", methods=["GET", "POST"], strict_slashes=False)
def profile_edit():
    """Step 4b: mirrors /profile form shape (D-06) - target_titles/target_locations/skills/min_salary only."""
    data = _wizard()
    existing_edit = data.get("profile_edit") or {}
    resume_profile = data.get("resume_profile") or {}

    if request.method == "POST":
        min_salary_raw = request.form.get("min_salary", "").strip()
        try:
            min_salary = int(min_salary_raw) if min_salary_raw else None
        except ValueError:
            min_salary = None
        slice_: dict = {
            "profile_edit": {
                "target_titles": request.form.get("target_titles", "").strip(),
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

    return render_template(
        "onboarding/profile_edit.html",
        target_titles=existing_edit.get("target_titles", ""),
        target_locations=existing_edit.get("target_locations", ""),
        skills=existing_edit.get("skills") or parsed_skills_text,
        min_salary=existing_edit.get("min_salary") or "",
        step_num=4,
        step_label="Profile",
    )


@onboarding_bp.route("/imap_credentials", methods=["GET", "POST"], strict_slashes=False)
def imap_credentials():
    """Step 5: Gmail IMAP smoke test (D-08, D-09). Failure re-renders the page (HTTP 200) with error + preserved form."""
    data = _wizard()
    existing_imap = data.get("imap") or {}

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        app_password = request.form.get("app_password", "")  # do NOT strip - app passwords may include spaces
        skip = bool(request.form.get("skip"))

        if skip:
            # D-08 escape hatch: save credentials anyway (even if test would fail), mark not-verified
            state.write_wizard_data(_db(), {
                "imap": {
                    "host": "imap.gmail.com",
                    "port": 993,
                    "email": email,
                    "app_password": app_password,
                    "folder": "INBOX",
                    "enabled": True,
                    "verified": False,
                }
            })
            return redirect(url_for("onboarding.schedule"))

        if not email or not app_password:
            return render_template(
                "onboarding/imap_credentials.html",
                error="Both Gmail address and app password are required.",
                email=email,
                step_num=5,
                step_label="Gmail",
            )

        result = imap_test.check_imap(host="imap.gmail.com", port=993, email=email, app_password=app_password)
        if not result.ok:
            # D-08: re-render with error + preserved email; HTTP 200 (NOT 302)
            return render_template(
                "onboarding/imap_credentials.html",
                error=result.message,
                email=email,
                step_num=5,
                step_label="Gmail",
            )

        state.write_wizard_data(_db(), {
            "imap": {
                "host": "imap.gmail.com",
                "port": 993,
                "email": email,
                "app_password": app_password,
                "folder": "INBOX",
                "enabled": True,
                "verified": True,
            }
        })
        return redirect(url_for("onboarding.schedule"))

    return render_template(
        "onboarding/imap_credentials.html",
        email=existing_imap.get("email", ""),
        step_num=5,
        step_label="Gmail",
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
        step_num=6,
        step_label="Schedule",
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

        # Profile fields are textareas separated by newlines (D-06)
        def _split_lines(s: str) -> list[str]:
            return [line.strip() for line in (s or "").splitlines() if line.strip()]

        config_slice: dict = {
            "providers": {
                "primary": provider_block.get("name", "anthropic"),
            },
            "sources": {
                "imap": {
                    "enabled": imap_block.get("enabled", True),
                    "host": imap_block.get("host", "imap.gmail.com"),
                    "port": imap_block.get("port", 993),
                    "email": imap_block.get("email", ""),
                    "app_password": imap_block.get("app_password", ""),
                    "folder": imap_block.get("folder", "INBOX"),
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
        if profile_edit.get("min_salary"):
            config_slice["profile"]["min_salary"] = profile_edit["min_salary"]
        if provider_block.get("api_key"):
            # API key only attached for BYO-key providers (D-04)
            config_slice["providers"]["api_keys"] = {provider_block["name"]: provider_block["api_key"]}

        # --- Side effect 1: atomic config.yaml write (D-15) ---
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
        merged_cfg = state._deep_merge(existing_cfg, config_slice)
        state._write_config(merged_cfg, config_path)  # atomic temp+rename (CLAUDE.md mandate)
        logger.info("onboarding done: wrote %s atomically", config_path)

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
        scheduler = get_scheduler()
        if scheduler is not None:
            # Capture the concrete Flask app BEFORE the request returns. The `current_app`
            # proxy is request-scoped and will raise outside the request context.
            app_obj = current_app._get_current_object()

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
            logger.info("onboarding done: get_scheduler() returned None (likely TESTING=True) — skipping first ingest kickoff")

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
        step_num=7,
        step_label="Ready",
    )
