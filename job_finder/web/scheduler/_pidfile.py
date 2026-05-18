"""Cross-process file lock for the background scheduler.

Replaces the prior PID-tracking advisory pidfile (2026-05-17 hotfix Fix 6),
which was vulnerable to two failure modes on Windows:

1. PID reuse — the OS aggressively recycles PIDs, so an unrelated process
   landing on the dead scheduler's old PID made the psutil-based liveness
   check return True and refuse to start the scheduler.
2. atexit not firing on force-kill / unclean shutdown, leaving the pidfile
   on disk indefinitely.

portalocker acquires an OS-level file lock that the kernel releases when
the holding process terminates — including SIGKILL, Ctrl+C interruption
mid-shutdown, and Windows force-kill paths. The lock IS the liveness
signal; the pidfile contents are diagnostic-only.

Public-surface contract: ``_acquire_scheduler_pidfile`` is patched by
``tests/conftest.py`` via the package attribute path
``job_finder.web.scheduler._acquire_scheduler_pidfile``. The package's
``__init__.py`` re-exports it so the patch swaps the attribute that
``init_scheduler`` looks up.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# Module-level: holds the lock file handle for the lifetime of this process.
# OS releases the lock automatically when this process terminates (any cause).
# Do NOT close in atexit — explicit close races with shutdown ordering and
# the OS release is the contract.
_lock_handle = None


def _acquire_scheduler_pidfile(app) -> bool:
    """Acquire a cross-process OS file lock before starting the scheduler.

    Returns:
        True if the lock was acquired (safe to start scheduler), False if
        another live process is already holding it (caller must skip).
    """
    global _lock_handle

    db_path = app.config.get("DB_PATH", "jobs.db")
    pidfile = Path(db_path).resolve().parent / "logs" / "scheduler.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    # Open for read/write so we can both lock and rewrite the PID. Mode "a+"
    # creates the file if absent without truncating an existing one.
    fh = open(pidfile, "a+", encoding="utf-8")  # noqa: SIM115 — must outlive function
    try:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except (portalocker.exceptions.LockException, OSError) as exc:
        try:
            fh.seek(0)
            existing = fh.read().strip()
        except OSError:
            existing = "<unreadable>"
        logger.warning(
            "Scheduler: another instance is already running "
            "(pidfile=%s, contents=%s, error=%s) — this process will NOT "
            "start a scheduler",
            pidfile,
            existing,
            exc,
        )
        try:
            fh.close()
        except Exception:
            pass
        return False

    # Lock acquired. Rewrite the pidfile with our PID for diagnostics —
    # liveness itself is the lock, not the file contents.
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()

    _lock_handle = fh  # keep alive for process lifetime so the lock persists
    logger.info("Scheduler: acquired lock at %s (PID %d)", pidfile, os.getpid())
    return True
