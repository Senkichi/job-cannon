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
from datetime import UTC

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
