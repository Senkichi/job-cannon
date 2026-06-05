"""System-tray entry point for Job Cannon (Issue #40, Commit D).

``TrayApp`` owns the tray icon on the main thread while Flask runs in a
background daemon thread.  Design invariants:

* **``create_app()`` called exactly once** — in ``__init__``, before any tray
  code runs.  The terminal-mode fallback reuses ``self.app``; calling
  ``create_app()`` again would race the scheduler singleton initialised by
  ``init_scheduler()``.  See §11.2 and §11.4 of the Process Lifecycle Plan.

* **Asymmetric fallback** — if ``pystray.Icon`` construction fails *before*
  Flask starts, fall back to terminal mode reusing ``self.app``.  If
  ``icon.run()`` fails *after* Flask started, stay headless: tearing down a
  live scheduler to restart in terminal mode would interrupt in-flight scoring
  jobs and drop HTTP connections from users who already opened the URL.

* **``_shutdown_all()`` is idempotent** and delegates scheduler + Ollama
  teardown to ``runtime_shutdown()`` from Issue #1.  Werkzeug shutdown is
  tray-mode-specific (tray mode owns the ``make_server`` instance directly;
  terminal mode lets Werkzeug exit via ``KeyboardInterrupt``).
"""

from __future__ import annotations

import logging
import threading
import webbrowser

logger = logging.getLogger(__name__)


class TrayApp:
    """Manages the tray icon, Flask server thread, and clean shutdown."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

        # Bind/client host split (wildcard-bind handling, mirrors Issue #1's
        # split in __main__.py — duplicated defensively here so TrayApp works
        # even if __main__.py path skips the split).
        from job_finder.config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT

        server = cfg.get("server", {})
        self.bind_host: str = server.get("host", DEFAULT_SERVER_HOST)
        self.port: int = server.get("port", DEFAULT_SERVER_PORT)
        if self.bind_host in ("0.0.0.0", "::", ""):  # noqa: S104 — string comparison, not a bind call
            self.client_host = "127.0.0.1"
        else:
            self.client_host = self.bind_host
        self.url = f"http://{self.client_host}:{self.port}"

        # create_app() called exactly once for the process. The scheduler
        # singleton initialised by init_scheduler() is reused by every
        # downstream path, including the terminal-mode fallback.
        from job_finder.web import create_app

        self.app = create_app(config=cfg)
        self.flask_thread: threading.Thread | None = None
        self.werkzeug_server = None
        self.icon = None
        self._shutdown_done: bool = False

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("Open Job Cannon", self._open_browser, default=True),
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Pause scheduler",
                self._toggle_scheduler,
                checked=lambda i: self._scheduler_paused(),
            ),
            pystray.MenuItem("Open logs folder", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown_all(self) -> None:
        """Idempotent.  Delegates scheduler + Ollama to runtime_shutdown
        (Issue #1's shared helper).  Adds Werkzeug shutdown, which is
        tray-mode-specific."""
        if self._shutdown_done:
            return
        self._shutdown_done = True

        from job_finder.web._runtime import runtime_shutdown

        runtime_shutdown()  # handles scheduler + owned Popens (idempotent)

        if self.werkzeug_server is not None:
            try:
                self.werkzeug_server.shutdown()
            except Exception as exc:
                logger.warning("Werkzeug shutdown raised: %s", exc)

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _quit(self, icon, item) -> None:
        self._shutdown_all()
        icon.stop()

    def _open_browser(self, icon, item) -> None:
        try:
            webbrowser.open(self.url, new=2)
        except Exception as exc:
            logger.warning("webbrowser.open failed: %s", exc)

    def _open_logs(self, icon, item) -> None:
        import os
        import subprocess
        import sys

        from job_finder.web.user_data_dirs import user_data_root

        path = user_data_root() / "logs"
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as exc:
            logger.warning("Open logs folder failed: %s", exc)

    def _scheduler_paused(self) -> bool:
        from job_finder.web.scheduler import get_scheduler

        sched = get_scheduler()
        if sched is None:
            return True  # nothing running
        return sched.state == 2  # APScheduler PAUSED

    def _toggle_scheduler(self, icon, item) -> None:
        from job_finder.web.scheduler import get_scheduler

        sched = get_scheduler()
        if sched is None:
            return
        if self._scheduler_paused():
            sched.resume()
        else:
            sched.pause()

    def _status_label(self, item) -> str:
        return f"Listening on {self.url}"

    # ------------------------------------------------------------------
    # Flask server thread
    # ------------------------------------------------------------------

    def _run_flask(self) -> None:
        """Run Werkzeug server.  ``self.app`` already constructed in ``__init__``."""
        from werkzeug.serving import make_server

        assert self.app is not None, "TrayApp.app must be set in __init__"
        self.werkzeug_server = make_server(self.bind_host, self.port, self.app, threaded=True)
        self.werkzeug_server.serve_forever()

    # ------------------------------------------------------------------
    # Icon asset
    # ------------------------------------------------------------------

    def _load_icon(self):
        from importlib import resources

        from PIL import Image

        with resources.files("job_finder.assets").joinpath("tray_icon.png").open("rb") as fh:
            return Image.open(fh).copy()

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the tray icon + Flask server.  Blocks until the user quits.

        Fallback (asymmetric):
        * Phase 1 failure (``Icon`` construction) → terminal mode with
          existing ``self.app`` (safe: Flask not started yet).
        * Phase 2 failure before setup callback → same terminal mode fallback.
        * Phase 2 failure after setup callback → headless block (safe: Flask
          is running; tearing it down would interrupt in-flight requests).
        """
        # Phase 1: try to construct the tray icon.
        try:
            import pystray

            self.icon = pystray.Icon(
                "job-cannon", self._load_icon(), "Job Cannon", self._build_menu()
            )
        except Exception as exc:
            logger.warning(
                "Tray icon construction failed (%s); falling back to terminal mode "
                "with existing app instance",
                exc,
            )
            return self._run_terminal_mode_with_existing_app()

        # Phase 2: start Flask via pystray's setup callback.
        setup_fired = False

        def _on_setup(icon):
            nonlocal setup_fired
            icon.visible = True
            self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
            self.flask_thread.start()
            setup_fired = True

        try:
            self.icon.run(setup=_on_setup)
        except Exception as exc:
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
            self._shutdown_all()

    def _run_terminal_mode_with_existing_app(self) -> None:
        """Terminal-mode fallback REUSES ``self.app`` — does NOT call
        ``create_app()`` again.  Bypasses TrayApp lifecycle ownership;
        Werkzeug's own serve loop handles Ctrl+C.  ``runtime_shutdown`` still
        fires via ``_shutdown_all``'s delegation."""
        debug = self.cfg.get("server", {}).get("debug", False)
        try:
            self.app.run(
                host=self.bind_host,
                port=self.port,
                debug=debug,
                use_reloader=False,
            )
        finally:
            self._shutdown_all()

    def _block_until_signal(self) -> None:
        """Headless-mode block until SIGINT/SIGTERM, then return so ``finally``
        fires ``_shutdown_all()``."""
        import signal

        stop = threading.Event()

        def _handler(sig, frame):
            stop.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        stop.wait()
