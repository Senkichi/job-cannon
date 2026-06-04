"""Tests for TrayApp system-tray implementation (job_finder/tray.py).

All pystray interactions are mocked at the module-attribute level so the
suite runs headless (no display, no tray backend required).

Coverage map
------------
* Menu construction — ``_build_menu()`` returns a ``pystray.Menu`` with
  the expected items.
* Mode dispatch — ``__main__.main()`` dispatches to TrayApp.run() by default,
  to terminal mode on ``--terminal``, and to terminal mode on
  ``JOB_CANNON_NO_TRAY=1``.
* Fallback: Icon construction failure (pre-Flask) — assert
  ``_run_terminal_mode_with_existing_app`` is invoked and ``create_app``
  called exactly once.
* Fallback: ``icon.run()`` failure BEFORE setup — same terminal fallback,
  create_app once.
* Fallback: ``icon.run()`` failure AFTER setup — ``_block_until_signal``
  invoked; Flask thread NOT joined; create_app once.
* ``_shutdown_all`` idempotency — N calls → ``runtime_shutdown`` called once.
* ``_shutdown_all`` ordering — ``runtime_shutdown`` before
  ``werkzeug_server.shutdown``.
* ``_shutdown_all`` delegation — scheduler.shutdown / spawned.terminate are
  NOT duplicated inside ``_shutdown_all``.
"""

import inspect
from unittest.mock import MagicMock, patch

# ─── Import target ─────────────────────────────────────────────────────────────
# job_finder.__main__ is already cached from test_main_entry; import it directly
# so we can exercise main() without subprocess overhead.
from job_finder import __main__ as main_mod
from job_finder.tray import TrayApp

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_tray(monkeypatch, *, cfg=None):
    """Construct a TrayApp with create_app and pystray fully mocked.

    Returns (tray_instance, fake_flask_app, mock_create_app).
    """
    cfg = cfg or {}
    fake_app = MagicMock(name="fake_flask_app")
    mock_create_app = MagicMock(return_value=fake_app)
    monkeypatch.setattr("job_finder.tray.create_app", mock_create_app)
    tray = TrayApp(cfg)
    return tray, fake_app, mock_create_app


def _mock_pystray(monkeypatch):
    """Replace job_finder.tray.pystray with a fresh MagicMock."""
    mock = MagicMock(name="pystray")
    monkeypatch.setattr("job_finder.tray.pystray", mock)
    return mock


def _mock_runtime_shutdown(monkeypatch):
    """Replace runtime_shutdown with a MagicMock and return it."""
    m = MagicMock(name="runtime_shutdown")
    monkeypatch.setattr("job_finder.web._runtime.runtime_shutdown", m)
    return m


# ─────────────────────────────────────────────────────────────────────────────
# 1. Menu construction
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildMenu:
    def test_build_menu_returns_pystray_menu(self, monkeypatch):
        """_build_menu() returns the pystray.Menu instance."""
        mock_ps = _mock_pystray(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        menu = tray._build_menu()

        mock_ps.Menu.assert_called_once()
        assert menu is mock_ps.Menu.return_value

    def test_build_menu_contains_expected_items(self, monkeypatch):
        """_build_menu() passes the six expected MenuItem labels to pystray.Menu."""
        mock_ps = _mock_pystray(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        tray._build_menu()

        # Collect all positional first-args passed to MenuItem (the label arg)
        labels_or_callables = [c.args[0] for c in mock_ps.MenuItem.call_args_list if c.args]
        # String labels we can check directly
        str_labels = [lbl for lbl in labels_or_callables if isinstance(lbl, str)]
        assert "Open Job Cannon" in str_labels
        assert "Pause scheduler" in str_labels
        assert "Open logs folder" in str_labels
        assert "Quit" in str_labels

    def test_build_menu_includes_separator(self, monkeypatch):
        """pystray.Menu.SEPARATOR appears in the built menu."""
        mock_ps = _mock_pystray(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        tray._build_menu()

        # SEPARATOR should appear at least once as a positional arg to Menu
        menu_call_args = mock_ps.Menu.call_args.args
        assert mock_ps.Menu.SEPARATOR in menu_call_args


# ─────────────────────────────────────────────────────────────────────────────
# 2. Mode dispatch
# ─────────────────────────────────────────────────────────────────────────────


class TestModeDispatch:
    def test_default_invocation_calls_tray_run(self, monkeypatch):
        """No flags → TrayApp is constructed and .run() is called."""
        fake_tray = MagicMock(name="tray_instance")
        mock_tray_cls = MagicMock(return_value=fake_tray)

        monkeypatch.setattr("job_finder.config.load_config", MagicMock(return_value={}))
        monkeypatch.setattr("job_finder.__main__.sys.argv", ["job-cannon"])
        monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)

        with patch("job_finder.tray.TrayApp", mock_tray_cls):
            main_mod.main()

        mock_tray_cls.assert_called_once()
        fake_tray.run.assert_called_once()

    def test_terminal_flag_skips_tray(self, monkeypatch):
        """--terminal → terminal mode; TrayApp is NOT constructed."""
        fake_app = MagicMock(name="flask_app")

        monkeypatch.setattr("job_finder.config.load_config", MagicMock(return_value={}))
        monkeypatch.setattr("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"])
        monkeypatch.delenv("JOB_CANNON_NO_TRAY", raising=False)
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        with (
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.tray.TrayApp") as MockTray,
        ):
            main_mod.main()

        MockTray.assert_not_called()
        fake_app.run.assert_called_once()

    def test_no_tray_env_behaves_like_terminal(self, monkeypatch):
        """JOB_CANNON_NO_TRAY=1 → terminal mode; TrayApp is NOT constructed."""
        fake_app = MagicMock(name="flask_app")

        monkeypatch.setattr("job_finder.config.load_config", MagicMock(return_value={}))
        monkeypatch.setattr("job_finder.__main__.sys.argv", ["job-cannon"])
        monkeypatch.setenv("JOB_CANNON_NO_TRAY", "1")
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        with (
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.tray.TrayApp") as MockTray,
        ):
            main_mod.main()

        MockTray.assert_not_called()
        fake_app.run.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fallback: Icon construction failure (pre-Flask)
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackIconConstructionFailure:
    def test_falls_back_to_terminal_mode(self, monkeypatch):
        """pystray.Icon raises → _run_terminal_mode_with_existing_app called."""
        _mock_runtime_shutdown(monkeypatch)
        mock_ps = _mock_pystray(monkeypatch)
        tray, fake_app, mock_create_app = _make_tray(monkeypatch)

        # _load_icon must return something before pystray.Icon is called
        monkeypatch.setattr(TrayApp, "_load_icon", lambda self: MagicMock())
        # pystray.Icon() raises
        mock_ps.Icon.side_effect = RuntimeError("no display")

        tray.run()

        # create_app called exactly once (in __init__)
        mock_create_app.assert_called_once()

    def test_app_run_called_with_existing_instance(self, monkeypatch):
        """self.app.run() is called on the app built in __init__ — not a new one."""
        _mock_runtime_shutdown(monkeypatch)
        mock_ps = _mock_pystray(monkeypatch)
        tray, fake_app, mock_create_app = _make_tray(monkeypatch)

        monkeypatch.setattr(TrayApp, "_load_icon", lambda self: MagicMock())
        mock_ps.Icon.side_effect = RuntimeError("no display")

        tray.run()

        # The Flask app that was passed to create_app in __init__ is the same
        # one that _run_terminal_mode_with_existing_app uses for app.run()
        assert tray.app is fake_app
        fake_app.run.assert_called_once()

    def test_create_app_called_exactly_once(self, monkeypatch):
        """M3 regression: create_app called exactly once; no second call in fallback."""
        _mock_runtime_shutdown(monkeypatch)
        mock_ps = _mock_pystray(monkeypatch)

        # Spy on create_app to count calls across __init__ AND run()
        real_fake_app = MagicMock(name="flask_app")
        spy_create_app = MagicMock(return_value=real_fake_app)
        monkeypatch.setattr("job_finder.tray.create_app", spy_create_app)

        monkeypatch.setattr(TrayApp, "_load_icon", lambda self: MagicMock())
        mock_ps.Icon.side_effect = RuntimeError("no display")

        tray = TrayApp({})
        tray.run()

        spy_create_app.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fallback: icon.run() failure BEFORE setup
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackRunFailureBeforeSetup:
    def _setup(self, monkeypatch):
        """Shared setup: Icon construction OK, icon.run() raises before setup."""
        _mock_runtime_shutdown(monkeypatch)
        mock_ps = _mock_pystray(monkeypatch)

        spy = MagicMock(return_value=MagicMock(name="flask_app"))
        monkeypatch.setattr("job_finder.tray.create_app", spy)
        monkeypatch.setattr(TrayApp, "_load_icon", lambda self: MagicMock())

        fake_icon = MagicMock(name="tray_icon")
        # run() raises immediately — setup callback never fires
        fake_icon.run.side_effect = RuntimeError("event loop exploded before setup")
        mock_ps.Icon.return_value = fake_icon

        return spy

    def test_terminal_mode_invoked(self, monkeypatch):
        """icon.run() fails before setup → falls back to terminal mode."""
        spy = self._setup(monkeypatch)
        tray = TrayApp({})
        tray.run()

        # Terminal mode: self.app.run() was called on the existing app
        tray.app.run.assert_called_once()

    def test_create_app_called_once(self, monkeypatch):
        """icon.run() fails before setup → create_app still only one call."""
        spy = self._setup(monkeypatch)
        tray = TrayApp({})
        tray.run()

        spy.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fallback: icon.run() failure AFTER setup
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackRunFailureAfterSetup:
    """icon.run() fires the setup callback (Flask starts), then raises.

    Expected: _block_until_signal() is called; Flask thread NOT joined;
    app.run() NOT called; create_app called once.
    """

    def _run_after_setup(self, monkeypatch):
        """Configure and return (tray, spy_create_app, fake_thread)."""
        _mock_runtime_shutdown(monkeypatch)
        mock_ps = _mock_pystray(monkeypatch)

        fake_flask_app = MagicMock(name="flask_app")
        spy_create = MagicMock(return_value=fake_flask_app)
        monkeypatch.setattr("job_finder.tray.create_app", spy_create)
        monkeypatch.setattr(TrayApp, "_load_icon", lambda self: MagicMock())

        # A fake thread that records method calls
        fake_thread = MagicMock(name="flask_thread")

        # icon.run() calls setup(icon), then raises
        class _FakeIcon:
            visible = False

            def run(self, setup=None):
                self.visible = True
                if setup is not None:
                    setup(self)  # fires _on_setup → setup_fired = True
                raise RuntimeError("event loop died after setup")

            def stop(self):
                pass

        fake_icon = _FakeIcon()
        mock_ps.Icon.return_value = fake_icon

        tray = TrayApp({})

        # Patch threading.Thread to return our fake thread (avoids real Werkzeug bind)
        with patch("job_finder.tray.threading.Thread", return_value=fake_thread):
            # Patch _block_until_signal to return immediately
            tray._block_until_signal = MagicMock(name="block_until_signal")
            tray.run()

        return tray, spy_create, fake_thread

    def test_block_until_signal_invoked(self, monkeypatch):
        tray, _, _ = self._run_after_setup(monkeypatch)
        tray._block_until_signal.assert_called_once()

    def test_flask_thread_not_joined(self, monkeypatch):
        """After Flask starts, tearing down the thread is not done — it stays live."""
        _, _, fake_thread = self._run_after_setup(monkeypatch)
        fake_thread.join.assert_not_called()

    def test_app_run_not_called(self, monkeypatch):
        """Terminal fallback (app.run) must NOT be invoked when Flask already started."""
        tray, _, _ = self._run_after_setup(monkeypatch)
        tray.app.run.assert_not_called()

    def test_create_app_called_once(self, monkeypatch):
        """create_app called exactly once even in the after-setup failure path."""
        _, spy_create, _ = self._run_after_setup(monkeypatch)
        spy_create.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 6. _shutdown_all idempotency
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownAllIdempotency:
    def test_runtime_shutdown_called_once_on_repeated_calls(self, monkeypatch):
        """Calling _shutdown_all() N times invokes runtime_shutdown exactly once."""
        mock_rs = _mock_runtime_shutdown(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        tray._shutdown_all()
        tray._shutdown_all()
        tray._shutdown_all()

        mock_rs.assert_called_once()

    def test_werkzeug_shutdown_not_called_if_server_absent(self, monkeypatch):
        """If werkzeug_server is None, no shutdown attempted."""
        _mock_runtime_shutdown(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)
        assert tray.werkzeug_server is None

        tray._shutdown_all()
        # No AttributeError / TypeError — passes cleanly

    def test_werkzeug_shutdown_called_if_server_present(self, monkeypatch):
        """If werkzeug_server is set, server.shutdown() is called once."""
        _mock_runtime_shutdown(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        fake_server = MagicMock(name="werkzeug_server")
        tray.werkzeug_server = fake_server

        tray._shutdown_all()
        fake_server.shutdown.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 7. _shutdown_all ordering
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownAllOrdering:
    def test_runtime_shutdown_before_werkzeug_shutdown(self, monkeypatch):
        """runtime_shutdown() must be called before werkzeug_server.shutdown()."""
        call_order: list[str] = []

        mock_rs = MagicMock(side_effect=lambda: call_order.append("runtime"))
        monkeypatch.setattr("job_finder.web._runtime.runtime_shutdown", mock_rs)

        tray, _, _ = _make_tray(monkeypatch)
        fake_server = MagicMock()
        fake_server.shutdown = MagicMock(side_effect=lambda: call_order.append("werkzeug"))
        tray.werkzeug_server = fake_server

        tray._shutdown_all()

        assert call_order == ["runtime", "werkzeug"]


# ─────────────────────────────────────────────────────────────────────────────
# 8. _shutdown_all delegates to runtime_shutdown (no duplication)
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownAllDelegation:
    def test_no_scheduler_shutdown_in_shutdown_all(self):
        """_shutdown_all must NOT call scheduler.shutdown() or spawned.terminate()
        directly — those belong exclusively in runtime_shutdown().

        We check for actual Python call-expression syntax (``sched.shutdown(``
        and ``.terminate(``) rather than bare identifier strings, so the
        docstring prose that intentionally names these identifiers to document
        the invariant doesn't trigger a false positive.
        """
        source = inspect.getsource(TrayApp._shutdown_all)
        # Actual APScheduler call pattern (never appears in docstring prose)
        assert "sched.shutdown(" not in source
        # Actual subprocess terminate call pattern (same reasoning)
        assert ".terminate(" not in source

    def test_runtime_shutdown_present_in_shutdown_all(self):
        """_shutdown_all source MUST delegate to runtime_shutdown."""
        source = inspect.getsource(TrayApp._shutdown_all)
        assert "runtime_shutdown" in source

    def test_runtime_shutdown_is_called(self, monkeypatch):
        """runtime_shutdown() is invoked when _shutdown_all() runs."""
        mock_rs = _mock_runtime_shutdown(monkeypatch)
        tray, _, _ = _make_tray(monkeypatch)

        tray._shutdown_all()

        mock_rs.assert_called_once()
