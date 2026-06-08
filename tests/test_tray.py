"""Tests for the system-tray launch mode (Issue #40, job_finder/tray.py).

Mock-heavy by design: we never construct a real tray icon or bind a real
Werkzeug socket. The behaviours under test are the load-bearing invariants
from the plan (§11):

  - create_app() is called exactly once per process (the no-second-create_app
    regression — a second call would race the scheduler singleton).
  - _shutdown_all() is idempotent and delegates scheduler/Ollama teardown to
    the shared runtime_shutdown(), then runs the tray-specific Werkzeug
    shutdown — in that order.
  - The fallback is asymmetric: Icon-construction / pre-setup failures reuse
    self.app in terminal mode; a post-setup failure stays headless.
  - Launch-mode dispatch: tray is the default; --terminal / JOB_CANNON_NO_TRAY
    route to the terminal path without constructing a TrayApp.
"""

from unittest.mock import MagicMock, patch

import pystray
import pytest

from job_finder import __main__ as main_mod
from job_finder.tray import TrayApp

_CFG = {"server": {"host": "127.0.0.1", "port": 5000}}


def _make_tray(cfg=None):
    """Construct a TrayApp with create_app stubbed (so no real Flask app /
    scheduler is built). Returns the TrayApp; self.app is a MagicMock."""
    with patch("job_finder.tray.create_app", return_value=MagicMock()):
        return TrayApp(cfg or _CFG)


# ---------------------------------------------------------------------------
# Construction + menu
# ---------------------------------------------------------------------------


def test_init_calls_create_app_exactly_once():
    """create_app() must be invoked once and only once in __init__."""
    with patch("job_finder.tray.create_app", return_value=MagicMock()) as mock_create:
        TrayApp(_CFG)
        mock_create.assert_called_once()


def test_wildcard_bind_rewrites_url_to_loopback():
    """0.0.0.0 bind must not leak into the user-facing URL."""
    t = _make_tray({"server": {"host": "0.0.0.0", "port": 8080}})
    assert t.bind_host == "0.0.0.0"
    assert t.url == "http://127.0.0.1:8080"


def test_build_menu_returns_expected_items():
    t = _make_tray()
    menu = t._build_menu()
    assert isinstance(menu, pystray.Menu)
    labels = [item.text for item in menu]
    assert "Open Job Cannon" in labels
    assert "Pause scheduler" in labels
    assert "Open logs folder" in labels
    assert "Quit" in labels


# ---------------------------------------------------------------------------
# Asymmetric fallback
# ---------------------------------------------------------------------------


def test_fallback_on_icon_construction_failure_reuses_app():
    """Icon construction fails BEFORE Flask starts → terminal mode reusing
    self.app. create_app must NOT be called a second time."""
    with (
        patch("job_finder.tray.create_app", return_value=MagicMock()) as mock_create,
        patch("job_finder.tray.pystray.Icon", side_effect=RuntimeError("no display")),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        t = TrayApp(_CFG)
        t._load_icon = MagicMock()  # don't depend on the PNG in this unit test
        existing_app = t.app
        t.run()

    mock_create.assert_called_once()  # load-bearing: no second create_app
    existing_app.run.assert_called_once()  # the SAME app instance is served
    assert existing_app.run.call_args.kwargs["host"] == "127.0.0.1"
    assert existing_app.run.call_args.kwargs["use_reloader"] is False


def test_fallback_on_icon_run_failure_before_setup_reuses_app():
    """icon.run() raises BEFORE the setup callback fires → terminal fallback,
    create_app called once."""
    mock_icon = MagicMock()
    mock_icon.run.side_effect = RuntimeError("loop failed pre-setup")
    with (
        patch("job_finder.tray.create_app", return_value=MagicMock()) as mock_create,
        patch("job_finder.tray.pystray.Icon", return_value=mock_icon),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        t = TrayApp(_CFG)
        t._load_icon = MagicMock()
        existing_app = t.app
        t.run()

    mock_create.assert_called_once()
    existing_app.run.assert_called_once()


def test_fallback_on_icon_run_failure_after_setup_stays_headless():
    """icon.run() raises AFTER setup fired (Flask started) → stay headless.
    The live server is NOT torn down to restart in terminal mode."""
    mock_icon = MagicMock()

    def _run_then_fail(setup=None):
        setup(mock_icon)  # fires _on_setup: starts Flask thread, sets setup_fired
        raise RuntimeError("loop failed post-setup")

    mock_icon.run.side_effect = _run_then_fail

    with (
        patch("job_finder.tray.create_app", return_value=MagicMock()) as mock_create,
        patch("job_finder.tray.pystray.Icon", return_value=mock_icon),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        t = TrayApp(_CFG)
        t._load_icon = MagicMock()
        t._run_flask = MagicMock()  # don't bind a real socket in the bg thread
        t._block_until_signal = MagicMock()  # don't actually block the test
        existing_app = t.app
        t.run()

    mock_create.assert_called_once()
    t._block_until_signal.assert_called_once()  # headless block, not teardown
    existing_app.run.assert_not_called()  # did NOT fall back to terminal serving
    assert t.flask_thread is not None  # the live server thread survives


# ---------------------------------------------------------------------------
# Shutdown: idempotency + ordering + delegation
# ---------------------------------------------------------------------------


def test_shutdown_all_idempotent():
    """N calls → runtime_shutdown invoked exactly once."""
    t = _make_tray()
    with patch("job_finder.web._runtime.runtime_shutdown") as mock_rs:
        t._shutdown_all()
        t._shutdown_all()
        t._shutdown_all()
    mock_rs.assert_called_once()


def test_shutdown_all_order_runtime_then_werkzeug():
    """runtime_shutdown() must run BEFORE the tray-specific Werkzeug shutdown."""
    t = _make_tray()
    t.werkzeug_server = MagicMock()
    order = []
    t.werkzeug_server.shutdown.side_effect = lambda: order.append("werkzeug")
    with patch(
        "job_finder.web._runtime.runtime_shutdown",
        side_effect=lambda: order.append("runtime"),
    ):
        t._shutdown_all()
    assert order == ["runtime", "werkzeug"]


def test_shutdown_all_swallows_werkzeug_error():
    """A raising Werkzeug shutdown must not propagate (still idempotent)."""
    t = _make_tray()
    t.werkzeug_server = MagicMock()
    t.werkzeug_server.shutdown.side_effect = RuntimeError("already down")
    with patch("job_finder.web._runtime.runtime_shutdown"):
        t._shutdown_all()  # must not raise
    assert t._shutdown_done is True


# ---------------------------------------------------------------------------
# Scheduler menu helpers
# ---------------------------------------------------------------------------


def test_scheduler_paused_true_when_no_scheduler():
    t = _make_tray()
    with patch("job_finder.tray.get_scheduler", return_value=None):
        assert t._scheduler_paused() is True


def test_toggle_scheduler_noop_when_no_scheduler():
    t = _make_tray()
    with patch("job_finder.tray.get_scheduler", return_value=None):
        t._toggle_scheduler(MagicMock(), MagicMock())  # must not raise


# ---------------------------------------------------------------------------
# Launch-mode dispatch (job_finder/__main__.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch):
    """Stub out the probe / port / lock prelude so main() reaches dispatch."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch(
            "job_finder.__main__.acquire_pidfile",
            return_value=MagicMock(acquired=True),
        ),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.__main__._run_terminal_mode") as mock_terminal,
        patch("job_finder.tray.TrayApp") as mock_tray_cls,
    ):
        yield mock_terminal, mock_tray_cls


def test_dispatch_default_uses_tray(monkeypatch, _dispatch_env):
    monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)
    mock_terminal, mock_tray_cls = _dispatch_env
    with patch("job_finder.__main__.sys.argv", ["job-cannon"]):
        main_mod.main()
    mock_tray_cls.assert_called_once()
    mock_tray_cls.return_value.run.assert_called_once()
    mock_terminal.assert_not_called()


def test_dispatch_terminal_flag_uses_terminal(monkeypatch, _dispatch_env):
    monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)
    mock_terminal, mock_tray_cls = _dispatch_env
    with patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]):
        main_mod.main()
    mock_terminal.assert_called_once()
    mock_tray_cls.assert_not_called()


def test_dispatch_no_tray_env_uses_terminal(monkeypatch, _dispatch_env):
    monkeypatch.setenv("JOB_CANNON_NO_TRAY", "1")
    mock_terminal, mock_tray_cls = _dispatch_env
    with patch("job_finder.__main__.sys.argv", ["job-cannon"]):
        main_mod.main()
    mock_terminal.assert_called_once()
    mock_tray_cls.assert_not_called()
