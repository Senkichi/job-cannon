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
from job_finder.web.scheduler._sync import run_sync_now

__all__ = [
    "_acquire_scheduler_pidfile",
    "_ensure_ollama_running",
    "get_scheduler",
    "init_scheduler",
    "register_all_jobs",
    "reset_scheduler",
    "run_sync_now",
]

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

    # Guard 1: Skip in test mode or when caller has asked for "scheduler off,
    # but jobs themselves still do real work" (scripts/run_overnight.py pattern).
    # SKIP_SCHEDULER is the right flag for secondary app instances that share
    # a live Flask's DB but must NOT race its scheduler. TESTING also gates
    # several job functions to no-op (careers_crawl, ats_scan, ats_slug_probe,
    # ats_identity_reconcile) — wrong shape for run_overnight.py.
    if app.config.get("TESTING", False) or app.config.get("SKIP_SCHEDULER", False):
        logger.debug("Scheduler: skipped (TESTING or SKIP_SCHEDULER)")
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

        # We now own the pidfile — any prior scheduler process is dead, so any
        # batch_score_sessions row still at 'running'/'cancelling' was orphaned
        # by that dead process (its daemon worker thread died with it). Reap them
        # to 'error' so phantom scan/sync banners aren't re-mounted on page load.
        try:
            from job_finder.web.db_helpers import reap_orphan_sessions

            reap_orphan_sessions(app.config["DB_PATH"])
        except Exception as exc:
            logger.warning("Orphan-session reaper raised unexpectedly: %s", exc)

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
