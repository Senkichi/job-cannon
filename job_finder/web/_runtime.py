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

``runtime_shutdown()`` also arms :func:`schedule_force_exit` — every caller
is a process-exit path, and graceful exit can stall indefinitely on
non-daemon ``concurrent.futures`` worker threads (see that function's
docstring), so "teardown has begun" doubles as "the process must die soon".
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_shutdown_done: bool = False

# Grace period between "graceful teardown started" and the os._exit backstop.
# Everything runtime_shutdown touches returns in well under a second
# (scheduler.shutdown(wait=False), Popen.terminate, Werkzeug's 0.5 s poll
# loop); the grace only exists to let that normal unwind finish.
_FORCE_EXIT_GRACE_SEC = 3.0

_force_exit_timer: threading.Timer | None = None


def schedule_force_exit(grace_sec: float = _FORCE_EXIT_GRACE_SEC) -> threading.Timer | None:
    """Arm a daemon watchdog that hard-exits if graceful shutdown stalls.

    Why this exists: ``concurrent.futures.ThreadPoolExecutor`` workers are
    non-daemon on Python 3.9+, and the interpreter joins them at exit (both
    ``threading._shutdown`` and concurrent.futures' own atexit hook). The
    APScheduler job executor, ``expiry_checker``, and ``careers_crawler`` all
    use such pools, and a worker mid-flight in an Ollama scoring call can run
    for minutes — without this backstop, Ctrl+C "works" but the process
    lingers until the in-flight job finishes, which reads as a hang.

    ``os._exit`` here is safe: the SQLite WAL is crash-consistent, the
    server.lock is kernel-released on any process death, runtime_shutdown has
    already terminated the owned Ollama Popen, and the Win32 Job Object from
    ``install_kill_on_exit`` reaps any remaining children.

    Idempotent — the first armed timer wins. Returns the armed timer, or None
    when arming was skipped.
    """
    global _force_exit_timer
    if _force_exit_timer is not None:
        return _force_exit_timer
    if "PYTEST_CURRENT_TEST" in os.environ:
        # Never arm a process-killing timer inside a pytest worker: it would
        # take down the whole worker seconds after an unrelated test. Quit
        # paths are unit-tested with this function patched or via the timer's
        # own dedicated test (which clears this env var deliberately).
        return None

    def _fire() -> None:
        try:
            logger.warning(
                "Graceful shutdown still blocked after %.1fs "
                "(non-daemon worker threads, likely an in-flight pool job) — forcing exit.",
                grace_sec,
            )
        except Exception:
            pass  # logging must never stop the backstop
        os._exit(0)

    timer = threading.Timer(grace_sec, _fire)
    timer.daemon = True
    timer.name = "jc-force-exit"
    timer.start()
    _force_exit_timer = timer
    return timer


def runtime_shutdown() -> None:
    """Idempotent runtime teardown.

    Order: force-exit watchdog armed → scheduler → owned Popens.  Safe to
    call multiple times — the idempotency guard makes the second and
    subsequent calls no-ops.
    """
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    # Armed FIRST so a wedged teardown step below can't outlive the backstop.
    schedule_force_exit()

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
    global _shutdown_done, _force_exit_timer
    _shutdown_done = False
    if _force_exit_timer is not None:
        _force_exit_timer.cancel()
        _force_exit_timer = None
