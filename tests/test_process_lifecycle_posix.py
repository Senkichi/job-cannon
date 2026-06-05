"""POSIX process lifecycle unit tests (Issue #39 Commit C).

Skipped on Windows — POSIX signal constants and prctl semantics are
platform-specific.  The underlying module (_process_lifecycle_posix) is
importable on Windows (all OS-specific code is guarded), but the tests
themselves only make sense on POSIX.

Coverage:
- atexit.register called with _terminate_owned
- signal handlers installed for SIGTERM, SIGINT, and SIGHUP (where available)
- _owned_procs populated by register_owned_process()
- _terminate_owned calls terminate() on each proc, then kill() after grace
- make_pdeathsig_preexec_fn returns a callable on Linux when _libc is loaded
- make_pdeathsig_preexec_fn returns None on Darwin
- make_pdeathsig_preexec_fn returns None when _libc is None
- Fork-race: parent PID P1, preexec invoked with ppid P2 ≠ P1 → os._exit(1)
"""

from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

# This mark is applied to EVERY test in the file via pytest's module-level
# pytestmark mechanism.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only tests")

# Lazy module import — evaluated at collection time on all platforms (module
# is importable everywhere), but test execution is skipped on Windows via
# pytestmark above.
from job_finder.web import _process_lifecycle_posix

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_owned_procs():
    """Clear _owned_procs before and after each test for isolation."""
    _process_lifecycle_posix._owned_procs.clear()
    yield
    _process_lifecycle_posix._owned_procs.clear()


# ---------------------------------------------------------------------------
# install_kill_on_exit
# ---------------------------------------------------------------------------


def test_install_registers_atexit_with_terminate_owned():
    """`atexit.register` must be called with `_terminate_owned`."""
    with patch("atexit.register") as mock_atexit, patch("signal.signal"):
        _process_lifecycle_posix.install_kill_on_exit()
    mock_atexit.assert_any_call(_process_lifecycle_posix._terminate_owned)


def test_install_registers_sigterm_handler():
    """`signal.signal(SIGTERM, ...)` must be called."""
    installed: list[int] = []

    def _fake_signal(signum, handler):
        installed.append(signum)

    with patch("atexit.register"), patch("signal.signal", side_effect=_fake_signal):
        _process_lifecycle_posix.install_kill_on_exit()

    assert signal.SIGTERM in installed


def test_install_registers_sigint_handler():
    """`signal.signal(SIGINT, ...)` must be called."""
    installed: list[int] = []

    def _fake_signal(signum, handler):
        installed.append(signum)

    with patch("atexit.register"), patch("signal.signal", side_effect=_fake_signal):
        _process_lifecycle_posix.install_kill_on_exit()

    assert signal.SIGINT in installed


def test_install_registers_sighup_handler():
    """`signal.signal(SIGHUP, ...)` must be called on platforms that have SIGHUP."""
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform")

    installed: list[int] = []

    def _fake_signal(signum, handler):
        installed.append(signum)

    with patch("atexit.register"), patch("signal.signal", side_effect=_fake_signal):
        _process_lifecycle_posix.install_kill_on_exit()

    assert signal.SIGHUP in installed


# ---------------------------------------------------------------------------
# register_owned_process
# ---------------------------------------------------------------------------


def test_register_owned_process_appends_to_list():
    """`register_owned_process` must append the proc to `_owned_procs`."""
    mock_proc = MagicMock(name="proc")
    _process_lifecycle_posix.register_owned_process(mock_proc)
    assert mock_proc in _process_lifecycle_posix._owned_procs


# ---------------------------------------------------------------------------
# _terminate_owned
# ---------------------------------------------------------------------------


def test_terminate_owned_calls_terminate_first():
    """`_terminate_owned` must call `terminate()` on each live process."""
    mock_proc = MagicMock(name="proc")
    mock_proc.poll.return_value = None  # process still running
    _process_lifecycle_posix._owned_procs.append(mock_proc)

    _process_lifecycle_posix._terminate_owned(grace_seconds=0.01)

    mock_proc.terminate.assert_called_once()


def test_terminate_owned_calls_kill_after_grace_timeout():
    """`_terminate_owned` must call `kill()` on processes that survive the grace."""
    mock_proc = MagicMock(name="proc")
    mock_proc.poll.return_value = None  # still running throughout
    mock_proc.wait.side_effect = Exception("timeout")  # refuses to exit
    _process_lifecycle_posix._owned_procs.append(mock_proc)

    _process_lifecycle_posix._terminate_owned(grace_seconds=0.01)

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# make_pdeathsig_preexec_fn
# ---------------------------------------------------------------------------


def test_make_pdeathsig_returns_callable_on_linux_with_libc():
    """On Linux with _libc loaded, must return a callable."""
    if sys.platform != "linux":
        pytest.skip("Linux-only assertion")
    if _process_lifecycle_posix._libc is None:
        pytest.skip("libc not loaded on this system")

    fn = _process_lifecycle_posix.make_pdeathsig_preexec_fn()
    assert callable(fn)


def test_make_pdeathsig_returns_none_on_darwin():
    """On Darwin (macOS), must return None — no prctl equivalent."""
    with patch("job_finder.web._process_lifecycle_posix.sys") as mock_sys:
        mock_sys.platform = "darwin"
        result = _process_lifecycle_posix.make_pdeathsig_preexec_fn()
    assert result is None


def test_make_pdeathsig_returns_none_when_libc_is_none():
    """When _libc is None (load failed or non-Linux), must return None."""
    with patch.object(_process_lifecycle_posix, "_libc", None):
        result = _process_lifecycle_posix.make_pdeathsig_preexec_fn()
    assert result is None


# ---------------------------------------------------------------------------
# Fork-race close test
# ---------------------------------------------------------------------------


def test_fork_race_exit_on_ppid_mismatch():
    """If the parent PID at spawn time differs from os.getppid() inside the
    preexec callable, os._exit(1) must be called exactly once.

    This simulates the race window where the parent dies between fork() and
    the child's prctl() call — the child detects reparenting and exits.
    """
    P1 = 12345  # parent PID at spawn
    P2 = 99999  # what os.getppid() returns after reparenting (P2 ≠ P1)

    mock_libc = MagicMock(name="libc")

    with (
        patch.object(_process_lifecycle_posix, "_libc", mock_libc),
        patch("job_finder.web._process_lifecycle_posix.sys") as mock_sys,
        patch("job_finder.web._process_lifecycle_posix.os") as mock_os,
    ):
        mock_sys.platform = "linux"
        mock_os.getpid.return_value = P1  # captured before fork
        mock_os.getppid.return_value = P2  # observed inside preexec (mismatch)
        mock_os._exit = MagicMock(name="os._exit")

        preexec_fn = _process_lifecycle_posix.make_pdeathsig_preexec_fn()
        assert preexec_fn is not None, "expected a callable on linux with libc"

        preexec_fn()  # invoke as the child would

    mock_os._exit.assert_called_once_with(1)


def test_fork_race_no_exit_when_ppid_matches():
    """When os.getppid() matches parent_pid_at_spawn, os._exit must NOT be called."""
    P1 = 12345

    mock_libc = MagicMock(name="libc")

    with (
        patch.object(_process_lifecycle_posix, "_libc", mock_libc),
        patch("job_finder.web._process_lifecycle_posix.sys") as mock_sys,
        patch("job_finder.web._process_lifecycle_posix.os") as mock_os,
    ):
        mock_sys.platform = "linux"
        mock_os.getpid.return_value = P1  # parent PID at spawn
        mock_os.getppid.return_value = P1  # same PID — parent still alive
        mock_os._exit = MagicMock(name="os._exit")

        preexec_fn = _process_lifecycle_posix.make_pdeathsig_preexec_fn()
        assert preexec_fn is not None

        preexec_fn()

    mock_os._exit.assert_not_called()
