"""System-tray application wrapper for Job Cannon (Issue #40 — Commit D).

Launches Flask in a daemon background thread, places a tray icon on the main
thread, and provides a minimal menu: Open / URL label / Pause scheduler /
Open logs / Quit.

Architectural invariants (§11.2, §11.4)
----------------------------------------
* ``create_app()`` is called **exactly once** per process — inside
  ``TrayApp.__init__``, before any tray code runs.  The terminal-mode
  fallback reuses ``self.app``.  A second ``create_app()`` call would
  race the scheduler singleton already initialised by ``init_scheduler()``.

* ``_shutdown_all()`` delegates scheduler + Ollama teardown to
  :func:`job_finder.web._runtime.runtime_shutdown`.  Werkzeug shutdown is
  tray-mode-specific (tray mode owns the ``make_server`` instance;
  terminal mode lets Werkzeug exit via ``KeyboardInterrupt``).

* Asymmetric fallback (§11.4):
  - *Before* Flask starts: fall back to terminal mode, reusing
    ``self.app`` (safe — no running server to disrupt).
  - *After* Flask starts: stay headless rather than tear down the live
    scheduler and in-flight connections.  The app is functional; only
    the tray icon is missing.

* ``ThreadedWSGIServer.daemon_threads = True`` (Werkzeug §16.6): request
  handler threads are daemonised, so ``server.shutdown()`` returns
  promptly even with open HTMX long-poll / SSE connections.
"""

import logging
import os
import subprocess
import sys
import threading
import webbrowser

import pystray

from job_finder.config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT
from job_finder.web import create_app
from job_finder.web.scheduler import get_scheduler
from job_finder.web.user_data_dirs import user_data_root

logger = logging.getLogger(__name__)


class TrayApp:
    """Owner of the system-tray icon and the Werkzeug server it wraps.

    Lifecycle
    ---------
    1. ``__init__``: load config, resolve bind/client host split, call
       ``create_app()`` once.
    2. ``run()``: try to construct the pystray ``Icon``; fall back to
       terminal mode if construction fails.  If construction succeeds,
       start Flask via the ``setup`` callback and enter the pystray event
       loop.  If the event loop itself fails, stay headless rather than
       tearing down a live server.
    3. ``_shutdown_all()``: idempotent; delegates to
       ``runtime_shutdown()`` then shuts Werkzeug if it was ever started.
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

        # Bind/client host split: wildcard-bind for all-interface servers,
        # loopback for the browser URL the user clicks (mirrors __main__'s
        # approach defensively so TrayApp works even when called standalone).
        server = cfg.get("server", {})
        self.bind_host: str = server.get("host", DEFAULT_SERVER_HOST)
        self.port: int = server.get("port", DEFAULT_SERVER_PORT)
        if self.bind_host in ("0.0.0.0", "::", ""):  # noqa: S104
            self.client_host = "127.0.0.1"
        else:
            self.client_host = self.bind_host
        self.url = f"http://{self.client_host}:{self.port}"

        # create_app() called exactly once for the process.  The scheduler
        # singleton initialised by init_scheduler() inside create_app() is
        # reused by every downstream path, including the terminal-mode fallback.
        self.app = create_app(config=cfg)
        self.flask_thread: threading.Thread | None = None
        self.werkzeug_server = None  # set by _run_flask, type: BaseWSGIServer
        self.icon: pystray.Icon | None = None
        self._shutdown_done = False

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self) -> pystray.Menu:
        """Build and return the tray icon menu."""
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

    def _status_label(self, item) -> str:
        return f"Listening on {self.url}"

    # ── Scheduler helpers ─────────────────────────────────────────────────────

    def _scheduler_paused(self) -> bool:
        """True when the scheduler is None or in PAUSED state (APScheduler state=2)."""
        sched = get_scheduler()
        if sched is None:
            return True
        return sched.state == 2  # APScheduler PAUSED

    def _toggle_scheduler(self, icon, item) -> None:
        sched = get_scheduler()
        if sched is None:
            return
        if self._scheduler_paused():
            sched.resume()
        else:
            sched.pause()

    # ── Menu callbacks ────────────────────────────────────────────────────────

    def _open_browser(self, icon=None, item=None) -> None:
        try:
            webbrowser.open(self.url, new=2)
        except Exception as exc:
            logger.warning("webbrowser.open failed: %s", exc)

    def _open_logs(self, icon=None, item=None) -> None:
        """Open the logs directory in the OS file explorer."""
        path = user_data_root() / "logs"
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 — controlled path
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as exc:
            logger.warning("Open logs folder failed: %s", exc)

    def _quit(self, icon, item) -> None:
        self._shutdown_all()
        icon.stop()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _shutdown_all(self) -> None:
        """Idempotent process-level teardown.

        Delegates scheduler + Ollama to :func:`runtime_shutdown` (Issue #1's
        shared helper).  Adds Werkzeug shutdown, which is tray-mode-specific:
        tray mode owns the ``make_server`` instance directly; terminal mode
        lets Werkzeug exit naturally via ``KeyboardInterrupt``.

        Callers must NOT duplicate ``scheduler.shutdown`` /
        ``spawned.terminate`` logic here — ``runtime_shutdown`` is the only
        path.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        from job_finder.web._runtime import runtime_shutdown

        runtime_shutdown()  # handles scheduler + Ollama (idempotent)

        if self.werkzeug_server is not None:
            try:
                self.werkzeug_server.shutdown()
            except Exception as exc:
                logger.warning("Werkzeug shutdown raised: %s", exc)

    # ── Flask thread ──────────────────────────────────────────────────────────

    def _run_flask(self) -> None:
        """Run the Werkzeug server.  ``self.app`` is already constructed."""
        from werkzeug.serving import make_server

        assert self.app is not None, "TrayApp.app must be set in __init__"
        self.werkzeug_server = make_server(self.bind_host, self.port, self.app, threaded=True)
        self.werkzeug_server.serve_forever()

    # ── Asset ─────────────────────────────────────────────────────────────────

    def _load_icon(self):
        """Load the tray icon from the bundled asset file.

        Returns a :class:`PIL.Image.Image` object.  PIL is imported lazily so
        that modules importing ``job_finder.tray`` for type-checking purposes
        do not pay the PIL boot cost.
        """
        from importlib import resources

        from PIL import Image

        with resources.files("job_finder.assets").joinpath("tray_icon.png").open("rb") as fh:
            return Image.open(fh).copy()

    # ── Block until signal (headless fallback) ────────────────────────────────

    def _block_until_signal(self) -> None:
        """Block the main thread until SIGINT or SIGTERM, then return.

        Used in the headless-fallback path (tray event loop died after Flask
        started).  Returning lets the outer ``finally`` fire ``_shutdown_all``.
        """
        import signal

        stop = threading.Event()

        def _handler(sig, frame):
            stop.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        stop.wait()

    # ── Terminal-mode fallback ────────────────────────────────────────────────

    def _run_terminal_mode_with_existing_app(self) -> None:
        """Terminal-mode fallback that REUSES ``self.app``.

        Does NOT call ``create_app()`` again — that would race the scheduler
        singleton already initialised by ``init_scheduler()``.

        Bypasses TrayApp lifecycle ownership; Werkzeug's ``app.run()``
        handles ``Ctrl+C`` naturally.  ``runtime_shutdown`` still fires via
        ``_shutdown_all``'s delegation.
        """
        debug = self.cfg.get("debug", False)
        try:
            self.app.run(
                host=self.bind_host,
                port=self.port,
                debug=debug,
                use_reloader=False,
            )
        finally:
            self._shutdown_all()

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tray icon and the Flask server.

        Phase 1 — Icon construction
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Tries to build the pystray ``Icon``.  If construction fails (missing
        display, platform with no tray support), falls back immediately to
        terminal mode with the already-constructed ``self.app``.

        Phase 2 — pystray event loop
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Flask starts inside the ``setup`` callback so the event loop is
        running before the server binds.  Two sub-cases if the loop raises:

        * ``setup_fired = False`` (loop died before setup): fall back to
          terminal mode — Flask has not started yet; the existing app is safe
          to hand to ``app.run()``.
        * ``setup_fired = True`` (loop died after Flask started): stay headless
          via :meth:`_block_until_signal`.  Tearing down a live server to
          restart in terminal mode would interrupt in-flight connections and
          orphan the scheduler.

        In all paths, the outer ``finally`` guarantees ``_shutdown_all``
        fires exactly once (idempotency guard inside handles the rest).
        """
        # ── Phase 1: Icon construction ────────────────────────────────────────
        try:
            self.icon = pystray.Icon(
                "job-cannon",
                self._load_icon(),
                "Job Cannon",
                self._build_menu(),
            )
        except Exception as exc:
            logger.warning(
                "Tray icon construction failed (%s); falling back to terminal mode "
                "with existing app instance",
                exc,
            )
            return self._run_terminal_mode_with_existing_app()

        # ── Phase 2: pystray event loop ───────────────────────────────────────
        setup_fired = False

        def _on_setup(icon) -> None:
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
