"""POSIX subprocess cleanup via atexit, signals, and prctl(PR_SET_PDEATHSIG).

Issue #39 Commit C, §12.2.

Provides:
- ``install_kill_on_exit()``: register atexit and SIGTERM/SIGINT/SIGHUP handlers
- ``register_owned_process(proc)``: track Popen handles for cleanup
- ``make_pdeathsig_preexec_fn()``: Linux preexec_fn with fork-race close guard
- ``_terminate_owned()``: terminate all tracked procs (SIGTERM → SIGKILL after grace)

Design notes
------------
- Does NOT call ``os.setsid()`` — that would detach us from the controlling
  terminal, breaking Ctrl+C and SIGHUP-on-terminal-close.
- ``_owned_procs`` is the module-level list populated by
  ``register_owned_process()``.  It is intentionally defined here (not in
  the dispatcher) so that the POSIX implementation owns its own state.
- ``make_pdeathsig_preexec_fn()`` is Linux-only (requires ``prctl``).  macOS
  and other POSIX systems return ``None`` — graceful-degrade path.
- Fork-race mitigation: ``parent_pid_at_spawn`` is captured before ``fork()``;
  after ``prctl`` the child re-checks ``os.getppid()`` and calls
  ``os._exit(1)`` on mismatch (parent died in the fork→prctl window).
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import logging
import os
import signal
import sys
import time

logger = logging.getLogger(__name__)

# Linux-only: load libc for prctl(PR_SET_PDEATHSIG).
_PR_SET_PDEATHSIG = 1  # from <sys/prctl.h>
_libc = None
if sys.platform == "linux":
    try:
        _lib_name = ctypes.util.find_library("c")
        if _lib_name:
            _libc = ctypes.CDLL(_lib_name, use_errno=True)
    except OSError:
        _libc = None

# Module-level owned-process list.  Populated by register_owned_process();
# iterated by _terminate_owned() on exit.
_owned_procs: list = []


def install_kill_on_exit() -> None:
    """Register POSIX cleanup hooks for owned subprocesses.

    Registers an atexit handler and installs SIGTERM, SIGINT, and (where
    available) SIGHUP signal handlers so that owned processes are terminated
    on both normal and signal-driven exit.
    """
    atexit.register(_terminate_owned)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_signal)


def register_owned_process(proc) -> None:
    """Track *proc* (a ``subprocess.Popen`` handle) for cleanup on exit.

    Also records the child's PID + identity in this instance's metadata sidecar
    (best-effort) so a later launch's port reclaim can terminate a reparented
    orphan — the same backstop the Windows path uses. No-ops the metadata write
    unless this process holds the claim.
    """
    _owned_procs.append(proc)
    try:
        import psutil

        from job_finder.web._pidfile import record_owned_pid

        try:
            ps = psutil.Process(proc.pid)
            name, create_time = ps.name(), ps.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name, create_time = "", None
        record_owned_pid(proc.pid, name=name, create_time=create_time)
    except Exception:  # pragma: no cover - PID bookkeeping must never break spawn
        logger.debug("register_owned_process bookkeeping failed", exc_info=True)


def make_pdeathsig_preexec_fn():
    """Return a ``preexec_fn`` that sets ``PR_SET_PDEATHSIG`` on a child.

    Linux-only (requires ctypes prctl).  Returns ``None`` on macOS and when
    ``_libc`` is not loaded — callers pass the result directly to
    ``subprocess.Popen(preexec_fn=...)`` so ``None`` means "no preexec".

    Fork-race mitigation
    --------------------
    The parent captures its own PID *before* ``fork()``.  After ``prctl``,
    the child re-checks ``os.getppid()``; if it does not match, the parent
    died between ``fork()`` and ``prctl()`` (child was reparented to init),
    so the child calls ``os._exit(1)`` immediately.
    """
    if sys.platform != "linux" or _libc is None:
        return None
    parent_pid_at_spawn = os.getpid()  # captured in parent, before fork

    def _preexec() -> None:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent_pid_at_spawn:
            # Parent died between fork() and prctl(); we have been reparented.
            # Exit immediately — there is no supervisor left.
            os._exit(1)

    return _preexec


def _terminate_owned(grace_seconds: float = 2.0) -> None:
    """Terminate all tracked Popen handles.

    Phase 1 — SIGTERM every live process.
    Phase 2 — wait up to *grace_seconds* for each to exit.
    Phase 3 — SIGKILL any that are still alive after the grace period.
    """
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
