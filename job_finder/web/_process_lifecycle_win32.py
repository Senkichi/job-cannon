"""Windows Job Object subprocess cleanup (Issue #39 Commit C, §10.2).

Assigns the current process to an unnamed Job Object configured with
``KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK``.  When the handle closes
(process exits or is force-killed), the OS reaps all job members —
transitively covering Ollama and Playwright children spawned by this process.

Handle-retention contract (load-bearing)
-----------------------------------------
The job handle is retained in ``_job_handle`` at module scope for the entire
process lifetime.  Closing it triggers ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
and kills us.  ``install_kill_on_exit()`` therefore returns ``None`` and stores
the handle internally; callers have no value to accidentally discard.

On ``ERROR_ACCESS_DENIED`` (nested Job Object — e.g. run inside uv/pytest/CI):
    The function returns without retaining the handle.  We were never a job
    member, so GC-closing the handle does not kill us.  This is the
    graceful-degrade path; the app still works, only forced-kill reap is absent.
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
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
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
