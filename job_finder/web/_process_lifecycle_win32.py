"""Windows Job Object implementation for process lifecycle management.

Creates an unnamed Job Object with KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK
and assigns the current process.  The handle is retained at module scope for
the entire process lifetime.

Handle-retention contract (load-bearing, §10.2):
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE kills ALL member processes when the
    last open handle closes.  After AssignProcessToJobObject succeeds, we ARE
    a job member.  If a caller discarded the handle, pywin32 would GC-close it
    and the OS would kill us.  install_kill_on_exit() therefore returns None
    and stores the handle in module-level ``_job_handle`` — callers have
    nothing to discard.

ACCESS_DENIED (§10.2 fallback):
    On systems where the process is already inside a Job Object that does not
    permit child jobs (e.g. some CI runners, or a terminal emulator that has
    set job limits), AssignProcessToJobObject raises pywintypes.error with
    winerror == ERROR_ACCESS_DENIED.  We log a warning and return without
    retaining the handle — the process was never a job member, so GC-closing
    the (discarded) handle does not kill us.

SILENT_BREAKAWAY_OK (§10.2):
    Allows child processes to break away from the job if they request it via
    CREATE_BREAKAWAY_FROM_JOB.  Playwright's browser children use this flag;
    without SILENT_BREAKAWAY_OK those spawns would fail with ERROR_ACCESS_DENIED
    from CreateProcess.
"""

import logging

import pywintypes
import win32api
import win32job
import winerror

logger = logging.getLogger(__name__)

# Module-level: keep the job handle alive for the entire process lifetime.
# Closing this handle would trigger JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
_job_handle = None
_install_attempted = False


def install_kill_on_exit() -> None:
    """Create a Job Object and assign the current process to it.

    Idempotent: second and subsequent calls return immediately without
    creating additional job objects.

    Returns None by design — callers do NOT keep or close the return value.
    The handle is retained in ``_job_handle`` at module scope so that its
    lifetime equals the process and the KILL_ON_JOB_CLOSE trigger fires
    exactly when the process tree should die.
    """
    global _job_handle, _install_attempted
    if _install_attempted:
        return
    _install_attempted = True

    job = win32job.CreateJobObject(None, "")  # unnamed, default DACL
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_ACCESS_DENIED:
            logger.warning(
                "AssignProcessToJobObject failed with ACCESS_DENIED. "
                "Subprocess auto-reap on forced-kill is disabled for this session. "
                "This is normal when running inside an existing Job Object "
                "(e.g. a CI runner or terminal emulator that has job limits set)."
            )
            # Do NOT retain the handle on failure.  We were never a job
            # member, so closing the handle (on GC) does not kill us.
            return
        raise
    # SUCCESS: retain the handle at module scope so its lifetime equals the
    # process.  _job_handle is intentionally module-level, not a local; this
    # is the whole point of the handle-retention contract.
    _job_handle = job
