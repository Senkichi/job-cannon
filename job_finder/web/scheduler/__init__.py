"""APScheduler background scheduler for automatic job ingestion.

Runs Gmail, SerpAPI, and DataForSEO ingestion 3x/day (midnight, 8am, 4pm local).
The scheduler is started once per process via init_scheduler(app).

Guards:
1. Flask debug reloader guard: WERKZEUG_RUN_MAIN prevents double-start when
   Flask's reloader spawns a child process (use_reloader=False everywhere,
   but this guard is a safety net).
2. Double-init guard: module-level _scheduler singleton prevents re-initialization
   if create_app() is called more than once in the same process.
3. Testing guard: scheduler is skipped when app.config["TESTING"] is True.
4. Single-instance claim: the scheduler starts only if THIS process holds the
   one (host, port) liveness lock (job_finder.web._pidfile.holds_claim) — the
   same lock the launcher acquires before binding. No second scheduler lock.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from job_finder.web._pidfile import holds_claim
from job_finder.web.scheduler._jobs import register_all_jobs
from job_finder.web.scheduler._ollama import (
    AlreadyRunning,
    Installable,
    Unavailable,
    probe_ollama,
    resolve_ollama_url,
    spawn_ollama,
)
from job_finder.web.scheduler._sync import run_sync_now

__all__ = [
    "get_scheduler",
    "get_spawned_ollama_proc",
    "holds_claim",
    "init_scheduler",
    "probe_ollama",
    "register_all_jobs",
    "reset_scheduler",
    "run_sync_now",
]

logger = logging.getLogger(__name__)

# Module-level singleton -- prevents double initialization
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()

# Popen handle for an Ollama process we spawned (None = not spawned by us)
_spawned_ollama_proc: subprocess.Popen | None = None


def get_spawned_ollama_proc() -> subprocess.Popen | None:
    """Return the Popen handle for a spawned-by-us Ollama process, or None."""
    return _spawned_ollama_proc


def init_scheduler(app) -> None:
    """Initialize and start the background scheduler.

    Called from create_app() after all blueprints are registered.
    Safe to call multiple times -- guards prevent double initialization.

    Args:
        app: Flask application instance (fully constructed).
    """
    global _scheduler, _spawned_ollama_proc

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

        # Guard 4: Single-instance claim. The scheduler runs *in the same
        # process* that bound the port and holds the one (host, port) liveness
        # lock (see job_finder.web._pidfile). If this process does not hold that
        # claim — e.g. a secondary app instance sharing the DB, or any caller
        # that built the app without going through the launcher — it must not
        # start a competing scheduler. This replaces the former second,
        # separately-keyed scheduler pidfile: one lock, one truth.
        if not holds_claim():
            logger.info(
                "Scheduler: this process does not hold the (host, port) liveness "
                "claim — not starting (another instance owns it, or the app was "
                "built without acquiring the lock)"
            )
            return

        # We hold the claim — any prior scheduler process is dead, so any
        # batch_score_sessions row still at 'running'/'cancelling' was orphaned
        # by that dead process (its daemon worker thread died with it). Reap them
        # to 'error' so phantom scan/sync banners aren't re-mounted on page load.
        try:
            from job_finder.web.db_helpers import reap_orphan_sessions

            reap_orphan_sessions(app.config["DB_PATH"])
        except Exception as exc:
            logger.warning("Orphan-session reaper raised unexpectedly: %s", exc)

        # -------------------------------------------------------------------
        # Smart Ollama probe (Issue #37, §6.2-§6.4)
        #
        # Mutate app.config["JF_CONFIG"] directly — NOT the snapshot returned
        # by get_config_snapshot() (that's a deepcopy; mutations are no-ops for
        # later readers). See §6.3 invariant.
        # -------------------------------------------------------------------
        try:
            live_config = app.config.setdefault("JF_CONFIG", {})
            resolved_url = resolve_ollama_url(live_config)

            # Determine target model from config (fall back to default)
            target_model = (
                live_config.get("providers", {})
                .get("overrides", {})
                .get("ollama", {})
                .get("score", "qwen2.5:14b")
            )

            state = probe_ollama(target_model, resolved_url)

            if isinstance(state, (AlreadyRunning, Installable)):
                # Store the resolved URL back into live config so that
                # OllamaProvider picks up the correct base_url later.
                live_config.setdefault("providers", {}).setdefault("ollama", {})["base_url"] = (
                    resolved_url
                )

                if isinstance(state, Installable):
                    proc = spawn_ollama(state.path)
                    _spawned_ollama_proc = proc

            elif isinstance(state, Unavailable):
                live_config["_jf_ollama_unavailable"] = True

        except Exception as exc:
            logger.warning("Ollama probe raised unexpectedly: %s", exc)

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
    global _scheduler, _spawned_ollama_proc
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                logger.debug("scheduler shutdown error", exc_info=True)
            _scheduler = None
        _spawned_ollama_proc = None
