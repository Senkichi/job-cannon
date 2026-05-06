"""Jobs blueprint -- full Job Board routes with HTMX partials."""

import logging
import time as _time
from datetime import UTC, datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.db import (
    get_distinct_locations,
    get_distinct_sources,
    get_filtered_jobs,
    get_job,
    get_pipeline_events,
    load_job_context,
    update_pipeline_status,
)
from job_finder.web.activity_tracker import (
    ACTION_EXPAND_JOB,
    ACTION_PASTE_JD,
    ACTION_RESCORE,
    ACTION_SAVE_JD,
    ACTION_STATUS_CHANGE,
    log_activity,
)


def _get_stale_count(conn) -> int:
    """Return count of jobs with is_stale = 1."""
    row = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_stale = 1").fetchone()
    return row[0] if row else 0


from job_finder.web.blueprints import PIPELINE_STATUSES, trigger_interview_prep_if_applied
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")


def _safe_float(raw: str, param_name: str) -> float | None:
    """Coerce a query-string value to float, or abort 400 on malformed input."""
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        abort(400, description=f"Invalid value for {param_name}: {raw!r}")


def _safe_int(raw: str, param_name: str) -> int | None:
    """Coerce a query-string value to int, or abort 400 on malformed input."""
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        abort(400, description=f"Invalid value for {param_name}: {raw!r}")


def _get_filter_kwargs() -> dict:
    """Extract and coerce filter query parameters from request.args."""
    args = request.args

    # Multi-select status: getlist returns [] when absent, or [""] for a blank submit
    statuses = [s for s in args.getlist("status") if s]

    return {
        "status": statuses if len(statuses) > 1 else (statuses[0] if statuses else None),
        "location": args.get("location") or None,
        "min_score": _safe_float(args.get("min_score", ""), "min_score"),
        "max_score": _safe_float(args.get("max_score", ""), "max_score"),
        "salary_min": _safe_int(args.get("salary_min", ""), "salary_min"),
        "source": args.get("source") or None,
        "posted_within": args.get("posted_within") or None,
        "freshness": args.get("freshness") or None,
        "date_from": args.get("date_from") or None,
        "date_to": args.get("date_to") or None,
        "sort_by": args.get("sort_by", "score"),
        "sort_dir": args.get("sort_dir", "DESC"),
        "limit": 200,
        "hide_stale": args.get("hide_stale") == "on" if args else True,
        "show_hidden": args.get("show_hidden") == "on",
    }


def relative_date(iso_str):
    """Format date as 'Mar 3 (1w ago)' — absolute then relative.

    Per locked user decision: format MUST be 'Mar 3 (1w ago)'
    (absolute date then relative in parentheses).
    """
    if not iso_str:
        return "---"
    try:
        dt = datetime.fromisoformat(iso_str[:19])
    except (ValueError, TypeError):
        return iso_str[:10] if iso_str else "---"

    # Absolute part: "Mar 3" — handle Windows (%#d) vs Unix (%-d)
    try:
        abs_part = dt.strftime("%b %-d")
    except ValueError:
        abs_part = dt.strftime("%b %#d")

    now = datetime.now()
    delta = now - dt
    days = delta.days

    if days < 0:
        rel = "future"
    elif days == 0:
        rel = "today"
    elif days == 1:
        rel = "1d ago"
    elif days < 7:
        rel = f"{days}d ago"
    elif days < 30:
        weeks = days // 7
        rel = f"{weeks}w ago"
    elif days < 365:
        months = days // 30
        rel = f"{months}mo ago"
    else:
        years = days // 365
        rel = f"{years}y ago"

    return f"{abs_part} ({rel})"


@jobs_bp.record_once
def _register_filters(state):
    """Register the relative_date Jinja2 filter when blueprint is registered."""
    state.app.jinja_env.filters["relative_date"] = relative_date


@jobs_bp.route("/", strict_slashes=False)
def index():
    """Job Board landing page -- full page render with filter bar."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    filters = _get_filter_kwargs()
    jobs = get_filtered_jobs(conn, **filters)
    locations = get_distinct_locations(conn)
    sources = get_distinct_sources(conn)
    stale_count = _get_stale_count(conn)
    archived_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
    ).fetchone()[0]
    hidden_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status IN ('archived', 'withdrawn', 'dismissed', 'rejected')"
    ).fetchone()[0]

    return render_template(
        "jobs/index.html",
        jobs=jobs,
        filters=request.args,
        pipeline_statuses=PIPELINE_STATUSES,
        locations=locations,
        sources=sources,
        stale_count=stale_count,
        archived_count=archived_count,
        hidden_count=hidden_count,
    )


@jobs_bp.route("/table", strict_slashes=False)
def table():
    """HTMX partial -- returns only the table body rows (no full page)."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    filters = _get_filter_kwargs()
    jobs = get_filtered_jobs(conn, **filters)

    return render_template(
        "jobs/_table.html",
        jobs=jobs,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/archived-table", strict_slashes=False)
def archived_table():
    """HTMX partial -- archived job rows for the collapsible section."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    jobs = get_filtered_jobs(
        conn, status="archived", sort_by="first_seen", sort_dir="DESC", limit=200
    )
    return render_template(
        "jobs/_table.html",
        jobs=jobs,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/<path:dedup_key>/expand", strict_slashes=False)
def expand(dedup_key: str):
    """HTMX partial -- returns accordion expansion row for a job."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]
    prep_row = ctx["prep_row"]

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_EXPAND_JOB,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in expand", exc_info=True)

    return render_template(
        "jobs/_row_expanded.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
        prep_row=prep_row,
    )


@jobs_bp.route("/<path:dedup_key>/collapse", strict_slashes=False)
def collapse(dedup_key: str):
    """HTMX partial -- returns hidden placeholder <tr> to restore pre-expansion DOM state."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    return render_template(
        "jobs/_row_collapse_response.html",
        job=job,
    )


@jobs_bp.route("/<path:dedup_key>/status", methods=["POST"], strict_slashes=False)
def update_status(dedup_key: str):
    """HTMX POST -- change pipeline status and return updated status cell."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    new_status = request.form.get("pipeline_status", "")
    if new_status not in PIPELINE_STATUSES:
        return "Invalid status", 400

    # Capture old status before update for activity metadata
    old_job = get_job(conn, dedup_key)
    old_status = old_job.get("pipeline_status") if old_job else None

    update_pipeline_status(conn, dedup_key, new_status, source="manual")

    # Trigger interview prep generation in background when status moves to "applied"
    trigger_interview_prep_if_applied(
        dedup_key,
        new_status,
        current_app.config["DB_PATH"],
        current_app.config.get("JF_CONFIG", {}),
        testing=current_app.config.get("TESTING", False),
    )

    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_STATUS_CHANGE,
            entity_id=dedup_key,
            metadata={
                "old_status": old_status,
                "new_status": new_status,
                "title": (old_job.get("title") if old_job else None),
                "company": (old_job.get("company") if old_job else None),
            },
        )
    except Exception:
        logger.debug("log_activity failed in update_status", exc_info=True)

    status_html = render_template(
        "jobs/_status_cell.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
    )

    if new_status == "archived":
        # OOB update: refresh the archived count badge.
        # Do NOT set HX-Trigger: jobs-updated — it causes tbody refetch
        # that kills the in-flight archive fadeout animation.
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
        ).fetchone()[0]
        oob_counter = f'<span id="archived-count" hx-swap-oob="innerHTML">{archived_count}</span>'
        resp = make_response(status_html + oob_counter)
    elif new_status in ("dismissed", "withdrawn", "rejected"):
        # Trigger table re-fetch so the row disappears from the filtered view.
        # Unlike archived (which has a fadeout animation), these statuses
        # simply remove the row immediately.
        resp = make_response(status_html)
        resp.headers["HX-Trigger-After-Settle"] = "jobs-updated"
    else:
        resp = make_response(status_html)

    return resp


@jobs_bp.route("/<path:dedup_key>/detail-inline", strict_slashes=False)
def detail_inline(dedup_key: str):
    """HTMX partial -- returns full detail as inline table row."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    events = get_pipeline_events(conn, dedup_key)
    return render_template(
        "jobs/_row_detail.html",
        job=job,
        events=events,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/<path:dedup_key>/paste-jd", methods=["POST"], strict_slashes=False)
def paste_jd(dedup_key: str):
    """HTMX POST -- accept pasted JD text, store it, trigger Sonnet eval.

    Stores jd_text in jobs.jd_full, then calls evaluate_job_sonnet.
    Budget-gated via cost_gate. Returns updated expanded row partial.
    """
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="Please provide a job description.",
            prep_row=ctx["prep_row"],
        )

    # Cap at 8000 chars — same limit applied by upsert_job during ingestion.
    jd_text = jd_text[:8000]

    # Store the JD text
    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (jd_text, dedup_key),
    )
    conn.commit()

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_PASTE_JD,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "jd_length": len(jd_text),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in paste_jd", exc_info=True)

    # Attempt v3 unified scoring (budget-gated)
    error = None
    try:
        from job_finder.web.claude_client import cost_gate
        from job_finder.web.scoring_orchestrator import score_and_persist_job

        config = current_app.config.get("JF_CONFIG", {})
        if cost_gate(conn, config, "scoring"):
            # Refresh job row with jd_full
            job = get_job(conn, dedup_key)
            score_and_persist_job(job, conn, config)
        else:
            logger.info("paste-jd: budget cap reached, scoring skipped for %s", dedup_key)
            error = "Budget cap reached. Scoring skipped."

    except ImportError as e:
        logger.warning("paste-jd: scorer not available: %s", e)
        error = "Scoring unavailable. JD saved for later."
    except Exception as e:
        logger.error("paste-jd: scoring failed for %s: %s", dedup_key, e)
        error = "Re-scoring failed. Try again later."

    # Return updated expanded row + OOB score cell (updates compact row in-place)
    ctx = load_job_context(conn, dedup_key)
    expanded = render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        error=error,
        prep_row=ctx["prep_row"],
    )
    oob_score = render_template("jobs/_score_cell.html", job=ctx["job"], oob=True)
    return make_response(expanded + "<template>" + oob_score + "</template>")


@jobs_bp.route("/<path:dedup_key>/rescore", methods=["POST"], strict_slashes=False)
def rescore(dedup_key: str):
    """HTMX POST -- re-trigger Sonnet evaluation for a job that already has jd_full.

    Returns updated expanded row partial.
    """
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    if not job.get("jd_full"):
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="No JD available for re-scoring. Paste a JD first.",
            prep_row=ctx["prep_row"],
        )

    # Capture old classification before re-evaluation
    old_classification = job.get("classification")

    # Attempt v3 re-evaluation (budget-gated)
    error = None
    t0 = _time.time()
    try:
        from job_finder.web.claude_client import cost_gate
        from job_finder.web.scoring_orchestrator import score_and_persist_job

        config = current_app.config.get("JF_CONFIG", {})
        if cost_gate(conn, config, "scoring"):
            result = score_and_persist_job(job, conn, config)
            if result and getattr(result, "status", None) == "ok":
                # Re-query to get the persisted classification (derived
                # at persist time from sub_scores + legitimacy_note).
                refreshed = get_job(conn, dedup_key)
                new_classification = (refreshed or {}).get("classification")
                try:
                    log_activity(
                        db_path,
                        ACTION_RESCORE,
                        entity_id=dedup_key,
                        metadata={
                            "old_classification": old_classification,
                            "new_classification": new_classification,
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception:
                    pass
        else:
            logger.info("rescore: budget cap reached, scoring skipped for %s", dedup_key)
            error = "Budget cap reached. Scoring skipped."

    except ImportError as e:
        logger.warning("rescore: scorer not available: %s", e)
        error = "Re-scoring failed. Try again later."
        try:
            log_activity(
                db_path,
                ACTION_RESCORE,
                entity_id=dedup_key,
                metadata={
                    "status": "failed",
                    "error": "ImportError",
                    "duration_seconds": round(_time.time() - t0, 2),
                },
            )
        except Exception:
            pass
    except Exception as e:
        logger.error("rescore: scoring failed for %s: %s", dedup_key, e)
        error = "Re-scoring failed. Try again later."
        try:
            log_activity(
                db_path,
                ACTION_RESCORE,
                entity_id=dedup_key,
                metadata={
                    "status": "failed",
                    "error": type(e).__name__,
                    "duration_seconds": round(_time.time() - t0, 2),
                },
            )
        except Exception:
            pass

    ctx = load_job_context(conn, dedup_key)
    expanded = render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        error=error,
        prep_row=ctx["prep_row"],
    )
    oob_score = render_template("jobs/_score_cell.html", job=ctx["job"], oob=True)
    return make_response(expanded + "<template>" + oob_score + "</template>")


@jobs_bp.route("/<path:dedup_key>/score-cell", strict_slashes=False)
def score_cell(dedup_key: str):
    """HTMX partial -- returns just the score <td> for a single job."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    return render_template("jobs/_score_cell.html", job=job)


@jobs_bp.route("/<path:dedup_key>/interview-prep/status", strict_slashes=False)
def interview_prep_status(dedup_key: str):
    """HTMX poll endpoint -- returns current interview prep state for a job.

    Called every 2s by hx-trigger="every 2s" in _interview_prep_generating.html.
    Returns:
    - _interview_prep_generating.html (with hx-trigger) while status='generating'
    - _interview_prep.html (static, no hx-trigger -- stops polling) when status='done'
    - error fragment when status='error'
    - empty string (200) if no prep row exists yet
    """
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    prep_row = conn.execute(
        "SELECT id, status, company_brief, predicted_questions, gap_mitigation, "
        "questions_to_ask, error_msg, generated_at "
        "FROM interview_preps WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (dedup_key,),
    ).fetchone()

    if prep_row is None:
        return "", 200

    status = prep_row["status"] if prep_row else None

    # Timeout safety net: auto-error if generating for >15 minutes
    if status == "generating":
        try:
            generated_at = datetime.fromisoformat(dict(prep_row).get("generated_at", ""))
            elapsed_min = (
                datetime.now(UTC).replace(tzinfo=None) - generated_at
            ).total_seconds() / 60
            if elapsed_min > 15:
                logger.warning(
                    "Interview prep for %s timed out after %.1f min", dedup_key, elapsed_min
                )
                conn.execute(
                    "UPDATE interview_preps SET status='error', error_msg=? WHERE id=? AND status='generating'",
                    ("Timed out (>15 min)", prep_row["id"]),
                )
                conn.commit()
                return (
                    '<div class="text-xs text-red-400 p-3">Interview prep error: Timed out (&gt;15 min)</div>',
                    200,
                )
        except (ValueError, TypeError, KeyError):
            pass

        return render_template(
            "jobs/_interview_prep_generating.html",
            job=job,
        )
    elif status == "done":
        return render_template(
            "jobs/_interview_prep.html",
            job=job,
            prep=prep_row,
        )
    elif status == "error":
        error_msg = prep_row["error_msg"] or "Interview prep failed."
        return (
            f'<div class="text-xs text-red-400 p-3">Interview prep error: {error_msg}</div>',
            200,
        )

    return "", 200


@jobs_bp.route("/<path:dedup_key>/save-jd", methods=["POST"], strict_slashes=False)
def save_jd(dedup_key: str):
    """HTMX POST -- save jd_full without triggering scoring."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]
    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="Please provide a job description.",
            prep_row=ctx["prep_row"],
        )

    # Cap at 8000 chars — same limit applied by upsert_job during ingestion.
    jd_text = jd_text[:8000]

    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (jd_text, dedup_key),
    )
    conn.commit()

    try:
        log_activity(
            db_path,
            ACTION_SAVE_JD,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "jd_length": len(jd_text),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in save_jd", exc_info=True)

    ctx = load_job_context(conn, dedup_key)
    return render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        jd_saved=True,
        prep_row=ctx["prep_row"],
    )


@jobs_bp.route("/<path:dedup_key>/jd-edit-form", strict_slashes=False)
def jd_edit_form(dedup_key: str):
    """HTMX GET -- return the JD paste form pre-filled with existing jd_full."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    return render_template("jobs/_jd_edit_form.html", job=job)


@jobs_bp.route("/<path:dedup_key>", strict_slashes=False)
def detail(dedup_key: str):
    """Full job detail page at /jobs/<dedup_key>."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    job = get_job(conn, dedup_key)
    if job is None:
        return render_template("jobs/detail.html", job=None), 404

    events = get_pipeline_events(conn, dedup_key)

    return render_template(
        "jobs/detail.html",
        job=job,
        events=events,
        pipeline_statuses=PIPELINE_STATUSES,
    )
