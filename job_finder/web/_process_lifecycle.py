"""Process lifecycle management for job-cannon.

Dispatches to platform-specific implementations:
- Windows: Job Object with KILL_ON_JOB_CLOSE + SILENT_BREAKAWAY_OK
- POSIX (Linux/Darwin): atexit + signal handlers + Linux prctl PDEATHSIG

Public API (preserved from stub façade):
    install_kill_on_exit() -> None
    register_owned_process(proc) -> None
    make_pdeathsig_preexec_fn() -> callable | None
"""

import logging
import sys

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    from ._process_lifecycle_win32 import install_kill_on_exit

    # Windows: Job Object inheritance covers subprocess reap automatically;
    # per-Popen tracking is unnecessary.  Keep the no-op stubs so importers
    # (spawn sites from Issue #1) compile without change.
    def register_owned_process(proc) -> None:
        """No-op on Windows: Job Object inheritance handles subprocess reap."""

    def make_pdeathsig_preexec_fn():
        """prctl is Linux-only; no-op on Windows."""
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
