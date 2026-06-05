"""Console-script entry point: ``python -m job_finder`` or ``job-cannon``.

Exposes :func:`main` for the ``[project.scripts]`` entry registered in
``pyproject.toml``. Equivalent to the legacy ``python run.py`` invocation
but resolves ``config.yaml`` via :func:`job_finder.config.resolve_config_path`
so the binary can be launched from any working directory once installed.

Launch modes
------------
* **Tray mode** (default): pystray owns the main thread; Flask runs in a
  background daemon thread.  Click "Quit" in the tray menu to exit.
* **Terminal mode** (``--terminal`` or ``JOB_CANNON_NO_TRAY=1``): existing
  terminal-mode path from Issue #1.  ``Ctrl+C`` triggers ``runtime_shutdown``
  and a clean exit.  No tray icon is constructed.

Also (UAT 2026-05-21 F2): in terminal mode, prints a "Job Cannon is starting
on <url>" banner and opens the user's default browser ~1.5 s after launch, so
a neophyte running ``uv run job-cannon --terminal`` is not stranded at the
Werkzeug log line.  Disable with ``JOB_CANNON_NO_BROWSER=1`` for headless /
CI use.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import webbrowser

logger = logging.getLogger(__name__)

# Delay before opening the browser. Flask's dev server binds in well under
# this on every machine we have tested. If Flask is not up yet the browser
# sees a connection error, the user reloads, and they are still ahead of
# where they started (no URL to copy from a Werkzeug log).
_BROWSER_OPEN_DELAY_SEC = 1.5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-cannon",
        description="Personal job search command center — Flask web app on localhost:5000.",
        epilog="Configuration: see docs/SETUP.md. Without config.yaml the app launches "
        "into the onboarding wizard on first run.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"job-cannon {_get_version()}",
    )
    parser.add_argument(
        "--terminal",
        action="store_true",
        help=(
            "Force terminal mode (no system tray). Default is tray mode. "
            "Alternatively set JOB_CANNON_NO_TRAY=1."
        ),
    )
    return parser


def _get_version() -> str:
    """Resolve the installed package version via importlib.metadata.

    Falls back to "0.0.0+dev" when the package isn't installed (e.g.,
    running from a source checkout without `uv pip install -e .`).
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("job-cannon")
        except PackageNotFoundError:
            return "0.0.0+dev"
    except Exception:
        return "0.0.0+dev"


def _open_browser(url: str) -> None:
    """Open ``url`` in the user's default browser.

    Swallows exceptions: on headless / SSH / WSL-without-X / locked-down
    corporate sessions, :func:`webbrowser.open` raises, and we do not want
    that to crash the dev server. A warning lands in the log instead.
    """
    try:
        webbrowser.open(url, new=2)  # new tab if possible
    except Exception as exc:
        logger.warning("Could not open browser at %s: %s", url, exc)


def _install_terminal_shutdown(app) -> None:
    """Register signal and console-control handlers for clean terminal-mode shutdown.

    Handles:
    - POSIX: SIGINT, SIGTERM, and SIGHUP (terminal close sends SIGHUP)
    - Windows: SIGINT, SIGTERM via signal module + SetConsoleCtrlHandler
      for CTRL_CLOSE_EVENT (terminal close), which bypasses Python signals

    Both the ``try/finally`` in main() AND these handlers are needed:
    - Werkzeug catches ``KeyboardInterrupt`` internally, so SIGINT returns
      control to main() and the ``finally`` fires runtime_shutdown().
    - Terminal close (CTRL_CLOSE_EVENT on Windows) bypasses Python signals;
      SetConsoleCtrlHandler is the only path that fires cleanup there.
    """
    from job_finder.web._runtime import runtime_shutdown

    def _signal_handler(signum, frame):
        runtime_shutdown()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except (OSError, ValueError):
            # Can happen in child threads or restricted environments
            pass

    # SIGHUP — terminal close on POSIX (not available on Windows)
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _signal_handler)
        except (OSError, ValueError):
            pass

    # Windows: SetConsoleCtrlHandler covers CTRL_CLOSE_EVENT (terminal close),
    # which does NOT trigger Python signal handlers.
    try:
        import win32api  # type: ignore[import]  # pywin32 transitive dep

        def _ctrl_handler(ctrl_type):
            runtime_shutdown()
            return True  # tell Windows we handled it

        win32api.SetConsoleCtrlHandler(_ctrl_handler, True)
    except ImportError:
        pass  # pywin32 not available — fine on non-Windows and some Windows configs


def _run_terminal_mode(cfg: dict, bind_host: str, port: int, url: str) -> None:
    """Existing terminal-mode path: create app, set up shutdown handlers,
    serve via Werkzeug dev server, and clean up on exit.

    This is the full Issue #1 terminal-mode path preserved exactly.
    ``TrayApp._run_terminal_mode_with_existing_app`` is the analogous path
    used by TrayApp when falling back, but reuses a pre-constructed app.
    """
    from job_finder.config import DEFAULT_SERVER_DEBUG
    from job_finder.web import create_app
    from job_finder.web._runtime import runtime_shutdown

    app = create_app(config=cfg)
    debug = cfg.get("server", {}).get("debug", DEFAULT_SERVER_DEBUG)

    no_browser = bool(os.environ.get("JOB_CANNON_NO_BROWSER"))
    print(f"Job Cannon is starting on {url}")
    if not no_browser:
        print("Opening your browser…  (Ctrl+C to stop)")
        timer = threading.Timer(_BROWSER_OPEN_DELAY_SEC, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()

    _install_terminal_shutdown(app)

    try:
        app.run(
            host=bind_host,
            port=port,
            debug=debug,
            use_reloader=False,
            # threaded=True is required for the SSE live-update stream (/events):
            # each open EventSource holds one worker thread for the life of the
            # connection, and the single-threaded default would let one stream
            # block every other request. Safe at single-user/local scale.
            threaded=True,
        )
    finally:
        runtime_shutdown()


def main() -> None:
    """Resolve config, then dispatch to tray mode or terminal mode."""
    # SHORT-CIRCUIT: parse --help / --version BEFORE any config / Flask imports.
    # argparse calls sys.exit(0) on --help and --version, so we never touch
    # load_config() if those flags are passed. This is what makes
    # `pipx install job-cannon && job-cannon --help` work without config.yaml.
    args = _build_parser().parse_args()

    # Lazy imports so a --help invocation doesn't pay the Flask import cost.
    from job_finder.config import (
        DEFAULT_SERVER_HOST,
        DEFAULT_SERVER_PORT,
        load_config,
    )

    cfg = load_config(allow_missing=True)

    from job_finder.web import _process_lifecycle

    # install_kill_on_exit returns None by design — the Job Object handle is
    # retained in module state inside _process_lifecycle_win32.  Idempotent.
    _process_lifecycle.install_kill_on_exit()

    server = cfg.get("server", {})
    bind_host: str = server.get("host", DEFAULT_SERVER_HOST)
    port: int = server.get("port", DEFAULT_SERVER_PORT)

    # Bind-host / client-host split (§7.3): wildcard binds must not leak
    # ``http://0.0.0.0:5000`` into any user-visible URL or HTTP probe.
    if bind_host in ("0.0.0.0", "::", ""):  # noqa: S104
        client_host = "127.0.0.1"
    else:
        client_host = bind_host
    url = f"http://{client_host}:{port}"

    if args.terminal or os.environ.get("JOB_CANNON_NO_TRAY"):
        # Terminal-mode path: Issue #1 behaviour preserved exactly.
        _run_terminal_mode(cfg, bind_host, port, url)
    else:
        # Tray mode (default): TrayApp.__init__ calls create_app() exactly once.
        from job_finder.tray import TrayApp

        TrayApp(cfg).run()


if __name__ == "__main__":
    main()
