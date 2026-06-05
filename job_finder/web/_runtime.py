"""Shared runtime teardown helper.

``runtime_shutdown()`` is the single source of truth for "tear down what the
main process owns". It is called from:
  - ``__main__.py`` ``try/finally`` block (terminal-mode normal exit)
  - ``__main__.py`` signal handlers (SIGINT / SIGTERM / SIGHUP)
  - Windows ``SetConsoleCtrlHandler`` callback (CTRL_CLOSE_EVENT)
  - Issue #40's TrayApp ``_shutdown_all()`` (tray mode)

Werkzeug shutdown is deliberately excluded — terminal mode lets Werkzeug
exit via KeyboardInterrupt; tray mode shuts Werkzeug down explicitly in
TrayApp._shutdown_all().

Order guarantee: scheduler.shutdown() → spawned-Ollama.terminate().
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_shutdown_done: bool = False


def runtime_shutdown() -> None:
    """Idempotent runtime teardown.

    Order: scheduler → owned Popens.  Safe to call multiple times — the
    idempotency guard makes the second and subsequent calls no-ops.
    """
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    # Import here (not at module level) to avoid circular-import issues and
    # to ensure the scheduler singleton is fully initialised before we try to
    # shut it down.
    from job_finder.web.scheduler import get_scheduler, get_spawned_ollama_proc

    scheduler = get_scheduler()
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Scheduler shutdown raised: %s", exc)

    spawned = get_spawned_ollama_proc()
    if spawned is not None and spawned.poll() is None:
        try:
            spawned.terminate()
        except (ProcessLookupError, OSError) as exc:
            logger.warning("Spawned-Ollama terminate raised: %s", exc)


def reset_for_testing() -> None:
    """Test-only: clear the idempotency guard so tests don't leak state."""
    global _shutdown_done
    _shutdown_done = False
