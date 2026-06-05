"""Main-process split-file advisory lock for the Job Cannon web server.

Uses a split-file pattern (server.lock + server.json) to work around the
Windows EACCES constraint: portalocker.LOCK_EX blocks contention readers
from reading the locked file on Windows, so we keep the lock file separate
from the metadata JSON. The JSON sidecar is always readable; the lock file
existence signals "lock may be held" — actual lock state is determined by
trying to acquire LOCK_NB.

Split-file vs. scheduler/_pidfile.py single-file pattern:
  The scheduler uses a single-file pattern (lock IS the payload) because its
  lock is liveness-only and PID contents are diagnostic. Do NOT refactor
  scheduler/_pidfile.py to this pattern — it would break the documented test
  patch surface at ``job_finder.web.scheduler._acquire_scheduler_pidfile``.

Windows note: ``portalocker.LOCK_EX`` on Windows prevents reads from other
processes via CreateFile. ``server.json`` is separate so that any contention
reader can parse the metadata without acquiring the lock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# Module-level: keeps lock file handles alive for this process's lifetime.
# OS releases all locks when the process terminates (any cause, including SIGKILL).
# Do NOT close these in atexit — explicit close races with shutdown ordering.
_lock_handles: dict[Path, object] = {}


class ExistingInstanceAction(Enum):
    """Result enum returned by ``handle_existing_instance``."""

    CONTINUE_STARTUP = "continue"  # dead-PID retry succeeded; caller should proceed
    EXIT_SUCCESS = "exit_0"  # confirmed live instance; browser opened
    EXIT_FAILURE = "exit_1"  # corrupt metadata or unresolvable contention


@dataclass
class AcquireResult:
    """Outcome of a ``acquire_pidfile`` call."""

    acquired: bool
    """True iff this process now holds the exclusive lock."""
    existing: dict | None = None
    """Parsed ``server.json`` metadata from the contending holder, or None."""
    fh: object | None = None
    """Open file handle for the acquired lock (kept alive by caller / _lock_handles)."""


def acquire_pidfile(lock_path: Path, meta_path: Path, metadata: dict) -> AcquireResult:
    """Acquire a kernel-released advisory lock at *lock_path*.

    On success:
      - Atomically writes *metadata* to *meta_path* (write-temp + Path.replace).
      - Retains the lock file handle in ``_lock_handles`` for process lifetime.
      - Returns ``AcquireResult(acquired=True, fh=<handle>)``.

    On contention (another live process holds the lock):
      - Returns ``AcquireResult(acquired=False, existing=<parsed metadata or None>)``.
      - The existing metadata comes from ``_read_metadata(meta_path)``; callers
        must validate semantic freshness via psutil before trusting PID values.

    The lock is kernel-released: it persists across Python atexit callbacks and
    is automatically released when the process terminates for any reason,
    including SIGKILL and Windows force-kill.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 — must outlive function
    try:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except (portalocker.exceptions.LockException, OSError):
        try:
            fh.close()
        except Exception:
            pass
        return AcquireResult(acquired=False, existing=_read_metadata(meta_path))

    # Lock acquired. Atomically write metadata sidecar (write-temp + rename).
    # Path.replace() is atomic on both POSIX (rename syscall) and Windows
    # (MoveFileEx with MOVEFILE_REPLACE_EXISTING).
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata), encoding="utf-8")
    tmp.replace(meta_path)

    # Keep handle alive so the OS lock persists for process lifetime.
    _lock_handles[lock_path] = fh
    logger.debug("Main-process lock acquired at %s (PID %d)", lock_path, metadata.get("pid", "?"))
    return AcquireResult(acquired=True, fh=fh)


def _read_metadata(meta_path: Path) -> dict | None:
    """Return parsed JSON metadata from *meta_path*, or None on any failure.

    Returns None when:
      - The file does not exist (holder mid-startup, or no prior instance).
      - The file exists but cannot be read (permission error, transient IO).
      - The file contains invalid JSON.

    Caller is responsible for semantic validation (psutil.pid_exists + cmdline
    match) before trusting any returned values.
    """
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
