"""Process lifecycle façade stub.

Commit A (Issue #37) ships the public interface; real OS-level reap mechanisms
arrive in Commit C (Issue #39). The stub is not a no-op for ``register_owned_process``:
Issue #3's POSIX impl reuses the same ``_owned_procs`` list, so list population
semantics must be correct from day one.

Public surface
--------------
install_kill_on_exit() -> None
    No-op stub. Replaced by Issue #39 (Windows Job Object).

register_owned_process(proc) -> None
    Appends proc to the module-level _owned_procs list. Issue #39 reuses
    this list for ``_terminate_owned`` — do NOT make this a true no-op.

make_pdeathsig_preexec_fn() -> callable | None
    Returns None (harmless preexec_fn for POSIX Popen). Replaced by
    Issue #39 (prctl PR_SET_PDEATHSIG).
"""

from __future__ import annotations

import subprocess

_owned_procs: list[subprocess.Popen] = []


def install_kill_on_exit() -> None:
    """No-op stub. Issue #39 replaces this with Windows Job Object install."""
    return


def register_owned_process(proc: subprocess.Popen) -> None:
    """Append *proc* to the module-level owned-process list.

    Issue #39's ``_terminate_owned`` function iterates this same list.
    Registration must happen at spawn time (Commit A) so Commit C can
    reuse it without changing call-sites.
    """
    _owned_procs.append(proc)


def make_pdeathsig_preexec_fn():
    """Return None (harmless preexec_fn value for POSIX Popen).

    Issue #39 replaces this with a real ``prctl(PR_SET_PDEATHSIG, SIGTERM)``
    closure on Linux. Returning None means ``subprocess.Popen(preexec_fn=None)``
    which is the default and entirely safe.
    """
    return None
