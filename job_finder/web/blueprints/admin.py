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

import logging

from flask import Blueprint, jsonify

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


@admin_bp.route("/jobs/<job_id>/run-now", methods=["POST"], strict_slashes=False)
def run_job_now(job_id: str):
    """Trigger immediate execution by setting next_run_time to now."""
    from datetime import datetime, timezone

    sched, err = _scheduler_or_503()
    if err:
        return err

    job = sched.get_job(job_id)
    if job is None:
        return jsonify({"error": f"no such job: {job_id}"}), 404

    job.modify(next_run_time=datetime.now(timezone.utc))
    logger.warning("Admin: triggered immediate run of %s", job_id)
    return jsonify({"id": job_id, "triggered": True})
