"""Process lifecycle dispatcher — platform-specific subprocess cleanup.

Replaces the Commit-A stub façade (Issue #37).  Public API is preserved
exactly so existing callers (scheduler/_ollama.py, etc.) need no changes:

    install_kill_on_exit() -> None
    register_owned_process(proc) -> None
    make_pdeathsig_preexec_fn() -> callable | None

Windows
    Job Object with ``KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK`` assigned to
    the current process.  Per-Popen tracking is unnecessary — Job Object
    inheritance reaps all descendants transitively — so
    ``register_owned_process`` and ``make_pdeathsig_preexec_fn`` are no-ops.

POSIX (Linux / macOS)
    atexit + SIGTERM/SIGINT/SIGHUP handlers that terminate every tracked
    Popen with a grace period.  Linux also provides a real
    ``make_pdeathsig_preexec_fn()`` (prctl PR_SET_PDEATHSIG) so spawned
    children receive SIGTERM on our death, with a fork-race close guard.

Other platforms
    Graceful no-ops.  The app still functions; only unclean-kill reap is
    absent.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    from ._process_lifecycle_win32 import install_kill_on_exit

    # Windows: Job Object inheritance handles subprocess reap transitively.
    # Keep Commit-A no-op stubs so importers do not break.
    def register_owned_process(proc) -> None: ...

    def make_pdeathsig_preexec_fn():
        return None

elif sys.platform in ("linux", "darwin"):
    from ._process_lifecycle_posix import (
        install_kill_on_exit,
        make_pdeathsig_preexec_fn,
        register_owned_process,
    )

else:

    def install_kill_on_exit() -> None:
        logger.debug("Process lifecycle: no implementation for platform %s", sys.platform)

    def register_owned_process(proc) -> None: ...

    def make_pdeathsig_preexec_fn():
        return None
