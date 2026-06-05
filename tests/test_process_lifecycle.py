"""Mock-heavy unit tests for Windows Job Object subprocess cleanup.

Issue #39 Commit C — platform-agnostic via sys.modules patching.
These tests run on Windows AND POSIX (win32 APIs are fully mocked).

Coverage:
- CreateJobObject called with (None, "")
- LimitFlags includes both KILL_ON_JOB_CLOSE and SILENT_BREAKAWAY_OK
- AssignProcessToJobObject called with GetCurrentProcess() handle
- ERROR_ACCESS_DENIED: returns without raising; _job_handle is None
- Idempotency: two calls → CreateJobObject called exactly once
- Success: _job_handle is not None (handle retained at module scope)
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Constants mirroring pywin32 values
# ---------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
_JOB_OBJ_EXT_LIMIT_INFO = 9
_ERROR_ACCESS_DENIED = 5


class _FakePywinError(Exception):
    """Minimal stand-in for ``pywintypes.error`` with a ``winerror`` attribute."""

    def __init__(self, winerror: int, funcname: str = "", strerror: str = "") -> None:
        self.winerror = winerror
        self.funcname = funcname
        self.strerror = strerror
        super().__init__(winerror, funcname, strerror)


# ---------------------------------------------------------------------------
# Fixture: inject mock win32 modules + load a fresh _process_lifecycle_win32
# ---------------------------------------------------------------------------

_MODULE_KEY = "job_finder.web._process_lifecycle_win32"
_WIN32_DEPS = ["win32job", "win32api", "pywintypes", "winerror"]


@pytest.fixture()
def win32_mod():
    """Inject fake win32 modules into sys.modules, import the implementation
    module fresh, and yield (module, win32job_mock, win32api_mock, job_handle).

    Teardown removes the freshly loaded module and restores the original
    sys.modules entries so subsequent tests and test-suite imports are unaffected.
    """
    mock_win32job = MagicMock(name="win32job")
    mock_win32api = MagicMock(name="win32api")
    mock_pywintypes = MagicMock(name="pywintypes")
    mock_winerror = MagicMock(name="winerror")

    # Configure win32job constants used by the implementation.
    mock_win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    mock_win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    mock_win32job.JobObjectExtendedLimitInformation = _JOB_OBJ_EXT_LIMIT_INFO

    mock_job_handle = MagicMock(name="job_handle")
    mock_win32job.CreateJobObject.return_value = mock_job_handle
    mock_win32job.QueryInformationJobObject.return_value = {
        "BasicLimitInformation": {"LimitFlags": 0}
    }

    mock_pywintypes.error = _FakePywinError
    mock_winerror.ERROR_ACCESS_DENIED = _ERROR_ACCESS_DENIED

    proc_handle = MagicMock(name="proc_handle")
    mock_win32api.GetCurrentProcess.return_value = proc_handle

    # Snapshot existing sys.modules state.
    originals = {k: sys.modules.get(k) for k in _WIN32_DEPS}
    saved_mod = sys.modules.pop(_MODULE_KEY, None)

    # Inject mocks so the import below resolves them.
    sys.modules["win32job"] = mock_win32job
    sys.modules["win32api"] = mock_win32api
    sys.modules["pywintypes"] = mock_pywintypes
    sys.modules["winerror"] = mock_winerror

    mod = importlib.import_module(_MODULE_KEY)

    yield mod, mock_win32job, mock_win32api, mock_job_handle

    # --- teardown ---
    # Drop the module we loaded so the next test gets a fresh instance.
    sys.modules.pop(_MODULE_KEY, None)

    # Restore win32 stub/real modules.
    for k in _WIN32_DEPS:
        if originals[k] is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = originals[k]

    # Restore the previously loaded module, if any.
    if saved_mod is not None:
        sys.modules[_MODULE_KEY] = saved_mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_job_object_called_with_none_and_empty_string(win32_mod):
    """CreateJobObject must be called with (None, "") — unnamed, default DACL."""
    mod, win32job, _win32api, _job_handle = win32_mod
    mod.install_kill_on_exit()
    win32job.CreateJobObject.assert_called_once_with(None, "")


def test_limit_flags_include_kill_on_job_close(win32_mod):
    """KILL_ON_JOB_CLOSE flag must be set via SetInformationJobObject."""
    mod, win32job, _win32api, _job_handle = win32_mod
    mod.install_kill_on_exit()
    _, args, _ = win32job.SetInformationJobObject.mock_calls[0]
    info = args[2]
    assert info["BasicLimitInformation"]["LimitFlags"] & _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_limit_flags_include_silent_breakaway_ok(win32_mod):
    """SILENT_BREAKAWAY_OK flag must be set via SetInformationJobObject."""
    mod, win32job, _win32api, _job_handle = win32_mod
    mod.install_kill_on_exit()
    _, args, _ = win32job.SetInformationJobObject.mock_calls[0]
    info = args[2]
    assert info["BasicLimitInformation"]["LimitFlags"] & _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK


def test_assign_process_to_job_object_uses_get_current_process(win32_mod):
    """AssignProcessToJobObject must be called with GetCurrentProcess() handle."""
    mod, win32job, win32api, job_handle = win32_mod
    mod.install_kill_on_exit()
    proc_handle = win32api.GetCurrentProcess.return_value
    win32job.AssignProcessToJobObject.assert_called_once_with(job_handle, proc_handle)


def test_access_denied_returns_without_raising(win32_mod):
    """On ERROR_ACCESS_DENIED, install_kill_on_exit must return without raising."""
    mod, win32job, _win32api, _job_handle = win32_mod
    win32job.AssignProcessToJobObject.side_effect = _FakePywinError(
        _ERROR_ACCESS_DENIED, "AssignProcessToJobObject", "Access is denied."
    )
    mod.install_kill_on_exit()  # must NOT raise


def test_access_denied_does_not_retain_handle(win32_mod):
    """On ERROR_ACCESS_DENIED, _job_handle must remain None.

    Load-bearing: closing a handle we never used as a job member is safe;
    retaining it would risk GC killing us if we were ever accidentally assigned.
    """
    mod, win32job, _win32api, _job_handle = win32_mod
    win32job.AssignProcessToJobObject.side_effect = _FakePywinError(
        _ERROR_ACCESS_DENIED, "AssignProcessToJobObject", "Access is denied."
    )
    mod.install_kill_on_exit()
    assert mod._job_handle is None


def test_idempotent_calls_create_job_object_exactly_once(win32_mod):
    """Calling install_kill_on_exit() twice must invoke CreateJobObject once."""
    mod, win32job, _win32api, _job_handle = win32_mod
    mod.install_kill_on_exit()
    mod.install_kill_on_exit()  # second call — must be a no-op
    win32job.CreateJobObject.assert_called_once()


def test_success_retains_job_handle(win32_mod):
    """On success, _job_handle must be set to keep the Job Object alive."""
    mod, _win32job, _win32api, _job_handle = win32_mod
    mod.install_kill_on_exit()
    assert mod._job_handle is not None
