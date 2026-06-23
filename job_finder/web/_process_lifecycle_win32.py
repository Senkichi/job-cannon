"""Windows Job Object subprocess cleanup (Issue #39 Commit C, §10.2).

Assigns the current process to an unnamed Job Object configured with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (only).  When the handle closes
(process exits or is force-killed), the OS reaps all job members —
transitively covering the Ollama and Playwright children spawned by this
process, because those children **inherit** job membership.

Why NOT ``SILENT_BREAKAWAY_OK``
-------------------------------
The original config also set ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``, which
lets child processes silently *leave* the job at creation.  That is precisely
what defeated the reap: a spawned Ollama broke away from the job, so when the
launcher was force-killed and the job closed, Ollama survived as an orphan —
the long-running multi-process accumulation bug.  Dropping the flag makes
children stay in the job, so ``KILL_ON_JOB_CLOSE`` reaps them on any death of
the job owner (clean exit, crash, or ``taskkill /F``).  Nested jobs are
supported on Windows 8+, so a child that needs its own job (e.g. Chromium)
still nests under ours rather than failing to start.

Handle-retention contract (load-bearing)
-----------------------------------------
The job handle is retained in ``_job_handle`` at module scope for the entire
process lifetime.  Closing it triggers ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
and kills us.  ``install_kill_on_exit()`` therefore returns ``None`` and stores
the handle internally; callers have no value to accidentally discard.

On ``ERROR_ACCESS_DENIED`` (nested Job Object — e.g. run inside uv/pytest/CI):
    The function returns without retaining the handle.  We were never a job
    member, so GC-closing the handle does not kill us.  This is the
    graceful-degrade path; the app still works, only forced-kill reap is
    absent — and that gap is exactly what the metadata ``owned_pids`` record
    (``register_owned_process`` → ``record_owned_pid``) backstops: a later
    launch's port reclaim can still find and terminate a reparented orphan.
"""

from __future__ import annotations

import logging

import pywintypes  # type: ignore[import]  # pywin32
import win32api  # type: ignore[import]  # pywin32
import win32job  # type: ignore[import]  # pywin32
import winerror  # type: ignore[import]  # pywin32

logger = logging.getLogger(__name__)

# Module-level handle.  Keeping it alive = keeping the Job Object alive.
# If this handle is closed (GC, process exit, or explicit close) the OS
# fires JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and reaps all job members.
_job_handle = None
_install_attempted = False

# Popen handles of children we spawned, retained so they are not GC'd and so a
# graceful shutdown path can address them.  Mirrors the POSIX implementation;
# on Windows the Job Object is the primary unclean-kill reaper, this list plus
# the metadata owned_pids record are the belt-and-suspenders.
_owned_procs: list = []


def install_kill_on_exit() -> None:
    """Create a Job Object and assign the current process.

    Idempotent — safe to call multiple times; only the first call does work.
    Returns ``None`` by design so callers cannot accidentally hold the handle.
    """
    global _job_handle, _install_attempted
    if _install_attempted:
        return
    _install_attempted = True

    job = win32job.CreateJobObject(None, "")  # unnamed Job Object, default DACL
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    # KILL_ON_JOB_CLOSE only — deliberately NOT SILENT_BREAKAWAY_OK (see module
    # docstring): children must stay in the job to be reaped on owner death.
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_ACCESS_DENIED:
            # Running inside a nested Job Object (uv, pytest, CI runner …).
            # Subprocess auto-reap on forced kill is unavailable for this session.
            logger.warning(
                "AssignProcessToJobObject failed with ACCESS_DENIED. "
                "Subprocess auto-reap on exit is disabled for this session."
            )
            # Do NOT retain the handle on failure.  We were never a job member,
            # so GC-closing the handle will not kill us.
            return
        raise
    # SUCCESS: keep the handle alive at module scope for the full process lifetime.
    _job_handle = job


def register_owned_process(proc) -> None:
    """Track a spawned child (``subprocess.Popen``) the launcher owns.

    Two effects, both backstops to the Job Object's transitive reap:
      1. Retain the Popen handle in ``_owned_procs`` so it is not GC'd.
      2. Record the child's PID + identity in this instance's metadata sidecar
         (``_pidfile.record_owned_pid``) so a later launch's port reclaim can
         terminate it even if it was reparented by an unclean death while the
         Job Object was degraded (ACCESS_DENIED path).

    Best-effort and never raises: child cleanup must not break the spawn path.
    """
    _owned_procs.append(proc)
    try:
        import psutil

        from job_finder.web._pidfile import record_owned_pid

        pid = proc.pid
        try:
            ps = psutil.Process(pid)
            name, create_time = ps.name(), ps.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name, create_time = "", None
        record_owned_pid(pid, name=name, create_time=create_time)
    except Exception:  # pragma: no cover - PID bookkeeping must never break spawn
        logger.debug("register_owned_process bookkeeping failed", exc_info=True)
