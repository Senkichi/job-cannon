"""Mock-heavy unit tests for job_finder.web._process_lifecycle_win32.

These tests are platform-neutral: they run on Windows, Linux, and macOS alike
because win32 dependencies (win32api, win32job, pywintypes, winerror) are
injected into sys.modules as stubs before the implementation module is loaded.

Assertions per acceptance criteria:
- CreateJobObject called with (None, "")
- LimitFlags includes JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and
  JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
- AssignProcessToJobObject called with win32api.GetCurrentProcess() handle
- ACCESS_DENIED: returns without raising; _job_handle is None
- Idempotency: second call does NOT invoke CreateJobObject again
- Success: _job_handle is not None (handle retained at module scope)
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal win32 exception stand-in
# ---------------------------------------------------------------------------


class _PywintypesError(Exception):
    """Minimal stand-in for pywintypes.error that carries a .winerror attribute.

    pywintypes.error on a live Windows system has the signature
    (winerror, funcname, strerror).  We only need winerror for the tests.
    """

    def __init__(self, winerror: int = 0, funcname: str = "", strerror: str = "") -> None:
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror


# Numeric value for ERROR_ACCESS_DENIED — must match mock_winerror.ERROR_ACCESS_DENIED
_ERROR_ACCESS_DENIED = 5

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MODULE_NAME = "job_finder.web._process_lifecycle_win32"


def _make_win32_mocks() -> dict:
    """Return a fresh dict of win32 stub modules for one test run."""
    mock_pywintypes = MagicMock()
    mock_pywintypes.error = _PywintypesError  # real exception class

    mock_winerror = MagicMock()
    mock_winerror.ERROR_ACCESS_DENIED = _ERROR_ACCESS_DENIED

    # Constants match the real pywin32 values; correctness is tested by
    # checking the bits OR'd into LimitFlags, not the absolute value.
    mock_win32job = MagicMock()
    mock_win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    mock_win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
    mock_win32job.JobObjectExtendedLimitInformation = 9
    # QueryInformationJobObject returns a nested dict; the code does an in-place
    # |= on BasicLimitInformation.LimitFlags then passes the dict to Set*.
    mock_win32job.QueryInformationJobObject.return_value = {
        "BasicLimitInformation": {"LimitFlags": 0}
    }

    mock_win32api = MagicMock()

    return {
        "win32api": mock_win32api,
        "win32job": mock_win32job,
        "pywintypes": mock_pywintypes,
        "winerror": mock_winerror,
    }


@pytest.fixture()
def win32_mod():
    """Yield (module, mocks) — a freshly-imported _process_lifecycle_win32
    backed by clean win32 stubs, with module-level state reset between tests.
    """
    mocks = _make_win32_mocks()

    # Ensure no stale cached module from a previous test iteration.
    sys.modules.pop(_MODULE_NAME, None)

    with patch.dict("sys.modules", mocks):
        mod = importlib.import_module(_MODULE_NAME)
        yield mod, mocks

    # Remove from cache so the next test gets a fresh import.
    sys.modules.pop(_MODULE_NAME, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_job_object_called_with_none_empty_string(win32_mod):
    """CreateJobObject must be called with (None, "")  — unnamed, default DACL."""
    mod, mocks = win32_mod
    mod.install_kill_on_exit()
    mocks["win32job"].CreateJobObject.assert_called_once_with(None, "")


def test_limit_flags_include_kill_on_close(win32_mod):
    """LimitFlags must have JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE set."""
    mod, mocks = win32_mod
    w = mocks["win32job"]
    mod.install_kill_on_exit()

    set_call = w.SetInformationJobObject.call_args
    info = set_call[0][2]
    flags = info["BasicLimitInformation"]["LimitFlags"]
    assert flags & w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_limit_flags_include_silent_breakaway_ok(win32_mod):
    """LimitFlags must have JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK set."""
    mod, mocks = win32_mod
    w = mocks["win32job"]
    mod.install_kill_on_exit()

    set_call = w.SetInformationJobObject.call_args
    info = set_call[0][2]
    flags = info["BasicLimitInformation"]["LimitFlags"]
    assert flags & w.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK


def test_assign_process_called_with_current_process_handle(win32_mod):
    """AssignProcessToJobObject must receive the handle from GetCurrentProcess()."""
    mod, mocks = win32_mod
    w = mocks["win32job"]
    wa = mocks["win32api"]
    mod.install_kill_on_exit()

    expected_job = w.CreateJobObject.return_value
    expected_handle = wa.GetCurrentProcess.return_value
    w.AssignProcessToJobObject.assert_called_once_with(expected_job, expected_handle)


def test_access_denied_returns_without_raising(win32_mod):
    """ACCESS_DENIED from AssignProcessToJobObject must not propagate."""
    mod, mocks = win32_mod
    mocks["win32job"].AssignProcessToJobObject.side_effect = _PywintypesError(
        winerror=_ERROR_ACCESS_DENIED
    )
    # Must not raise — graceful degradation
    mod.install_kill_on_exit()


def test_access_denied_handle_not_retained(win32_mod):
    """On ACCESS_DENIED the handle must NOT be retained (_job_handle stays None).

    This is load-bearing: if we retained the handle after a failed assign,
    GC would close it and KILL_ON_JOB_CLOSE would fire — killing us even
    though we were never a member.
    """
    mod, mocks = win32_mod
    mocks["win32job"].AssignProcessToJobObject.side_effect = _PywintypesError(
        winerror=_ERROR_ACCESS_DENIED
    )
    mod.install_kill_on_exit()
    assert mod._job_handle is None


def test_idempotent_second_call_creates_job_object_exactly_once(win32_mod):
    """Calling install_kill_on_exit() twice must invoke CreateJobObject only once."""
    mod, mocks = win32_mod
    mod.install_kill_on_exit()
    mod.install_kill_on_exit()
    assert mocks["win32job"].CreateJobObject.call_count == 1


def test_success_handle_retained_at_module_scope(win32_mod):
    """On success the job handle must be stored in _job_handle (not None)."""
    mod, _mocks = win32_mod
    mod.install_kill_on_exit()
    assert mod._job_handle is not None


def test_non_access_denied_win_error_propagates(win32_mod):
    """pywintypes.error with a winerror other than ACCESS_DENIED must re-raise."""
    mod, mocks = win32_mod
    OTHER_ERROR = 6  # ERROR_INVALID_HANDLE — not ACCESS_DENIED
    mocks["win32job"].AssignProcessToJobObject.side_effect = _PywintypesError(winerror=OTHER_ERROR)
    with pytest.raises(_PywintypesError):
        mod.install_kill_on_exit()
