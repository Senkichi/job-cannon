"""Cross-process pidfile lock for the background scheduler.

Prevents two independent Python processes from both running the 0,8,16 cron
schedule, which previously caused the 16:00 PT ingestion to fire twice and
double-bill Gmail/SerpAPI/DataForSEO.

Public-surface contract: ``_acquire_scheduler_pidfile`` is patched by
``tests/conftest.py`` via the package attribute path
``job_finder.web.scheduler._acquire_scheduler_pidfile``. The package's
``__init__.py`` re-exports it so the patch swaps the attribute that
``init_scheduler`` looks up.
"""

import atexit
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level state mirrors the legacy scheduler.py shape: the atexit
# cleanup closure needs to know which path WE wrote.
_pidfile_path: Path | None = None


def _acquire_scheduler_pidfile(app) -> bool:
    """Acquire a cross-process pidfile lock before starting the scheduler.

    Self-heals stale pidfiles: if the recorded PID is no longer alive, the
    lock is taken cleanly. Cross-process liveness check uses psutil.

    Returns:
        True if the lock was acquired (safe to start scheduler), False if
        another live instance is already running (caller must skip).
    """
    global _pidfile_path

    db_path = app.config.get("DB_PATH", "jobs.db")
    pidfile = Path(db_path).resolve().parent / "logs" / "scheduler.pid"

    if pidfile.exists():
        try:
            existing_pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None  # corrupt pidfile — treat as stale

        if existing_pid and existing_pid != os.getpid():
            try:
                import psutil

                alive = psutil.pid_exists(existing_pid)
            except Exception:
                alive = False

            if alive:
                logger.warning(
                    "Scheduler: another instance (PID %d) is already running — "
                    "this process will NOT start a scheduler to prevent duplicate cron firings",
                    existing_pid,
                )
                return False

    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    _pidfile_path = pidfile

    def _cleanup_pidfile() -> None:
        try:
            if _pidfile_path and _pidfile_path.exists():
                # Only remove if WE still own it (avoid racing with another process
                # that may have taken over after a crash).
                try:
                    owner_pid = int(_pidfile_path.read_text().strip())
                except Exception:
                    owner_pid = None
                if owner_pid == os.getpid():
                    _pidfile_path.unlink()
        except Exception:
            pass  # best-effort cleanup; next start self-heals via liveness check

    atexit.register(_cleanup_pidfile)
    logger.info("Scheduler: acquired pidfile lock at %s (PID %d)", pidfile, os.getpid())
    return True
