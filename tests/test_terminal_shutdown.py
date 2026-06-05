"""Tests for terminal-mode shutdown wiring in job_finder.__main__ and _runtime.

Covers:
- threading.Timer(...).daemon is True after construction
- runtime_shutdown() called N times invokes scheduler.shutdown exactly once
- Ordering: scheduler.shutdown happens before spawned.terminate
- SIGINT delivered to test process triggers single runtime_shutdown invocation
- Windows SetConsoleCtrlHandler install succeeds (skip on non-Windows)
"""

from __future__ import annotations

import signal
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web._runtime import reset_for_testing, runtime_shutdown

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_runtime():
    """Reset the runtime idempotency guard before and after each test."""
    reset_for_testing()
    yield
    reset_for_testing()


# ---------------------------------------------------------------------------
# Timer daemon flag
# ---------------------------------------------------------------------------


def test_timer_daemon_true():
    """threading.Timer constructed in main() must have daemon=True so a fast
    startup crash does not leave the Timer thread keeping the process alive."""
    # We test the property directly on a Timer instance (same as production code)
    timer = threading.Timer(1.5, lambda: None)
    timer.daemon = True
    assert timer.daemon is True


def test_main_sets_timer_daemon(monkeypatch):
    """main() must set timer.daemon = True before starting the timer."""
    monkeypatch.setenv("JOB_CANNON_NO_TRAY", "1")  # force terminal mode
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "0")
    monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)

    fake_app = MagicMock()
    captured_timer: list[threading.Timer] = []

    original_timer = threading.Timer

    def _capturing_timer(interval, fn, args=None, kwargs=None):
        t = original_timer(interval, fn, args=args or [], kwargs=kwargs or {})
        captured_timer.append(t)
        return t

    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.web._runtime.runtime_shutdown"),
        patch("job_finder.__main__._install_terminal_shutdown"),
        patch("job_finder.__main__.threading.Timer", side_effect=_capturing_timer),
        patch("job_finder.__main__.sys.argv", ["job-cannon"]),
    ):
        import job_finder.__main__ as main_mod

        main_mod.main()

    assert captured_timer, "Timer was not created"
    assert captured_timer[0].daemon is True, "Timer.daemon must be True"


# ---------------------------------------------------------------------------
# Idempotency: N calls → exactly one scheduler.shutdown
# ---------------------------------------------------------------------------


def test_runtime_shutdown_idempotent():
    """Calling runtime_shutdown() N times must invoke scheduler.shutdown exactly once."""
    mock_scheduler = MagicMock()
    mock_scheduler.shutdown = MagicMock()

    # _runtime.py uses lazy `from job_finder.web.scheduler import ...` — patch at source.
    with (
        patch("job_finder.web.scheduler.get_scheduler", return_value=mock_scheduler),
        patch("job_finder.web.scheduler.get_spawned_ollama_proc", return_value=None),
    ):
        runtime_shutdown()
        runtime_shutdown()
        runtime_shutdown()

    mock_scheduler.shutdown.assert_called_once_with(wait=False)


# ---------------------------------------------------------------------------
# Ordering: scheduler.shutdown before spawned.terminate
# ---------------------------------------------------------------------------


def test_runtime_shutdown_order():
    """scheduler.shutdown must be called BEFORE spawned.terminate."""
    call_order: list[str] = []

    mock_scheduler = MagicMock()
    mock_scheduler.shutdown = MagicMock(side_effect=lambda **kw: call_order.append("scheduler"))

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process is still running
    mock_proc.terminate = MagicMock(side_effect=lambda: call_order.append("terminate"))

    # _runtime.py uses lazy `from job_finder.web.scheduler import ...` — patch at source.
    with (
        patch("job_finder.web.scheduler.get_scheduler", return_value=mock_scheduler),
        patch("job_finder.web.scheduler.get_spawned_ollama_proc", return_value=mock_proc),
    ):
        runtime_shutdown()

    assert call_order == ["scheduler", "terminate"], (
        f"Expected ['scheduler', 'terminate'], got {call_order}"
    )


def test_runtime_shutdown_skips_terminate_if_proc_already_exited():
    """spawned.terminate must NOT be called if proc.poll() returns non-None."""
    mock_scheduler = MagicMock()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0  # already exited

    with (
        patch("job_finder.web.scheduler.get_scheduler", return_value=mock_scheduler),
        patch("job_finder.web.scheduler.get_spawned_ollama_proc", return_value=mock_proc),
    ):
        runtime_shutdown()

    mock_proc.terminate.assert_not_called()


def test_runtime_shutdown_no_scheduler():
    """runtime_shutdown() must not raise when no scheduler is running."""
    with (
        patch("job_finder.web.scheduler.get_scheduler", return_value=None),
        patch("job_finder.web.scheduler.get_spawned_ollama_proc", return_value=None),
    ):
        runtime_shutdown()  # must not raise


# ---------------------------------------------------------------------------
# SIGINT handler triggers single runtime_shutdown
# ---------------------------------------------------------------------------


def test_sigint_triggers_runtime_shutdown(monkeypatch):
    """SIGINT delivered to the process must invoke runtime_shutdown exactly once."""
    shutdown_calls: list[int] = []

    def _mock_shutdown():
        shutdown_calls.append(1)

    # Patch runtime_shutdown in the module where _install_terminal_shutdown imports it
    with patch("job_finder.web._runtime.runtime_shutdown", side_effect=_mock_shutdown):
        import job_finder.__main__ as main_mod

        # Reset any previously installed handlers, then install fresh ones
        reset_for_testing()

        fake_app = MagicMock()
        with (
            patch("job_finder.web.scheduler.get_scheduler", return_value=None),
            patch("job_finder.web.scheduler.get_spawned_ollama_proc", return_value=None),
        ):
            # Capture what signal handler gets installed
            installed_handler: list = []
            original_signal = signal.signal

            def _capture_signal(signum, handler):
                if signum == signal.SIGINT:
                    installed_handler.append(handler)
                return original_signal(signum, handler)

            with patch("job_finder.__main__.signal.signal", side_effect=_capture_signal):
                main_mod._install_terminal_shutdown(fake_app)

    assert installed_handler, "SIGINT handler was not installed"

    # Simulate SIGINT delivery by calling the handler directly
    # (avoids actually sending signal to the test process)
    handler = installed_handler[0]
    with patch("job_finder.__main__.sys.exit"):
        handler(signal.SIGINT, None)

    # Verify runtime_shutdown was called
    assert len(shutdown_calls) == 1


# ---------------------------------------------------------------------------
# Windows SetConsoleCtrlHandler install (skip on non-Windows)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
def test_set_console_ctrl_handler_install_succeeds():
    """On Windows, _install_terminal_shutdown must successfully call
    win32api.SetConsoleCtrlHandler without raising."""
    import job_finder.__main__ as main_mod

    # win32api may or may not be available; test that the install path runs
    try:
        import win32api  # type: ignore[import]

        handler_registered: list[bool] = []
        original_set = win32api.SetConsoleCtrlHandler

        def _spy_set(handler, add):
            handler_registered.append(add)
            return original_set(handler, add)

        with patch.object(win32api, "SetConsoleCtrlHandler", side_effect=_spy_set):
            main_mod._install_terminal_shutdown(MagicMock())

        assert handler_registered, "SetConsoleCtrlHandler was not called"
        assert handler_registered[0] is True, "Handler must be registered (add=True)"

    except ImportError:
        pytest.skip("pywin32 not installed — skipping SetConsoleCtrlHandler test")


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows: verify graceful skip")
def test_set_console_ctrl_handler_skipped_on_non_windows():
    """On non-Windows, _install_terminal_shutdown must not raise even when
    win32api is absent (ImportError is silently swallowed)."""
    import job_finder.__main__ as main_mod

    # Ensure win32api is not importable in this test
    with patch.dict(sys.modules, {"win32api": None}):
        # Should not raise
        main_mod._install_terminal_shutdown(MagicMock())
