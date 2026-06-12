"""System-tray launch mode for Job Cannon (Issue #40, default for end users).

Flask runs in a daemon background thread; the ``pystray`` icon owns the main
thread and exposes lifecycle controls (Open / Pause scheduler / Open logs /
Quit). This is the answer to the broader-userbase goal — a non-developer never
sees a terminal.

Three invariants make this safe to merge alongside the existing terminal path:

1. **``create_app()`` is called exactly once per process** — in ``__init__``,
   before any tray code runs. The scheduler singleton it initialises (via
   ``init_scheduler``) is reused by every downstream path, including the
   terminal-mode fallback. Calling ``create_app()`` a second time would race
   the existing scheduler thread.

2. **``_shutdown_all()`` delegates scheduler + Ollama teardown to the shared
   ``runtime_shutdown()``** (``job_finder.web._runtime``) — the same helper the
   terminal path uses. It only *adds* Werkzeug ``server.shutdown()``, which is
   tray-specific because tray mode owns the ``make_server`` instance directly.

3. **The fallback is asymmetric.** If ``Icon`` construction fails *before* Flask
   starts, fall back to terminal mode reusing the already-built ``self.app``.
   If ``icon.run()`` fails *after* Flask started, stay headless rather than tear
   down a live server (which would interrupt in-flight scoring jobs and drop
   HTTP connections from users who already opened the URL).

Ctrl+C on Windows (the "Ctrl+C is dead in tray mode" bug): while
``icon.run()`` owns the main thread it is blocked inside pystray's native
Win32 message pump, and CPython only delivers Python-level signal handlers
(including the default SIGINT → KeyboardInterrupt) between bytecodes on the
main thread — so a tray-mode process launched from a terminal ignores Ctrl+C
entirely. The fix is ``_install_win32_ctrl_handler``: Windows invokes console
control handlers on a *fresh injected thread*, which runs fine while the pump
is wedged. The handler tears everything down and posts a stop to the pump.
(POSIX tray backends have the analogous limitation — GLib/AppKit own the main
thread — but tray mode is Windows-first; POSIX terminal users run --terminal.)
"""

from __future__ import annotations

import logging
import threading
import webbrowser

import pystray
from apscheduler.schedulers.base import STATE_PAUSED
from werkzeug.serving import BaseWSGIServer, make_server

from job_finder.config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT
from job_finder.web import create_app
from job_finder.web.scheduler import get_scheduler
from job_finder.web.user_data_dirs import user_data_root

logger = logging.getLogger(__name__)


class TrayApp:
    """Owns the tray icon, the background Flask thread, and process teardown."""

    def __init__(self, cfg: dict, *, bind_host: str | None = None, port: int | None = None):
        self.cfg = cfg
        # main() resolves host/port with full precedence (--port CLI >
        # JOB_CANNON_PORT env > config > default) and passes them in — the
        # cfg-derived values below are only a fallback for callers that skip
        # the kwargs (tests). Before WP9 this re-derived from cfg
        # unconditionally, so tray mode silently ignored --port and served
        # the config port while the banner advertised the resolved one.
        #
        # Bind/client host split (wildcard-bind handling, mirrors __main__.py's
        # split). bind_host is passed to make_server(); client_host/url are
        # what the user sees, so http://0.0.0.0:5000 never leaks into a menu.
        server = cfg.get("server", {})
        self.bind_host = (
            bind_host if bind_host is not None else server.get("host", DEFAULT_SERVER_HOST)
        )
        self.port = port if port is not None else server.get("port", DEFAULT_SERVER_PORT)
        if self.bind_host in ("0.0.0.0", "::", ""):  # noqa: S104
            self.client_host = "127.0.0.1"
        else:
            self.client_host = self.bind_host
        self.url = f"http://{self.client_host}:{self.port}"

        # create_app() called exactly once for the process. See module docstring
        # invariant 1 — the terminal-mode fallback reuses self.app.
        self.app = create_app(config=cfg)
        self.flask_thread: threading.Thread | None = None
        self.werkzeug_server: BaseWSGIServer | None = None
        self.icon: pystray.Icon | None = None
        self._shutdown_done = False

    # -- menu ---------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open Job Cannon", self._open_browser, default=True),
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Pause scheduler",
                self._toggle_scheduler,
                checked=lambda item: self._scheduler_paused(),
            ),
            pystray.MenuItem("Open logs folder", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _status_label(self, item) -> str:
        return f"Listening on {self.url}"

    # -- menu actions -------------------------------------------------------

    def _open_browser(self, icon, item) -> None:
        try:
            webbrowser.open(self.url, new=2)
        except Exception as exc:  # headless / no display / locked-down session
            logger.warning("webbrowser.open failed: %s", exc)

    def _open_logs(self, icon, item) -> None:
        """Open the logs folder in the OS file explorer."""
        import os
        import subprocess
        import sys

        path = user_data_root() / "logs"
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 — documented Windows API
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as exc:
            logger.warning("Open logs folder failed: %s", exc)

    def _scheduler_paused(self) -> bool:
        sched = get_scheduler()
        if sched is None:
            return True  # nothing running → present as paused
        return sched.state == STATE_PAUSED

    def _toggle_scheduler(self, icon, item) -> None:
        sched = get_scheduler()
        if sched is None:
            return
        if self._scheduler_paused():
            sched.resume()
        else:
            sched.pause()

    def _quit(self, icon, item) -> None:
        self._shutdown_all()
        icon.stop()

    # -- lifecycle ----------------------------------------------------------

    def _shutdown_all(self) -> None:
        """Idempotent teardown of everything tray mode owns.

        Delegates scheduler + owned-Popens teardown to ``runtime_shutdown``
        (the shared helper terminal mode also uses), then shuts down Werkzeug,
        which is tray-mode-specific. Order: ``runtime_shutdown`` →
        ``werkzeug_server.shutdown``.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        from job_finder.web._runtime import runtime_shutdown

        runtime_shutdown()  # scheduler + owned Ollama Popen (idempotent)

        if self.werkzeug_server is not None:
            try:
                self.werkzeug_server.shutdown()
            except Exception as exc:
                logger.warning("Werkzeug shutdown raised: %s", exc)

    def _install_win32_ctrl_handler(self):
        """Register a Windows console control handler for the pump phase.

        Console handlers run on a thread Windows injects into the process, so
        this is the ONLY quit path that works while the main thread is blocked
        in pystray's native message pump (where Python signal handlers — and
        therefore Ctrl+C — can never fire). Claims every event: Ctrl+C and
        Ctrl+Break tear down and stop the pump; close/logoff/shutdown do the
        same, after which Windows terminates the process.

        Returns the registered handler (kept so it can be unregistered), or
        None when pywin32 is unavailable (non-Windows, or stripped installs).
        """
        try:
            import win32api  # type: ignore[import]  # pywin32 transitive dep
        except ImportError:
            return None

        def _handler(ctrl_type):
            self._shutdown_all()
            icon = self.icon
            if icon is not None:
                try:
                    icon.stop()  # PostMessage-based in the win32 backend: thread-safe
                except Exception as exc:
                    logger.warning("icon.stop() from console handler raised: %s", exc)
            return True

        win32api.SetConsoleCtrlHandler(_handler, True)
        return _handler

    @staticmethod
    def _remove_win32_ctrl_handler(handler) -> None:
        """Unregister a handler returned by ``_install_win32_ctrl_handler``.

        Must happen before any path that relies on normal Python SIGINT
        delivery (terminal fallback, headless ``_block_until_signal``) —
        a still-registered handler would claim Ctrl+C and stop a pump that
        is no longer the thing serving requests.
        """
        if handler is None:
            return
        try:
            import win32api  # type: ignore[import]

            win32api.SetConsoleCtrlHandler(handler, False)
        except Exception as exc:
            logger.warning("Console handler unregister raised: %s", exc)

    def _run_flask(self) -> None:
        """Serve via Werkzeug. ``self.app`` was built in ``__init__`` — we do
        NOT call ``create_app()`` again (invariant 1)."""
        assert self.app is not None, "TrayApp.app must be set in __init__"
        self.werkzeug_server = make_server(self.bind_host, self.port, self.app, threaded=True)
        self.werkzeug_server.serve_forever()

    def _load_icon(self):
        from importlib import resources

        from PIL import Image

        with resources.files("job_finder.assets").joinpath("tray_icon.png").open("rb") as fh:
            return Image.open(fh).copy()

    def run(self) -> None:
        # Phase 1: construct the tray icon. Failure here means Flask hasn't
        # started yet — safe to route to terminal mode with the existing app.
        try:
            self.icon = pystray.Icon(
                "job-cannon", self._load_icon(), "Job Cannon", self._build_menu()
            )
        except Exception as exc:
            logger.warning(
                "Tray icon construction failed (%s); falling back to terminal "
                "mode with existing app instance",
                exc,
            )
            return self._run_terminal_mode_with_existing_app()

        # Phase 2: start Flask via pystray's setup callback (the documented
        # "tray is live" hook). If icon.run() raises BEFORE setup, Flask never
        # started — safe to terminal-fallback. If it raises AFTER setup, Flask
        # is up; stay headless rather than tear it down (invariant 3).
        setup_fired = False

        def _on_setup(icon):
            nonlocal setup_fired
            icon.visible = True
            self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
            self.flask_thread.start()
            setup_fired = True

        # Installed only for the pump phase: while icon.run() owns the main
        # thread, this injected-thread handler is the only working Ctrl+C path.
        ctrl_handler = self._install_win32_ctrl_handler()
        try:
            self.icon.run(setup=_on_setup)
        except Exception as exc:
            # Both fallback paths below rely on normal Python signal delivery,
            # which the pump-phase handler would otherwise pre-empt.
            self._remove_win32_ctrl_handler(ctrl_handler)
            ctrl_handler = None
            if not setup_fired:
                logger.warning(
                    "Tray icon event loop failed before Flask started (%s); "
                    "falling back to terminal mode",
                    exc,
                )
                return self._run_terminal_mode_with_existing_app()
            logger.warning(
                "Tray icon event loop failed after Flask started (%s). "
                "Continuing headless. App is reachable at %s. "
                "Press Ctrl+C to stop.",
                exc,
                self.url,
            )
            self._block_until_signal()
        finally:
            # Always clean up exactly once — Quit, fallback, headless signal, or
            # unhandled exit all converge here.
            self._remove_win32_ctrl_handler(ctrl_handler)
            self._shutdown_all()

    def _run_terminal_mode_with_existing_app(self) -> None:
        """Terminal-mode fallback that REUSES ``self.app`` (does not call
        ``create_app()`` again — invariant 1). Werkzeug's own serve loop handles
        Ctrl+C; ``runtime_shutdown`` still fires via ``_shutdown_all``."""
        debug = self.cfg.get("server", {}).get("debug", False)
        try:
            self.app.run(host=self.bind_host, port=self.port, debug=debug, use_reloader=False)
        finally:
            self._shutdown_all()

    def _block_until_signal(self) -> None:
        """Headless-mode block until SIGINT/SIGTERM, then return so the caller's
        ``finally`` fires ``_shutdown_all()``."""
        import signal

        stop = threading.Event()

        def _handler(sig, frame):
            stop.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        stop.wait()
