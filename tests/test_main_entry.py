"""Tests for the ``job-cannon`` console-script entry point (job_finder/__main__.py).

These tests cover the UAT 2026-05-21 F2 / Issue #290 behaviour: print a URL
banner and open the user's browser ~1.5 s after launch in BOTH terminal mode
and tray mode, with ``JOB_CANNON_NO_BROWSER=1`` as the opt-out switch.

We don't actually call ``main()`` end-to-end (it would block on
``app.run()``); instead we exercise ``_open_browser`` and
``_print_startup_banner`` directly and verify the URL-banner /
timer-scheduling logic via patching.

Note (Issue #40): tray mode is now the default launch mode, so the tests
that exercise terminal-mode serving pass ``--terminal`` explicitly. The
assertions are unchanged — they still verify the banner / Timer / app.run
wiring, now reached via the explicit flag instead of the bare invocation.
"""

from unittest.mock import MagicMock, patch

from job_finder import __main__ as main_mod


def test_no_utcnow_deprecation_warning(recwarn):
    """__main__.py must not emit DeprecationWarning for datetime.utcnow().

    The metadata dict in main() previously used datetime.utcnow() which emits
    a DeprecationWarning in Python 3.12+.  This test imports the module and
    checks that no such warning is present.
    """
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        import importlib

        import job_finder.__main__

        importlib.reload(job_finder.__main__)

    utcnow_warnings = [
        warning
        for warning in w
        if issubclass(warning.category, DeprecationWarning)
        and "utcnow" in str(warning.message).lower()
    ]
    assert not utcnow_warnings, f"utcnow DeprecationWarning found: {utcnow_warnings}"


# ---------------------------------------------------------------------------
# _print_startup_banner unit tests (Issue #290)
# ---------------------------------------------------------------------------


def test_print_startup_banner_terminal_mode_prints_url(monkeypatch, capsys):
    """Terminal mode banner prints 'Job Cannon is starting on <url>' without
    the tray-icon hint."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    with patch("job_finder.__main__.threading.Timer"):
        main_mod._print_startup_banner("http://127.0.0.1:5000", tray_mode=False)
    captured = capsys.readouterr()
    assert "Job Cannon is starting on http://127.0.0.1:5000" in captured.out
    assert "tray icon" not in captured.out


def test_print_startup_banner_tray_mode_prints_url_and_hint(monkeypatch, capsys):
    """Tray mode banner includes both the URL and the tray-icon hint."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    with patch("job_finder.__main__.threading.Timer"):
        main_mod._print_startup_banner("http://127.0.0.1:5000", tray_mode=True)
    captured = capsys.readouterr()
    assert "Job Cannon is starting on http://127.0.0.1:5000" in captured.out
    assert "tray icon" in captured.out


def test_print_startup_banner_schedules_timer_when_no_browser_not_set(monkeypatch, capsys):
    """Without JOB_CANNON_NO_BROWSER, _print_startup_banner schedules the Timer
    and returns it."""
    monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)
    fake_timer = MagicMock()
    with patch("job_finder.__main__.threading.Timer", return_value=fake_timer) as mock_timer_cls:
        result = main_mod._print_startup_banner("http://127.0.0.1:5000", tray_mode=False)
    mock_timer_cls.assert_called_once()
    call = mock_timer_cls.call_args
    assert call.args[0] == main_mod._BROWSER_OPEN_DELAY_SEC
    assert call.args[1] is main_mod._open_browser
    assert call.kwargs["args"] == ("http://127.0.0.1:5000",)
    fake_timer.start.assert_called_once()
    assert result is fake_timer


def test_print_startup_banner_no_timer_when_no_browser_set(monkeypatch, capsys):
    """JOB_CANNON_NO_BROWSER=1 suppresses the Timer; returns None."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    with patch("job_finder.__main__.threading.Timer") as mock_timer_cls:
        result = main_mod._print_startup_banner("http://127.0.0.1:5000", tray_mode=False)
    mock_timer_cls.assert_not_called()
    assert result is None


def test_open_browser_calls_webbrowser_open():
    """_open_browser delegates to webbrowser.open with new=2 (new tab)."""
    with patch("job_finder.__main__.webbrowser.open") as mock_open:
        main_mod._open_browser("http://127.0.0.1:5000")
        mock_open.assert_called_once_with("http://127.0.0.1:5000", new=2)


def test_open_browser_swallows_exceptions(caplog):
    """If webbrowser.open raises (headless / SSH / locked-down session), the
    failure must not propagate. The app's job is to serve requests, not to
    crash on a missing X server."""
    with patch(
        "job_finder.__main__.webbrowser.open",
        side_effect=RuntimeError("no display"),
    ):
        with caplog.at_level("WARNING", logger="job_finder.__main__"):
            main_mod._open_browser("http://127.0.0.1:5000")

        # Warning was logged but no exception escaped.
        assert any("Could not open browser" in record.getMessage() for record in caplog.records)


def test_main_no_browser_env_var_skips_timer_and_message(monkeypatch, capsys):
    """JOB_CANNON_NO_BROWSER=1 disables both the Timer and the "Opening your
    browser…" line. The URL banner still prints; it's the only stable place
    for the user to copy the URL when running headless."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    fake_app = MagicMock()
    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.threading.Timer") as mock_timer,
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        # Probe / port checks must be stubbed so the test doesn't depend on
        # what's actually bound on port 5000 during the test run.
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        main_mod.main()

    captured = capsys.readouterr()
    assert "Job Cannon is starting on" in captured.out
    assert "Opening your browser" not in captured.out
    mock_timer.assert_not_called()
    fake_app.run.assert_called_once()


def test_main_default_schedules_browser_open(monkeypatch, capsys):
    """Without the opt-out, main() prints the banner AND schedules the timer."""
    monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)

    fake_app = MagicMock()
    fake_timer = MagicMock()

    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.threading.Timer", return_value=fake_timer) as mock_timer_class,
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        main_mod.main()

    captured = capsys.readouterr()
    assert "Job Cannon is starting on http://127.0.0.1:5000" in captured.out
    assert "Opening your browser" in captured.out

    # Timer constructed with the documented delay + _open_browser callable
    # + URL passed positionally.
    mock_timer_class.assert_called_once()
    call = mock_timer_class.call_args
    assert call.args[0] == main_mod._BROWSER_OPEN_DELAY_SEC
    assert call.args[1] is main_mod._open_browser
    assert call.kwargs["args"] == ("http://127.0.0.1:5000",)
    fake_timer.start.assert_called_once()


def test_main_respects_server_overrides_in_config(monkeypatch, capsys):
    """The URL banner uses the client-host and port from config.

    When bind_host is a wildcard (0.0.0.0), the URL banner must show
    127.0.0.1 (not 0.0.0.0 — that's not a browser URL). app.run() still
    receives the original bind_host for the actual bind.
    """
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")  # silence the timer

    cfg = {"server": {"host": "0.0.0.0", "port": 8080, "debug": False}}
    fake_app = MagicMock()
    with (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        main_mod.main()

    captured = capsys.readouterr()
    # Banner shows client-accessible URL (127.0.0.1, not 0.0.0.0)
    assert "http://127.0.0.1:8080" in captured.out
    # app.run still uses the original bind_host so the socket binds to all interfaces
    fake_app.run.assert_called_once_with(
        host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True
    )


def test_main_passes_use_reloader_false(monkeypatch):
    """Regression: do not regress use_reloader=False — the Werkzeug reloader
    would spawn a second process and double-fire the browser-open Timer."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    fake_app = MagicMock()
    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["use_reloader"] is False
    # threaded=True is load-bearing for the SSE live-update stream: a single
    # held-open /events connection would otherwise block every other request.
    assert fake_app.run.call_args.kwargs["threaded"] is True
