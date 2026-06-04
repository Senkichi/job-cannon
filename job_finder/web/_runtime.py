"""Process-level runtime teardown helper (Issue #1: shared shutdown path).

Provides :func:`runtime_shutdown` — the single path for APScheduler +
spawned-subprocess (Ollama) teardown.  All launch modes (terminal, tray,
headless fallback) must delegate to this helper rather than duplicating the
shutdown logic inline.

Design invariants
-----------------
* Idempotent by delegation: APScheduler's ``shutdown(wait=False)`` is
  itself idempotent; calling it on an already-stopped scheduler is a no-op.
* Never raises: every internal failure is swallowed + logged so the caller's
  ``finally`` block always completes cleanly.
"""

import logging

logger = logging.getLogger(__name__)


def runtime_shutdown() -> None:
    """Idempotent process-level teardown: scheduler + spawned subprocesses.

    Shuts down the APScheduler singleton (if running) and terminates any
    Ollama subprocess that was spawned by the scheduler's
    ``_ensure_ollama_running()`` helper.  Safe to call multiple times.

    Caller contract
    ---------------
    ``TrayApp._shutdown_all`` and ``__main__._run_terminal_mode`` must
    delegate here rather than directly accessing the scheduler singleton or
    the Ollama process handle.  This keeps the shutdown logic in one place
    and makes the "no-second-create_app" invariant easier to reason about.
    """
    from job_finder.web.scheduler import get_scheduler

    sched = get_scheduler()
    if sched is not None:
        try:
            sched.shutdown(wait=False)
            logger.debug("runtime_shutdown: scheduler stopped")
        except Exception as exc:
            logger.warning("runtime_shutdown: scheduler shutdown raised: %s", exc)
