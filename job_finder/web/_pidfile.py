"""Main-process split-file advisory lock for Job Cannon.

Uses a split-file pattern: ``server.lock`` holds the kernel-released
exclusive lock; ``server.json`` is a readable metadata sidecar.

Why split files?
On Windows, ``portalocker.LOCK_EX`` on a single file also blocks readers
(EACCES errno 13). Contention callers that need to read the metadata
(pid, url, start_time_utc) would receive Permission denied. Splitting the
lock from the metadata means:
- The lock file is the liveness signal (kernel releases it on any process exit).
- The metadata file is always readable from any process.

This module must NOT be confused with ``job_finder/web/scheduler/_pidfile.py``,
which uses a single-file pattern for the background scheduler. That module
is preserved as-is per §7.2.5 of the Process Lifecycle Plan — do not
merge or refactor it.

Acquire once, hold for process lifetime. The OS releases the lock on any
process termination (clean shutdown, SIGKILL, crash). Never close the
handle in atexit — explicit close races with shutdown ordering.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# Module-level dict: keeps lock file handles alive for the process lifetime.
# The OS releases the lock when the handle is GC'd or the process exits.
# Never close handles stored here during normal operation.
_lock_handles: dict[Path, object] = {}


class ExistingInstanceAction(Enum):
    """Action to take after detecting a live or contested lock during startup."""

    CONTINUE_STARTUP = "continue"  # dead-PID retry succeeded; main should continue
    EXIT_SUCCESS = "exit_0"  # existing live JC instance; browser opened
    EXIT_FAILURE = "exit_1"  # unresolvable state; print message and exit 1


@dataclass
class AcquireResult:
    """Result of an ``acquire_pidfile`` call."""

    acquired: bool
    existing: dict | None = field(default=None)
    fh: object | None = field(default=None)


def acquire_pidfile(lock_path: Path, meta_path: Path, metadata: dict) -> AcquireResult:
    """Acquire a kernel-released advisory lock at ``lock_path``.

    On success:
    - Writes ``metadata`` atomically to ``meta_path`` (write-temp + Path.replace).
    - Retains the open file handle in ``_lock_handles`` for the process lifetime.
    - Returns ``AcquireResult(acquired=True, fh=<handle>)``.

    On failure (another process holds the lock):
    - Closes the file handle immediately (does not add to ``_lock_handles``).
    - Returns ``AcquireResult(acquired=False, existing=<metadata or None>)``
      where ``existing`` is the parsed metadata sidecar (may be None if the
      sidecar is missing or unparseable — caller must handle both cases).

    Args:
        lock_path: Path to the exclusive-lock file (``server.lock``).
        meta_path: Path to the readable metadata sidecar (``server.json``).
        metadata:  Dict to write atomically to ``meta_path`` on success.

    Returns:
        AcquireResult with ``acquired=True`` on success, ``acquired=False`` on contention.
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

    # Lock acquired: write metadata atomically (write-temp + rename).
    # Path.replace() is atomic on POSIX and atomic on Windows for same-volume
    # moves (Python 3.3+ guarantee). The .tmp suffix ensures the in-progress
    # write is never mistaken for a complete record.
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata), encoding="utf-8")
    tmp.replace(meta_path)

    _lock_handles[lock_path] = fh  # keep alive for process lifetime
    logger.info("Main process: acquired lock at %s (PID %s)", lock_path, metadata.get("pid"))
    return AcquireResult(acquired=True, fh=fh)


def _read_metadata(meta_path: Path) -> dict | None:
    """Return parsed metadata from ``meta_path``, or None if missing/unparseable.

    Callers must validate semantic freshness (``psutil.pid_exists`` + cmdline
    match) before trusting any returned values — the file may have been
    written by a now-dead or unrelated process.

    Returns:
        Parsed dict on success, None on missing file, read error, or JSON error.
    """
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
