"""APScheduler background scheduler for automatic job ingestion.

Runs Gmail, SerpAPI, and Thordata ingestion 3x/day (midnight, 8am, 4pm local).
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

from job_finder.web.db_helpers import get_config_snapshot
from job_finder.web.scheduler._jobs import register_all_jobs
from job_finder.web.scheduler._ollama import _ensure_ollama_running
from job_finder.web.scheduler._pidfile import _acquire_scheduler_pidfile

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

    # Guard 2: Flask debug reloader -- skip in child process.
    # WERKZEUG_RUN_MAIN="true" is set by Flask's reloader in the child process.
    # run.py sets use_reloader=False so this guard normally never triggers, but
    # it is kept as a safety net in case the reloader is enabled by accident.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        logger.debug("Scheduler: skipped (werkzeug reloader child process)")
        return

    with _scheduler_lock:
        # Guard 3: Already initialized (same process)
        if _scheduler is not None:
            logger.debug("Scheduler: already initialized, skipping")
            return

        # Guard 4: Cross-process pidfile lock. If another Python process has
        # already claimed the pidfile and is still alive, skip scheduler start.
        if not _acquire_scheduler_pidfile(app):
            return

        # Eagerly start Ollama so the nightly agentic backfill (3:30 AM) has
        # a live service to talk to. Best-effort; never raises.
        try:
            _ensure_ollama_running(get_config_snapshot(app))
        except Exception as exc:
            logger.warning("Ollama auto-start helper raised unexpectedly: %s", exc)

        scheduler = BackgroundScheduler(daemon=True)
        register_all_jobs(scheduler, app)
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "Scheduler started: ingestion 3x/day (0:00, 8:00, 16:00 local); enrichment 1h after each (1:00, 9:00, 17:00 local)"
        )


def run_sync_now(app) -> dict:
    """Trigger an immediate ingestion run (for the Sync Now button).

    Runs synchronously in the current thread. Returns the ingestion summary.
    If the pipeline fails, returns an error summary dict.

    Args:
        app: Flask application instance.

    Returns:
        Summary dict from run_ingestion, or an error dict if ingestion failed.
    """
    from job_finder.web.pipeline_runner import run_ingestion

    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")

    try:
        summary = run_ingestion(db_path, config, score=False)
        logger.info("Manual sync triggered: %d new jobs", summary.get("jobs_new", 0))
    except Exception as e:
        logger.error("Manual sync failed: %s", e)
        return {
            "gmail_fetched": 0,
            "gmail_errors": [str(e)],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "thordata_fetched": 0,
            "thordata_errors": [],
            "dataforseo_fetched": 0,
            "dataforseo_errors": [],
            "portal_search_fetched": 0,
            "portal_search_errors": [],
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

        detection_result = run_pipeline_detection(db_path, config)
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
