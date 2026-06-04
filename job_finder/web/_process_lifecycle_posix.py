"""POSIX process lifecycle implementation for job-cannon.

Registers atexit + signal handlers that terminate tracked subprocesses on
exit.  On Linux, provides ``make_pdeathsig_preexec_fn()`` so directly-spawned
children receive SIGTERM when their parent (us) dies — with a fork-race guard.

Coverage summary:
- ``atexit``: fires on normal exit and unhandled exceptions.
- SIGTERM / SIGINT / SIGHUP: fires on signal-based shutdown.
- prctl PR_SET_PDEATHSIG (Linux only): fires on SIGKILL of parent.
- macOS: no prctl equivalent; G3 reduces to the graceful-shutdown guarantee.

Documented limitations (§12.2.5, not fixable without Playwright API changes):
- Playwright children on Linux SIGKILL: Playwright manages its own subprocess
  launch at ``playwright/_impl/_transport.py:120`` and we have no public hook
  to inject ``preexec_fn``.  Playwright driver + Chromium may orphan on
  SIGKILL of us on Linux.  Per-call ``browser.close()`` covers graceful exit.
- macOS SIGKILL coverage: no prctl equivalent on Darwin.

Do NOT call os.setsid() here.  That would detach us from the controlling
terminal's session, breaking Ctrl+C and the SIGHUP-on-terminal-close path.
"""

import atexit
import ctypes
import ctypes.util
import logging
import os
import signal
import sys
import time

logger = logging.getLogger(__name__)

_PR_SET_PDEATHSIG = 1  # from <sys/prctl.h>
_libc = None
if sys.platform == "linux":
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        _libc = None

# Module-level list of tracked subprocesses.  Populated by
# register_owned_process().  Shared via the public dispatcher
# (_process_lifecycle.py) so that Commit-A spawn sites (scheduler/_ollama.py
# etc.) continue to work without modification.
_owned_procs: list = []


def install_kill_on_exit() -> None:
    """Install POSIX cleanup hooks.

    Registers atexit + SIGTERM/SIGINT/SIGHUP handlers.  Idempotent at the
    atexit level (atexit.register with the same callable is safe to call
    multiple times — the callback is just registered more than once, but
    _terminate_owned is itself idempotent because poll()/terminate()/kill()
    are all no-ops on already-dead processes).

    Does NOT call os.setsid() — see module docstring.
    """
    atexit.register(_terminate_owned)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_signal)


def register_owned_process(proc) -> None:
    """Track a Popen handle for cleanup on exit.

    Called by every spawn site whose child should die with us.
    Currently called from ``scheduler/_ollama.py`` for the Ollama subprocess.
    Append-only; clearing is done by ``_terminate_owned`` implicitly (dead
    procs are handled gracefully by poll() returning non-None).
    """
    _owned_procs.append(proc)


def make_pdeathsig_preexec_fn():
    """Return a preexec_fn that requests SIGTERM on parent death.

    Linux only.  Uses prctl(PR_SET_PDEATHSIG, SIGTERM) via ctypes.
    Returns None on macOS, non-Linux platforms, or when libc is unavailable.

    Fork-race guard (§12.2, §13 risk table):
        There is a window between fork() and the child's prctl() call during
        which a parent death goes undetected — the child gets reparented to
        init and the PDEATHSIG is never set.  Mitigation: capture
        ``parent_pid_at_spawn = os.getpid()`` in the parent's closure BEFORE
        fork; after prctl the child re-checks os.getppid() and calls
        os._exit(1) on mismatch.  This matches the ownership contract — a
        child whose parent died during attach should not become a detached
        service.
    """
    if sys.platform != "linux" or _libc is None:
        return None

    parent_pid_at_spawn = os.getpid()  # captured in parent, before fork

    def _preexec() -> None:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        # Fork-race close: if the parent died between fork() and prctl(),
        # we are now reparented to init (getppid() != original parent).
        # Exit immediately so we don't become a detached service.
        if os.getppid() != parent_pid_at_spawn:
            os._exit(1)

    return _preexec


def _terminate_owned(grace_seconds: float = 2.0) -> None:
    """Terminate every tracked Popen.  SIGTERM first; SIGKILL after grace."""
    for proc in _owned_procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    deadline = time.monotonic() + grace_seconds
    for proc in _owned_procs:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            proc.wait(timeout=remaining)
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass


def _handle_signal(sig, frame) -> None:
    """Signal handler: terminate owned processes then exit cleanly."""
    _terminate_owned()
    sys.exit(0)
