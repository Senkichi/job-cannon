"""POSIX process lifecycle tests for job_finder.web._process_lifecycle_posix.

Skipped entirely on Windows (pytestmark).  On Linux and macOS the module
imports cleanly (_libc = None on Darwin; ctypes prctl load attempted on Linux).

Assertions per acceptance criteria:
- atexit.register called with _terminate_owned
- Signal handlers installed for SIGTERM, SIGINT, and SIGHUP (where available)
- _owned_procs populated by register_owned_process(mock_popen)
- _terminate_owned calls terminate() on each proc, then kill() after grace
- make_pdeathsig_preexec_fn returns callable on Linux when _libc is loaded
- make_pdeathsig_preexec_fn returns None on Darwin
- make_pdeathsig_preexec_fn returns None when _libc is None
- Fork-race close: preexec calls os._exit(1) exactly once when ppid mismatches
"""

from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

# Skip every test in this file on Windows — POSIX-specific hooks (SIGHUP,
# prctl, os.setsid semantics) are not available there.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only tests; skipped cleanly on Windows",
)


# ---------------------------------------------------------------------------
# Module fixture — provides a fresh import with clean _owned_procs
# ---------------------------------------------------------------------------


@pytest.fixture()
def posix_mod():
    """Yield the POSIX lifecycle module with _owned_procs cleared."""
    import job_finder.web._process_lifecycle_posix as m

    m._owned_procs.clear()
    yield m
    m._owned_procs.clear()


# ---------------------------------------------------------------------------
# install_kill_on_exit
# ---------------------------------------------------------------------------


def test_install_registers_atexit_with_terminate_owned(posix_mod):
    """atexit.register must be called with _terminate_owned as the callback."""
    with patch("atexit.register") as mock_reg, patch("signal.signal"):
        posix_mod.install_kill_on_exit()
    mock_reg.assert_called_once_with(posix_mod._terminate_owned)


def test_install_registers_sigterm_handler(posix_mod):
    with patch("atexit.register"), patch("signal.signal") as mock_sig:
        posix_mod.install_kill_on_exit()
    registered_signals = [call[0][0] for call in mock_sig.call_args_list]
    assert signal.SIGTERM in registered_signals


def test_install_registers_sigint_handler(posix_mod):
    with patch("atexit.register"), patch("signal.signal") as mock_sig:
        posix_mod.install_kill_on_exit()
    registered_signals = [call[0][0] for call in mock_sig.call_args_list]
    assert signal.SIGINT in registered_signals


def test_install_registers_sighup_when_available(posix_mod):
    """SIGHUP handler is installed on POSIX where signal.SIGHUP exists."""
    with patch("atexit.register"), patch("signal.signal") as mock_sig:
        posix_mod.install_kill_on_exit()
    registered_signals = [call[0][0] for call in mock_sig.call_args_list]
    if hasattr(signal, "SIGHUP"):
        assert signal.SIGHUP in registered_signals


# ---------------------------------------------------------------------------
# register_owned_process
# ---------------------------------------------------------------------------


def test_register_owned_process_appends_to_owned_procs(posix_mod):
    mock_proc = MagicMock()
    posix_mod.register_owned_process(mock_proc)
    assert mock_proc in posix_mod._owned_procs


def test_register_owned_process_multiple_procs(posix_mod):
    p1, p2 = MagicMock(), MagicMock()
    posix_mod.register_owned_process(p1)
    posix_mod.register_owned_process(p2)
    assert p1 in posix_mod._owned_procs
    assert p2 in posix_mod._owned_procs
    assert len(posix_mod._owned_procs) == 2


# ---------------------------------------------------------------------------
# _terminate_owned
# ---------------------------------------------------------------------------


def test_terminate_owned_calls_terminate_on_running_proc(posix_mod):
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still running
    mock_proc.wait.return_value = None  # terminates quickly
    posix_mod._owned_procs.append(mock_proc)

    posix_mod._terminate_owned(grace_seconds=0.1)

    mock_proc.terminate.assert_called_once()


def test_terminate_owned_calls_kill_after_grace_timeout(posix_mod):
    """If wait() raises (timeout), kill() must be invoked as a last resort."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still running after terminate
    mock_proc.wait.side_effect = Exception("timeout simulated")

    posix_mod._owned_procs.append(mock_proc)
    posix_mod._terminate_owned(grace_seconds=0.01)

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()


def test_terminate_owned_no_kill_when_wait_succeeds(posix_mod):
    """If the process terminates within grace, kill() must NOT be called."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.wait.return_value = None  # success

    posix_mod._owned_procs.append(mock_proc)
    posix_mod._terminate_owned(grace_seconds=0.5)

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_not_called()


def test_terminate_owned_skips_already_dead_proc(posix_mod):
    """proc.poll() returning non-None means the process is already gone;
    terminate() must not be called (avoids ESRCH noise)."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0  # already exited

    posix_mod._owned_procs.append(mock_proc)
    posix_mod._terminate_owned(grace_seconds=0.01)

    mock_proc.terminate.assert_not_called()
    mock_proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# make_pdeathsig_preexec_fn
# ---------------------------------------------------------------------------


def test_make_pdeathsig_preexec_fn_returns_callable_on_linux(posix_mod):
    """On Linux with _libc loaded, the factory returns a callable."""
    mock_libc = MagicMock()
    with patch.object(sys, "platform", "linux"), patch.object(posix_mod, "_libc", mock_libc):
        fn = posix_mod.make_pdeathsig_preexec_fn()
    assert callable(fn)


def test_make_pdeathsig_preexec_fn_returns_none_on_darwin(posix_mod):
    """prctl is Linux-only; Darwin must return None."""
    with patch.object(sys, "platform", "darwin"):
        fn = posix_mod.make_pdeathsig_preexec_fn()
    assert fn is None


def test_make_pdeathsig_preexec_fn_returns_none_when_libc_is_none(posix_mod):
    """Even on Linux, if _libc failed to load the factory returns None."""
    with patch.object(sys, "platform", "linux"), patch.object(posix_mod, "_libc", None):
        fn = posix_mod.make_pdeathsig_preexec_fn()
    assert fn is None


# ---------------------------------------------------------------------------
# Fork-race close tests
# ---------------------------------------------------------------------------


def test_fork_race_preexec_exits_when_ppid_mismatches(posix_mod):
    """Fork-race close: if the parent died between fork and prctl the child
    must call os._exit(1) exactly once.

    Setup:
      - os.getpid() returns P1 during the factory call (parent at spawn time)
      - the returned _preexec callable is invoked with os.getppid() → P2 ≠ P1
        (simulates: parent died, child reparented to init)
    Expected: os._exit(1) called exactly once.
    """
    P1 = 12345
    P2 = 1  # init — parent is gone

    mock_libc = MagicMock()
    with patch.object(sys, "platform", "linux"), patch.object(posix_mod, "_libc", mock_libc):
        with patch("os.getpid", return_value=P1):
            preexec_fn = posix_mod.make_pdeathsig_preexec_fn()

    assert preexec_fn is not None

    with patch("os.getppid", return_value=P2), patch("os._exit") as mock_exit:
        preexec_fn()
    mock_exit.assert_called_once_with(1)


def test_fork_race_preexec_no_exit_when_ppid_matches(posix_mod):
    """If parent is still alive (getppid() == P1), os._exit must NOT be called."""
    P1 = 12345

    mock_libc = MagicMock()
    with patch.object(sys, "platform", "linux"), patch.object(posix_mod, "_libc", mock_libc):
        with patch("os.getpid", return_value=P1):
            preexec_fn = posix_mod.make_pdeathsig_preexec_fn()

    assert preexec_fn is not None

    with patch("os.getppid", return_value=P1), patch("os._exit") as mock_exit:
        preexec_fn()
    mock_exit.assert_not_called()


def test_fork_race_preexec_calls_prctl(posix_mod):
    """The preexec callable must invoke libc.prctl with PR_SET_PDEATHSIG=1."""
    P1 = 99

    mock_libc = MagicMock()
    with patch.object(sys, "platform", "linux"), patch.object(posix_mod, "_libc", mock_libc):
        with patch("os.getpid", return_value=P1):
            preexec_fn = posix_mod.make_pdeathsig_preexec_fn()

    with patch("os.getppid", return_value=P1), patch("os._exit"):
        preexec_fn()

    # PR_SET_PDEATHSIG = 1; SIGTERM = signal.SIGTERM
    mock_libc.prctl.assert_called_once_with(posix_mod._PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
