"""Generic kernel-released advisory lock for the main app process.

Split-file pattern: ``server.lock`` is exclusively held (liveness signal),
``server.json`` is a readable metadata sidecar (diagnostic).

On Windows, ``portalocker.LOCK_EX`` blocks contention readers from reading the
locked file (EACCES errno 13).  Splitting lock state from metadata lets any
contention reader parse the metadata JSON even while the lock is held.

See §7.2 of .planning/PROCESS-LIFECYCLE-PLAN.md.

Do NOT modify ``scheduler/_pidfile.py`` — that module's single-file pattern is
preserved per §7.2.5 (documented test patch surface).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# Module-level dict: keeps lock file handles alive for the lifetime of this
# process.  OS releases the locks automatically when the process terminates
# (any cause including SIGKILL).  Do NOT close in atexit — explicit close
# races with shutdown ordering; the OS release is the contract.
_lock_handles: dict[Path, object] = {}


class ExistingInstanceAction(Enum):
    """Decision returned by ``handle_existing_instance`` to the launcher."""

    CONTINUE_STARTUP = "continue"  # dead-PID retry succeeded; main should continue
    EXIT_SUCCESS = "exit_0"  # existing live instance; browser opened
    EXIT_FAILURE = "exit_1"  # corrupt metadata or unresolvable state


@dataclass
class AcquireResult:
    """Result of a ``acquire_pidfile`` call."""

    acquired: bool
    existing: dict | None = None
    fh: object | None = None


def acquire_pidfile(lock_path: Path, meta_path: Path, metadata: dict) -> AcquireResult:
    """Acquire a kernel-released advisory lock at *lock_path*.

    On success:
    - The lock file handle is retained in ``_lock_handles`` for the process
      lifetime so the kernel lock persists until the process terminates.
    - *metadata* is written atomically to *meta_path* (write-temp +
      ``Path.replace``), which is safe on both Windows and POSIX.

    On failure (lock already held by another process):
    - Returns ``AcquireResult(acquired=False, existing=...)`` where
      ``existing`` is the parsed content of *meta_path*, or ``None`` if the
      file is missing or unparseable.

    Args:
        lock_path: Path to the lock file (exclusively held while running).
        meta_path: Path to the JSON metadata sidecar (always readable).
        metadata: Dict to write atomically to *meta_path* on success.

    Returns:
        ``AcquireResult`` — check ``.acquired`` before proceeding.
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

    # Lock acquired — write metadata atomically via write-temp + rename.
    # Path.replace() is atomic on POSIX (rename(2)) and near-atomic on Windows
    # (MoveFileEx with MOVEFILE_REPLACE_EXISTING, Python 3.3+).
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata), encoding="utf-8")
    tmp.replace(meta_path)

    _lock_handles[lock_path] = fh  # keep alive; OS releases on process exit
    logger.info("server: acquired lock at %s (PID %s)", lock_path, metadata.get("pid"))
    return AcquireResult(acquired=True, fh=fh)


def _read_metadata(meta_path: Path) -> dict | None:
    """Return parsed metadata from *meta_path*, or ``None``.

    Returns ``None`` for: missing file, unreadable file, invalid JSON.
    Caller is responsible for semantic validation (PID liveness, cmdline
    match) before trusting the returned values.
    """
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
