"""Tests for TrayApp (Issue #40 Commit D) — system-tray app with asymmetric fallback.

Covers all acceptance-criteria test cases:
- Menu construction: _build_menu() returns a pystray.Menu with the expected items.
- Mode dispatch: default → TrayApp.run(); --terminal → terminal path; JOB_CANNON_NO_TRAY=1 → terminal path.
- Fallback on Icon construction failure (pre-Flask): monkeypatched pystray.Icon raises → _run_terminal_mode_with_existing_app.
- Fallback on icon.run() failure BEFORE setup: icon.run raises without calling setup → _run_terminal_mode_with_existing_app.
- Fallback on icon.run() failure AFTER setup: setup fires (Flask thread started) then icon.run raises → _block_until_signal; Flask NOT torn down; create_app once.
- _shutdown_all idempotency: N calls → runtime_shutdown once.
- _shutdown_all ordering: runtime_shutdown before werkzeug_server.shutdown.
- _shutdown_all delegation: scheduler.shutdown / spawned.terminate NOT duplicated in _shutdown_all source.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pystray
import pytest

from job_finder.tray import TrayApp
from job_finder.web._runtime import reset_for_testing

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Reset the runtime idempotency guard around every test."""
    reset_for_testing()
    yield
    reset_for_testing()


def _make_tray(cfg=None):
    """Convenience: build a TrayApp with create_app mocked out."""
    if cfg is None:
        cfg = {}
    with patch("job_finder.web.create_app") as mock_create_app:
        mock_create_app.return_value = MagicMock()
        tray = TrayApp(cfg)
    return tray, mock_create_app.return_value


# ---------------------------------------------------------------------------
# Menu construction
# ---------------------------------------------------------------------------


class TestBuildMenu:
    def test_returns_pystray_menu_instance(self):
        """_build_menu() must return a pystray.Menu."""
        tray, _ = _make_tray()
        menu = tray._build_menu()
        assert isinstance(menu, pystray.Menu)

    def test_menu_has_seven_items(self):
        """Menu should have 5 named items + 2 separators = 7 total."""
        tray, _ = _make_tray()
        menu = tray._build_menu()
        items = list(menu)
        assert len(items) == 7

    def test_menu_contains_expected_named_items(self):
        """Named items must include Open Job Cannon, Pause scheduler, Open logs folder, Quit."""
        tray, _ = _make_tray()
        menu = tray._build_menu()
        sep_text = pystray.Menu.SEPARATOR.text  # '- - - -'
        texts = [i.text for i in menu if i.text != sep_text]
        assert "Open Job Cannon" in texts
        assert "Pause scheduler" in texts
        assert "Open logs folder" in texts
        assert "Quit" in texts

    def test_open_job_cannon_is_default(self):
        """'Open Job Cannon' must be the default item (double-click action)."""
        tray, _ = _make_tray()
        menu = tray._build_menu()
        open_items = [i for i in menu if i.text == "Open Job Cannon"]
        assert len(open_items) == 1
        assert open_items[0].default is True

    def test_two_separators_present(self):
        """Two separators must divide the menu sections."""
        tray, _ = _make_tray()
        menu = tray._build_menu()
        sep_text = pystray.Menu.SEPARATOR.text
        separators = [i for i in menu if i.text == sep_text]
        assert len(separators) == 2


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------


class TestModeDispatch:
    def test_default_invocation_calls_tray_run(self, monkeypatch):
        """Without --terminal or JOB_CANNON_NO_TRAY, TrayApp(cfg).run() is called."""
        monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)

        mock_tray_instance = MagicMock()

        with (
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.tray.TrayApp") as MockTrayApp,
        ):
            MockTrayApp.return_value = mock_tray_instance
            from job_finder.__main__ import main

            main()

        MockTrayApp.assert_called_once()
        mock_tray_instance.run.assert_called_once()

    def test_terminal_flag_skips_tray(self, monkeypatch):
        """--terminal flag must not construct TrayApp; terminal path runs instead."""
        monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)

        with (
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.__main__._run_terminal_mode") as mock_terminal,
            patch("job_finder.tray.TrayApp") as MockTrayApp,
        ):
            from job_finder.__main__ import main

            main()

        mock_terminal.assert_called_once()
        MockTrayApp.assert_not_called()

    def test_no_tray_env_skips_tray(self, monkeypatch):
        """JOB_CANNON_NO_TRAY=1 env var must behave identically to --terminal."""
        monkeypatch.setenv("JOB_CANNON_NO_TRAY", "1")

        with (
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.__main__._run_terminal_mode") as mock_terminal,
            patch("job_finder.tray.TrayApp") as MockTrayApp,
        ):
            from job_finder.__main__ import main

            main()

        mock_terminal.assert_called_once()
        MockTrayApp.assert_not_called()


# ---------------------------------------------------------------------------
# Fallback: Icon construction failure (pre-Flask)
# ---------------------------------------------------------------------------


class TestFallbackPreFlask:
    def test_icon_constructor_failure_calls_terminal_fallback(self):
        """When pystray.Icon raises, run() must call _run_terminal_mode_with_existing_app."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_app = MagicMock()
            mock_create_app.return_value = mock_app
            tray = TrayApp({})

        with (
            patch("pystray.Icon", side_effect=RuntimeError("no display")),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch.object(tray, "_run_terminal_mode_with_existing_app") as mock_fallback,
            patch.object(tray, "_shutdown_all"),
        ):
            tray.run()

        mock_fallback.assert_called_once()

    def test_icon_failure_reuses_existing_app(self):
        """The terminal fallback must reuse self.app (not call create_app again)."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_app = MagicMock()
            mock_create_app.return_value = mock_app
            tray = TrayApp({})

        with (
            patch("pystray.Icon", side_effect=RuntimeError("no display")),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            tray.run()

        # create_app called exactly ONCE total — in __init__ only.
        mock_create_app.assert_called_once()
        # The fallback calls self.app.run (the pre-existing app instance).
        mock_app.run.assert_called_once()

    def test_create_app_called_exactly_once_on_icon_failure(self):
        """Load-bearing M3 regression: create_app must be called exactly once even
        when Icon construction fails and terminal fallback runs."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        with (
            patch("pystray.Icon", side_effect=RuntimeError("no display")),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            tray.run()

        assert mock_create_app.call_count == 1, (
            "create_app must be called exactly once — in TrayApp.__init__, never again"
        )


# ---------------------------------------------------------------------------
# Fallback: icon.run() failure BEFORE setup callback fires
# ---------------------------------------------------------------------------


class TestFallbackIconRunFailsBeforeSetup:
    def test_run_failure_before_setup_calls_terminal_fallback(self):
        """icon.run() raises before setup fires → _run_terminal_mode_with_existing_app."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        # raise WITHOUT calling the setup callback → setup_fired stays False
        mock_icon.run.side_effect = RuntimeError("icon loop failed pre-setup")

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch.object(tray, "_run_terminal_mode_with_existing_app") as mock_fallback,
            patch.object(tray, "_shutdown_all"),
        ):
            tray.run()

        mock_fallback.assert_called_once()

    def test_run_failure_before_setup_create_app_once(self):
        """create_app must still be called exactly once when icon.run fails pre-setup."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        mock_icon.run.side_effect = RuntimeError("icon loop failed pre-setup")

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            tray.run()

        assert mock_create_app.call_count == 1


# ---------------------------------------------------------------------------
# Fallback: icon.run() failure AFTER setup callback fires (headless mode)
# ---------------------------------------------------------------------------


class TestFallbackIconRunFailsAfterSetup:
    def _make_icon_that_fires_setup_then_fails(self, mock_icon):
        """Return a side_effect that calls the setup kwarg then raises."""

        def _run_with_setup(setup=None):
            if setup is not None:
                setup(mock_icon)  # fire _on_setup → sets setup_fired = True
            raise RuntimeError("icon loop failed after setup")

        return _run_with_setup

    def test_run_failure_after_setup_calls_block_until_signal(self):
        """After setup fires, icon.run failure must invoke _block_until_signal (headless)."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        mock_icon.run.side_effect = self._make_icon_that_fires_setup_then_fails(mock_icon)

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.tray.TrayApp._run_flask"),  # no real Werkzeug
            patch.object(tray, "_block_until_signal") as mock_block,
            patch.object(tray, "_shutdown_all"),
        ):
            tray.run()

        mock_block.assert_called_once()

    def test_run_failure_after_setup_does_not_call_terminal_fallback(self):
        """After setup fires, the headless path must NOT call _run_terminal_mode_with_existing_app."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        mock_icon.run.side_effect = self._make_icon_that_fires_setup_then_fails(mock_icon)

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.tray.TrayApp._run_flask"),
            patch.object(tray, "_block_until_signal"),
            patch.object(tray, "_run_terminal_mode_with_existing_app") as mock_terminal,
            patch.object(tray, "_shutdown_all"),
        ):
            tray.run()

        mock_terminal.assert_not_called()

    def test_run_failure_after_setup_flask_thread_not_joined(self):
        """Flask thread must NOT be joined when tray fails after setup — the live
        server keeps serving while we block headless."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        mock_icon.run.side_effect = self._make_icon_that_fires_setup_then_fails(mock_icon)
        captured_threads: list = []

        import threading

        original_thread = threading.Thread

        def _capturing_thread(*args, **kwargs):
            t = MagicMock(spec=original_thread)
            t.start = MagicMock()
            t.join = MagicMock()
            captured_threads.append(t)
            return t

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.tray.TrayApp._run_flask"),
            patch("job_finder.tray.threading.Thread", side_effect=_capturing_thread),
            patch.object(tray, "_block_until_signal"),
            patch.object(tray, "_shutdown_all"),
        ):
            tray.run()

        assert captured_threads, "Flask thread should have been created after setup fired"
        for t in captured_threads:
            t.join.assert_not_called()

    def test_run_failure_after_setup_create_app_once(self):
        """create_app must be called exactly once even when tray fails after setup."""
        with patch("job_finder.web.create_app") as mock_create_app:
            mock_create_app.return_value = MagicMock()
            tray = TrayApp({})

        mock_icon = MagicMock()
        mock_icon.run.side_effect = self._make_icon_that_fires_setup_then_fails(mock_icon)

        with (
            patch("pystray.Icon", return_value=mock_icon),
            patch("job_finder.tray.TrayApp._load_icon", return_value=MagicMock()),
            patch("job_finder.tray.TrayApp._run_flask"),
            patch.object(tray, "_block_until_signal"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            tray.run()

        assert mock_create_app.call_count == 1


# ---------------------------------------------------------------------------
# _shutdown_all: idempotency
# ---------------------------------------------------------------------------


class TestShutdownAllIdempotency:
    def test_shutdown_all_called_n_times_invokes_runtime_shutdown_once(self):
        """Calling _shutdown_all() N times must invoke runtime_shutdown exactly once."""
        tray, _ = _make_tray()

        with patch("job_finder.web._runtime.runtime_shutdown") as mock_shutdown:
            tray._shutdown_all()
            tray._shutdown_all()
            tray._shutdown_all()

        mock_shutdown.assert_called_once()

    def test_shutdown_all_second_call_is_noop(self):
        """Second call must not call werkzeug_server.shutdown a second time."""
        tray, _ = _make_tray()
        mock_ws = MagicMock()
        tray.werkzeug_server = mock_ws

        with patch("job_finder.web._runtime.runtime_shutdown"):
            tray._shutdown_all()
            tray._shutdown_all()

        mock_ws.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# _shutdown_all: ordering
# ---------------------------------------------------------------------------


class TestShutdownAllOrdering:
    def test_runtime_shutdown_before_werkzeug_shutdown(self):
        """runtime_shutdown must be called BEFORE werkzeug_server.shutdown."""
        tray, _ = _make_tray()
        call_order: list[str] = []

        mock_ws = MagicMock()
        mock_ws.shutdown.side_effect = lambda: call_order.append("werkzeug")
        tray.werkzeug_server = mock_ws

        with patch(
            "job_finder.web._runtime.runtime_shutdown",
            side_effect=lambda: call_order.append("runtime"),
        ):
            tray._shutdown_all()

        assert call_order == ["runtime", "werkzeug"], (
            f"Expected ['runtime', 'werkzeug'], got {call_order}"
        )

    def test_shutdown_without_werkzeug_server_does_not_raise(self):
        """_shutdown_all must not raise when werkzeug_server is None (pre-tray-start)."""
        tray, _ = _make_tray()
        assert tray.werkzeug_server is None

        with patch("job_finder.web._runtime.runtime_shutdown"):
            tray._shutdown_all()  # must not raise


# ---------------------------------------------------------------------------
# _shutdown_all: delegation (no duplicated scheduler/terminate logic)
# ---------------------------------------------------------------------------


class TestShutdownAllDelegation:
    def test_shutdown_all_does_not_duplicate_scheduler_logic(self):
        """scheduler.shutdown / spawned.terminate must NOT appear in _shutdown_all source.
        The shared runtime_shutdown() helper is the single path for those operations."""
        source = inspect.getsource(TrayApp._shutdown_all)

        # These must be absent — only runtime_shutdown() is the delegate
        assert "scheduler.shutdown" not in source, (
            "_shutdown_all must not call scheduler.shutdown directly — delegate to runtime_shutdown()"
        )
        assert "spawned.terminate" not in source, (
            "_shutdown_all must not call spawned.terminate directly — delegate to runtime_shutdown()"
        )
        # Delegation call must be present
        assert "runtime_shutdown" in source, (
            "_shutdown_all must call runtime_shutdown() for scheduler + Ollama teardown"
        )

    def test_shutdown_all_delegates_to_runtime_shutdown(self):
        """Verify runtime_shutdown is actually invoked (not just referenced)."""
        tray, _ = _make_tray()

        with patch("job_finder.web._runtime.runtime_shutdown") as mock_rs:
            tray._shutdown_all()

        mock_rs.assert_called_once()
