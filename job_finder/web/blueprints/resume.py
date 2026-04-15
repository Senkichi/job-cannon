"""Resume blueprint -- routes for resume generation and status polling.

Routes:
    POST /jobs/<path:dedup_key>/resume/generate
        Start background resume generation for a Sonnet-scored job.
        Returns _resume_generating.html partial with HTMX polling.

    GET /jobs/<path:dedup_key>/resume/status/<int:gen_id>
        Poll generation status. Returns appropriate partial based on status:
        - 'done':      _resume_done.html (no hx-trigger -- stops polling)
        - 'error':     _resume_error.html (no hx-trigger -- stops polling)
        - otherwise:   _resume_generating.html (hx-trigger every 600ms)

    POST /jobs/<path:dedup_key>/quick-apply
        One-click apply: generates resume if needed (synchronous), opens Drive
        doc + application URL in two tabs, sets pipeline_status to 'applied'.
        Returns _quick_apply_response.html with JS to open two tabs.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import quote_plus

from flask import Blueprint, current_app, render_template

from job_finder.config import DEFAULT_MODEL_SONNET
from job_finder.web.activity_tracker import log_activity, ACTION_GENERATE_RESUME, ACTION_QUICK_APPLY
from job_finder.web.blueprints import trigger_interview_prep_if_applied
from job_finder.web.db_helpers import get_db, standalone_connection
from job_finder.web.docx_formatter import build_resume_docx
from job_finder.web.drive_uploader import get_drive_service, upload_to_drive
from job_finder.web.profile_schema import load_profile
from job_finder.web.resume_generator import _generate_resume_background, generate_resume_single

logger = logging.getLogger(__name__)

resume_bp = Blueprint("resume", __name__, url_prefix="/jobs")

@resume_bp.route("/<path:dedup_key>/resume/generate", methods=["POST"], strict_slashes=False)
def generate(dedup_key: str):
    """Start background resume generation for a Sonnet-scored job.

    Requires that the job has a Sonnet score (sonnet_score IS NOT NULL).
    Inserts a pending resume_generations row, starts a daemon thread,
    and returns the polling fragment immediately.

    Returns 400 if job is not Sonnet-scored.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db(db_path)

    # Get job row
    from job_finder.db import get_job
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    # Guard: require Sonnet score
    if job.get("sonnet_score") is None:
        return "Job must be scored with Sonnet before generating a resume.", 400

    # Load profile
    profile_path = config.get("profile_path", "experience_profile.json")
    profile = load_profile(profile_path)

    # Build job_row dict for the background thread (sqlite3.Row is not picklable)
    job_row = dict(job)

    # Insert pending resume_generations row
    model = (
        config.get("scoring", {})
        .get("models", {})
        .get("sonnet", DEFAULT_MODEL_SONNET)
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cursor = conn.execute(
        "INSERT INTO resume_generations (job_id, generated_at, model, status, generation_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (dedup_key, now_str, model, "pending", "single"),
    )
    conn.commit()
    gen_id = cursor.lastrowid

    # Start background thread (daemon so it doesn't block app shutdown)
    t = threading.Thread(
        target=_generate_resume_background,
        args=(db_path, gen_id, job_row, profile, config),
        daemon=True,
    )
    t.start()

    try:
        log_activity(
            db_path,
            ACTION_GENERATE_RESUME,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "model": model,
                "status": "success",
            },
        )
    except Exception:
        pass

    return render_template(
        "jobs/_resume_generating.html",
        dedup_key=dedup_key,
        gen_id=gen_id,
    )

@resume_bp.route("/<path:dedup_key>/resume/status/<int:gen_id>", methods=["GET"], strict_slashes=False)
def status(dedup_key: str, gen_id: int):
    """Poll resume generation status.

    Returns the appropriate HTMX fragment based on current status:
    - 'done':    _resume_done.html (no hx-trigger -- stops polling)
    - 'error':   _resume_error.html (no hx-trigger -- stops polling)
    - otherwise: _resume_generating.html (hx-trigger every 600ms)
    """
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    row = conn.execute(
        "SELECT id, job_id, status, doc_url, error_msg, generated_at, model, validation_report "
        "FROM resume_generations WHERE id = ?",
        (gen_id,),
    ).fetchone()

    if row is None:
        return "Generation record not found.", 404

    gen_status = row["status"] if row["status"] else "pending"

    if gen_status == "done":
        return render_template(
            "jobs/_resume_done.html",
            dedup_key=dedup_key,
            gen_id=gen_id,
            doc_url=row["doc_url"],
            generated_at=row["generated_at"],
            validation_report=row["validation_report"],
        )

    if gen_status == "error":
        return render_template(
            "jobs/_resume_error.html",
            dedup_key=dedup_key,
            gen_id=gen_id,
            error_msg=row["error_msg"],
        )

    # Timeout safety net: auto-error if generating for >15 minutes
    if gen_status in ("pending", "generating") and row["generated_at"]:
        try:
            gen_at = datetime.fromisoformat(row["generated_at"])
            elapsed_min = (datetime.now(timezone.utc).replace(tzinfo=None) - gen_at).total_seconds() / 60
            if elapsed_min > 15:
                logger.warning("Resume gen %d timed out after %.1f min", gen_id, elapsed_min)
                conn.execute(
                    "UPDATE resume_generations SET status='error', error_msg=? WHERE id=? AND status IN ('pending', 'generating')",
                    ("Timed out (>15 min)", gen_id),
                )
                conn.commit()
                return render_template(
                    "jobs/_resume_error.html",
                    dedup_key=dedup_key,
                    gen_id=gen_id,
                    error_msg="Timed out (>15 min)",
                )
        except (ValueError, TypeError):
            pass

    # Still generating (pending or generating)
    return render_template(
        "jobs/_resume_generating.html",
        dedup_key=dedup_key,
        gen_id=gen_id,
    )

@resume_bp.route("/<path:dedup_key>/quick-apply", methods=["POST"], strict_slashes=False)
def quick_apply(dedup_key: str):
    """One-click apply: generate resume if needed, open tabs, set status to applied.

    Synchronous generation (blocking) -- this is an intentional UX trade-off.
    The user clicks once and waits up to 60s for the resume to be ready.
    On success, returns a response fragment that opens two browser tabs via JS
    (Drive doc + application URL) and shows confirmation with links.

    Returns 400 if the job lacks a Sonnet score.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db(db_path)

    # Get job context bundle
    from job_finder.db import load_job_context, update_pipeline_status
    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    # Guard: require Sonnet score
    if job.get("sonnet_score") is None:
        return "Job must be scored with Sonnet before using Quick Apply.", 400

    job_row = dict(job)

    # Check for an existing done resume from resume_history (already fetched by helper)
    existing = next(
        (r for r in ctx["resume_history"] if r["status"] == "done"),
        None,
    )

    if existing:
        doc_url = existing["doc_url"]
    else:
        # No resume yet -- generate synchronously
        profile_path = config.get("profile_path", "experience_profile.json")
        profile = load_profile(profile_path)

        # Open a direct connection (not g.db) -- this call may take 30-60s
        with standalone_connection(db_path) as direct_conn:

            resume_data = generate_resume_single(job_row, profile, direct_conn, config)

            if resume_data is None:
                # Budget exceeded
                return render_template(
                    "jobs/_resume_error.html",
                    dedup_key=dedup_key,
                    gen_id=None,
                    error_msg="Monthly budget exceeded -- cannot generate resume.",
                )

            # Format as .docx
            docx_buffer = build_resume_docx(resume_data)

            # Build document name
            company = job_row.get("company", "Unknown")
            title = job_row.get("title", "Resume")
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            doc_name = f"{company} - {title} - {date_str}"

            # Upload to Drive
            drive_service = get_drive_service()
            folder_id = config.get("drive", {}).get("folder_id", "")
            convert_to_gdoc = config.get("drive", {}).get("convert_to_gdoc", True)

            doc_url = upload_to_drive(
                drive_service,
                doc_name,
                docx_buffer,
                folder_id=folder_id,
                convert_to_gdoc=convert_to_gdoc,
            )

            # Insert done resume_generations row
            model = (
                config.get("scoring", {})
                .get("models", {})
                .get("sonnet", DEFAULT_MODEL_SONNET)
            )
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            direct_conn.execute(
                "INSERT INTO resume_generations "
                "(job_id, generated_at, model, status, doc_url, generation_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dedup_key, now_str, model, "done", doc_url, "single"),
            )
            direct_conn.commit()

    # Set pipeline_status to 'applied'
    update_pipeline_status(conn, dedup_key, "applied", source="quick_apply")

    # Trigger interview prep generation in background when status moves to "applied"
    trigger_interview_prep_if_applied(
        dedup_key,
        "applied",
        current_app.config["DB_PATH"],
        current_app.config.get("JF_CONFIG", {}),
        testing=current_app.config.get("TESTING", False),
    )

    # Build application URL
    source_urls_raw = job_row.get("source_urls", "[]")
    try:
        source_urls = json.loads(source_urls_raw) if isinstance(source_urls_raw, str) else source_urls_raw
    except (json.JSONDecodeError, TypeError):
        source_urls = []

    if source_urls:
        app_url = source_urls[0]
    else:
        # Google search fallback
        company = job_row.get("company", "")
        title = job_row.get("title", "")
        query = quote_plus(f"{company} {title} careers")
        app_url = f"https://www.google.com/search?q={query}"

    try:
        log_activity(
            db_path,
            ACTION_QUICK_APPLY,
            entity_id=dedup_key,
            metadata={
                "title": job_row.get("title"),
                "company": job_row.get("company"),
                "doc_url": doc_url,
                "app_url": app_url,
                "status": "success",
            },
        )
    except Exception:
        pass

    return render_template(
        "jobs/_quick_apply_response.html",
        dedup_key=dedup_key,
        doc_url=doc_url,
        app_url=app_url,
        job=job_row,
    )
