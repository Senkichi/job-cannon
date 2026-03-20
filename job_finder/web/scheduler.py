"""APScheduler background scheduler for automatic job ingestion.

Polls Gmail and SerpAPI every 30 minutes in a background thread.
The scheduler is started once per process via init_scheduler(app).

Guards:
1. Flask debug reloader guard: WERKZEUG_RUN_MAIN prevents double-start when
   Flask's reloader spawns a child process (run.py uses use_reloader=False,
   but this guard is a safety net).
2. Double-init guard: module-level _scheduler singleton prevents re-initialization
   if create_app() is called more than once in the same process.
3. Testing guard: scheduler is skipped when app.config["TESTING"] is True.
"""

import logging
import os
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Module-level singleton -- prevents double initialization
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


def init_scheduler(app) -> None:
    """Initialize and start the background scheduler.

    Called from create_app() after all blueprints are registered.
    Safe to call multiple times -- guards prevent double initialization.

    Args:
        app: Flask application instance (fully constructed).
    """
    global _scheduler

    # Guard 1: Skip in test mode
    if app.config.get("TESTING", False):
        logger.debug("Scheduler: skipped (TESTING=True)")
        return

    # Guard 2: Flask debug reloader -- skip in child process
    # (run.py sets use_reloader=False, but this is a safety net)
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        logger.debug("Scheduler: skipped (werkzeug reloader child process)")
        return

    with _scheduler_lock:
        # Guard 3: Already initialized
        if _scheduler is not None:
            logger.debug("Scheduler: already initialized, skipping")
            return

        scheduler = BackgroundScheduler(daemon=True)

        def run_pipeline():
            """Wrapped ingestion job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import log_activity, ACTION_SCHEDULED_SYNC
                from job_finder.web.pipeline_runner import run_ingestion
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    summary = run_ingestion(config, db_path)
                    logger.info(
                        "Scheduled ingestion: %d new jobs (gmail: %d, serpapi: %d)",
                        summary["jobs_new"],
                        summary["gmail_fetched"],
                        summary["serpapi_fetched"],
                    )
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_SYNC,
                        metadata={
                            "jobs_new": summary.get("jobs_new", 0),
                            "gmail_fetched": summary.get("gmail_fetched", 0),
                            "serpapi_fetched": summary.get("serpapi_fetched", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Scheduled ingestion failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_SYNC,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_pipeline,
            trigger=IntervalTrigger(minutes=30),
            id="ingestion_poll",
            replace_existing=True,
            max_instances=1,   # prevents overlap if a run takes >30 min
            coalesce=True,     # skip missed runs if app was down
        )

        def run_stale_job():
            """Nightly stale detection job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_STALE_DETECTION
                )
                from job_finder.web.stale_detector import run_stale_detection
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    result = run_stale_detection(db_path)
                    logger.info("Stale detection: %s", result)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_STALE_DETECTION,
                        metadata={
                            "stale_marked": result.get("stale_marked", 0),
                            "stale_cleared": result.get("stale_cleared", 0),
                            "archived": result.get("archived", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Stale detection failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_STALE_DETECTION,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_stale_job,
            trigger=CronTrigger(hour=2, minute=0),
            id="stale_detection",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_detection():
            """Pipeline detection job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_PIPELINE_DETECTION
                )
                from job_finder.web.pipeline_detector import run_pipeline_detection
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    result = run_pipeline_detection(config, db_path)
                    logger.info(
                        "Pipeline detection: %d scanned, %d auto-updated, %d queued",
                        result.get("emails_scanned", 0),
                        result.get("auto_updated", 0),
                        result.get("queued", 0),
                    )
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_PIPELINE_DETECTION,
                        metadata={
                            "emails_scanned": result.get("emails_scanned", 0),
                            "auto_updated": result.get("auto_updated", 0),
                            "queued": result.get("queued", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Pipeline detection failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_PIPELINE_DETECTION,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_detection,
            trigger=IntervalTrigger(minutes=30),
            id="pipeline_detection",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_feedback_poll():
            """Drive resume feedback poll executed by APScheduler."""
            with app.app_context():
                from job_finder.web.resume_feedback import run_drive_feedback_poll
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                try:
                    result = run_drive_feedback_poll(db_path, config)
                    logger.info("Drive feedback poll: %s", result)
                except Exception as e:
                    logger.error("Drive feedback poll failed: %s", e)

        scheduler.add_job(
            run_feedback_poll,
            trigger=IntervalTrigger(minutes=30),
            id="drive_feedback_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_consolidation():
            """Weekly preference consolidation job executed by APScheduler."""
            with app.app_context():
                from job_finder.web.resume_feedback import run_preference_consolidation
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                try:
                    result = run_preference_consolidation(db_path, config)
                    logger.info("Preference consolidation: %s", result)
                except Exception as e:
                    logger.error("Preference consolidation failed: %s", e)

        scheduler.add_job(
            run_consolidation,
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
            id="preference_consolidation",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_rejection_job():
            """Weekly rejection analysis job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_REJECTION_ANALYSIS
                )
                from job_finder.web.rejection_analyzer import run_rejection_analysis
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    result = run_rejection_analysis(db_path, config)
                    logger.info("Rejection analysis: %s", result)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_REJECTION_ANALYSIS,
                        metadata={
                            "rejections_analyzed": result.get("rejections_analyzed", 0),
                            "budget_exceeded": result.get("budget_exceeded", False),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Rejection analysis failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_REJECTION_ANALYSIS,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_rejection_job,
            trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
            id="rejection_analysis",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_ats_scan_job():
            """ATS scan job executed by APScheduler (Mon/Wed at 7:00 AM)."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_ATS_SCAN
                )
                from job_finder.web.ats_scanner import run_ats_scan
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                if not config.get("ats", {}).get("scan_enabled", True):
                    return
                t0 = _time.time()
                try:
                    result = run_ats_scan(db_path, config)
                    logger.info("ATS scan: %s", result)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_ATS_SCAN,
                        metadata={
                            "companies_scanned": result.get("companies_scanned", 0),
                            "jobs_found": result.get("jobs_found", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("ATS scan failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_ATS_SCAN,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_ats_scan_job,
            trigger=CronTrigger(day_of_week="mon,wed", hour=7, minute=0),
            id="ats_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_slug_probe_job():
            """ATS slug probe job executed by APScheduler (Mon/Wed at 7:30 AM).

            Runs 30 minutes after ATS scan to pick up newly-added companies
            from the day's ingestion run.
            """
            with app.app_context():
                from job_finder.web.ats_scanner import probe_ats_slugs
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")
                try:
                    result = probe_ats_slugs(db_path, config)
                    logger.info("ATS slug probe: %s", result)
                except Exception as e:
                    logger.error("ATS slug probe failed: %s", e)

        scheduler.add_job(
            run_slug_probe_job,
            trigger=CronTrigger(day_of_week="mon,wed", hour=7, minute=30),
            id="ats_slug_probe",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        def run_expiry_check_job():
            """Nightly expiry check job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_EXPIRY_CHECK
                )
                from job_finder.web.expiry_checker import run_expiry_check
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")

                if not config.get("expiry", {}).get("enabled", True):
                    return

                t0 = _time.time()
                try:
                    result = run_expiry_check(db_path, config)
                    logger.info("Expiry check: %s", result)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_EXPIRY_CHECK,
                        metadata={
                            "checked": result.get("checked", 0),
                            "archived": result.get("archived", 0),
                            "live": result.get("live", 0),
                            "inconclusive": result.get("inconclusive", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Expiry check failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_EXPIRY_CHECK,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_expiry_check_job,
            trigger=CronTrigger(hour=2, minute=30),
            id="expiry_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.start()
        _scheduler = scheduler
        logger.info("Scheduler started: Gmail + SerpAPI polling every 30 minutes")


def trigger_sync(app) -> dict:
    """Trigger an immediate ingestion run (for the Sync Now button).

    Runs synchronously in the current thread. Returns the ingestion summary.
    If the pipeline fails, returns an error summary dict.

    Args:
        app: Flask application instance.

    Returns:
        Summary dict from run_ingestion, or an error dict if ingestion failed.
    """
    from job_finder.web.pipeline_runner import run_ingestion
    config = app.config.get("JF_CONFIG", {})
    db_path = app.config.get("DB_PATH", "jobs.db")

    try:
        summary = run_ingestion(config, db_path)
        logger.info("Manual sync triggered: %d new jobs", summary.get("jobs_new", 0))
    except Exception as e:
        logger.error("Manual sync failed: %s", e)
        return {
            "gmail_fetched": 0,
            "gmail_errors": [str(e)],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "jobs_new": 0,
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.0,
            "error": str(e),
        }

    # Run pipeline detection after ingestion (non-blocking on failure)
    try:
        from job_finder.web.pipeline_detector import run_pipeline_detection
        detection_result = run_pipeline_detection(config, db_path)
        summary["detection_auto_updated"] = detection_result.get("auto_updated", 0)
        summary["detection_queued"] = detection_result.get("queued", 0)
        logger.info(
            "Manual sync detection: %d auto-updated, %d queued",
            summary["detection_auto_updated"],
            summary["detection_queued"],
        )
    except Exception as e:
        logger.error("Manual sync pipeline detection failed: %s", e)
        summary["detection_auto_updated"] = 0
        summary["detection_queued"] = 0

    return summary


def get_scheduler() -> BackgroundScheduler | None:
    """Return the running scheduler instance (or None if not started).

    Used for status checks and monitoring.
    """
    return _scheduler


def reset_scheduler() -> None:
    """Reset the scheduler singleton (test helper only).

    Shuts down the running scheduler if one exists, then clears the singleton.
    Only call this in tests to ensure a clean state between test runs.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                logger.debug("scheduler shutdown error", exc_info=True)
            _scheduler = None
