"""Admin blueprint — runtime control of the in-process APScheduler.

Endpoints (all return JSON):
    GET  /admin/jobs                 -- list scheduled jobs with state + next run
    POST /admin/jobs/<id>/pause      -- pause a job (won't fire until resumed)
    POST /admin/jobs/<id>/resume     -- resume a paused job

There is no UI; hit these with curl/HTMX from the dashboard if needed. The
scheduler is in-process, so these routes only affect the Flask worker that
serves the request -- which is also the only worker (single-process app).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC

from flask import Blueprint, current_app, jsonify, render_template, request

from job_finder.web.backfill_direct_links import backfill_direct_links
from job_finder.web.db_helpers import get_db
from job_finder.web.scheduler import get_scheduler

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _scheduler_or_503():
    sched = get_scheduler()
    if sched is None:
        return None, (jsonify({"error": "scheduler not running"}), 503)
    return sched, None


@admin_bp.route("/jobs", methods=["GET"], strict_slashes=False)
def list_jobs():
    sched, err = _scheduler_or_503()
    if err:
        return err

    jobs = []
    for job in sched.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "paused": job.next_run_time is None,
                "trigger": str(job.trigger),
            }
        )
    return jsonify({"jobs": jobs})


@admin_bp.route("/jobs/<job_id>/pause", methods=["POST"], strict_slashes=False)
def pause_job(job_id: str):
    sched, err = _scheduler_or_503()
    if err:
        return err

    if sched.get_job(job_id) is None:
        return jsonify({"error": f"no such job: {job_id}"}), 404

    sched.pause_job(job_id)
    logger.warning("Admin: paused scheduler job %s", job_id)
    return jsonify({"id": job_id, "paused": True})


@admin_bp.route("/jobs/<job_id>/resume", methods=["POST"], strict_slashes=False)
def resume_job(job_id: str):
    sched, err = _scheduler_or_503()
    if err:
        return err

    if sched.get_job(job_id) is None:
        return jsonify({"error": f"no such job: {job_id}"}), 404

    sched.resume_job(job_id)
    job = sched.get_job(job_id)
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    logger.warning("Admin: resumed scheduler job %s (next run %s)", job_id, next_run)
    return jsonify({"id": job_id, "paused": False, "next_run_time": next_run})


def _job_currently_running(sched, job_id: str) -> bool:
    """Return True if an instance of *job_id* is currently executing.

    APScheduler 3.x tracks per-job concurrent-instance counts in each
    executor's ``_instances`` defaultdict (BaseExecutor.submit_job increments
    on submit, decrements on completion). This is the same dict that enforces
    ``max_instances`` server-side, so reading it gives us the canonical
    answer. The attribute is internal but stable across 3.x; we fall back to
    "not running" if APScheduler changes shape, which preserves the legacy
    fire-and-hope behaviour rather than crashing the admin endpoint.
    """
    try:
        for executor in sched._executors.values():
            if executor._instances.get(job_id, 0) > 0:
                return True
    except (AttributeError, KeyError):
        return False
    return False


@admin_bp.route("/jobs/<job_id>/run-now", methods=["POST"], strict_slashes=False)
def run_job_now(job_id: str):
    """Trigger immediate execution by setting next_run_time to now.

    Pre-checks the executor's running-instance count; if an instance of the
    same job is already in flight, refuses with 409 instead of stacking a
    parallel run. Two concurrent run-now POSTs against a long-running job
    (e.g. ``reconcile_all_companies``) was the root cause of the writer-lock
    starvation regression chased in rev 6→8 (see ``.planning/NEXT_STEPS``).
    """
    from datetime import datetime

    sched, err = _scheduler_or_503()
    if err:
        return err

    job = sched.get_job(job_id)
    if job is None:
        return jsonify({"error": f"no such job: {job_id}"}), 404

    if _job_currently_running(sched, job_id):
        logger.info("Admin: run-now for %s rejected — instance already running", job_id)
        return (
            jsonify({"id": job_id, "triggered": False, "reason": "already running"}),
            409,
        )

    job.modify(next_run_time=datetime.now(UTC))
    logger.warning("Admin: triggered immediate run of %s", job_id)
    return jsonify({"id": job_id, "triggered": True})


# ---------------------------------------------------------------------------
# Unresolved-job triage (Phase 47.06 / 47.07)
#
# /admin/review surfaces rows flagged for manual review — those carrying
# non-empty unresolved_reasons (m078 column) or an unresolved structured
# location. A reviewer either approves (clears the flags, row re-enters the
# normal listing) or drops (sets pipeline_status='rejected'). Both append a
# brief audit line to the existing notes field; a dedicated audit table is a
# future phase (§8.4).
# ---------------------------------------------------------------------------


def _clear_location_unresolved(raw: str | None) -> str | None:
    """Return locations_structured JSON with every ``unresolved`` flag cleared.

    Falsy / unparseable / non-list input is returned unchanged — there is
    nothing to clear and we never want to corrupt an existing value.
    """
    if not raw:
        return raw
    try:
        locs = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(locs, list):
        return raw
    cleared = []
    for loc in locs:
        if isinstance(loc, dict):
            cleared.append({**loc, "unresolved": False})
        else:
            cleared.append(loc)
    return json.dumps(cleared)


def _append_note(existing: str | None, line: str) -> str:
    """Append an audit line to the notes field, newline-separated."""
    existing = (existing or "").rstrip()
    return f"{existing}\n{line}".lstrip() if existing else line


def _audit_line(action: str, reasons_json: str) -> str:
    """Compose a timestamped audit line for the notes field."""
    from job_finder.json_utils import utc_now_iso

    try:
        reasons = json.loads(reasons_json) if reasons_json else []
    except (json.JSONDecodeError, TypeError):
        reasons = []
    summary = ", ".join(reasons) if reasons else "(none)"
    return f"{utc_now_iso()} {action}: {summary}"


@admin_bp.route("/review", methods=["GET"], strict_slashes=False)
def review():
    """Triage page listing rows flagged for manual review.

    Direct browser GET returns the full page; an HTMX GET returns just the
    table fragment (per CLAUDE.md HX-Request convention). show_hidden=True so
    already-dropped (rejected) rows aren't silently excluded before the
    reviewer sees them; the unresolved="only" filter does the real scoping.
    """
    from job_finder.db import get_filtered_jobs
    from job_finder.web.db_helpers import get_db

    conn = get_db()
    jobs = get_filtered_jobs(
        conn,
        unresolved="only",
        show_hidden=True,
        sort_by="first_seen",
        sort_dir="DESC",
        limit=500,
    )
    if request.headers.get("HX-Request"):
        return render_template("admin/_review_rows.html", jobs=jobs)
    return render_template("admin/review.html", jobs=jobs)


@admin_bp.route("/review/<dedup_key>/approve", methods=["POST"], strict_slashes=False)
def approve_review(dedup_key: str):
    """Clear a row's unresolved flags so it re-enters the normal listing."""
    from job_finder.web.db_helpers import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT unresolved_reasons, locations_structured, notes FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return ("", 404)
    cleared_locs = _clear_location_unresolved(row["locations_structured"])
    notes = _append_note(row["notes"], _audit_line("approved", row["unresolved_reasons"]))
    conn.execute(
        "UPDATE jobs SET unresolved_reasons = '[]', locations_structured = ?, notes = ? "
        "WHERE dedup_key = ?",
        (cleared_locs, notes, dedup_key),
    )
    conn.commit()
    logger.info("Admin review: approved %s", dedup_key)
    # 200 (not 204) so HTMX performs the outerHTML swap with an empty body,
    # removing the row from the triage table (per CLAUDE.md).
    return ("", 200)


@admin_bp.route("/review/<dedup_key>/drop", methods=["POST"], strict_slashes=False)
def drop_review(dedup_key: str):
    """Reject a flagged row (pipeline_status='rejected')."""
    from job_finder.web.db_helpers import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT unresolved_reasons, notes FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return ("", 404)
    notes = _append_note(row["notes"], _audit_line("dropped", row["unresolved_reasons"]))
    conn.execute(
        "UPDATE jobs SET pipeline_status = 'rejected', notes = ? WHERE dedup_key = ?",
        (notes, dedup_key),
    )
    conn.commit()
    logger.info("Admin review: dropped %s", dedup_key)
    return ("", 200)


@admin_bp.route("/jobs/direct-links/backfill", methods=["POST"], strict_slashes=False)
def backfill_direct_links_route():
    """Resolve jobs.direct_url for the existing backlog (ATS/careers only, free).

    One-time manual op. May take a while on a large backlog (one ATS scan +
    careers scrape per NULL-direct_url job that has a linked company). For a
    clean run, pause the enrichment backfill first:
        POST /admin/jobs/enrichment_backfill/pause
    Idempotent — re-running only touches rows still NULL.
    """
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {}) or {}
    summary = backfill_direct_links(conn, config)
    logger.warning("Admin: direct-link backfill %s", summary)
    return jsonify(summary)
