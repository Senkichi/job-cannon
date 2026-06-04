"""Console-script entry point: ``python -m job_finder`` or ``job-cannon``.

Exposes :func:`main` for the ``[project.scripts]`` entry registered in
``pyproject.toml``. Equivalent to the legacy ``python run.py`` invocation
but resolves ``config.yaml`` via :func:`job_finder.config.resolve_config_path`
so the binary can be launched from any working directory once installed.

Also (UAT 2026-05-21 F2): prints a "Job Cannon is starting on <url>" banner
and opens the user's default browser ~1.5 s after launch, so a neophyte
running ``uv run job-cannon`` is not stranded at the Werkzeug log line.
Disable with ``JOB_CANNON_NO_BROWSER=1`` for headless / CI use.
"""

import argparse
import logging
import os
import sys  # noqa: F401 — tests patch `job_finder.__main__.sys.argv`; argparse reads through this module reference
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


def main() -> None:
    """Resolve config, build the Flask app, and start the dev server."""
    # SHORT-CIRCUIT: parse --help / --version BEFORE any config / Flask imports.
    # argparse calls sys.exit(0) on --help and --version, so we never touch
    # load_config() if those flags are passed. This is what makes
    # `pipx install job-cannon && job-cannon --help` work without config.yaml.
    _build_parser().parse_args()

    # Lazy imports so a --help invocation doesn't pay the Flask import cost.
    from job_finder.config import (
        DEFAULT_SERVER_DEBUG,
        DEFAULT_SERVER_HOST,
        DEFAULT_SERVER_PORT,
        load_config,
    )
    from job_finder.web import create_app

    cfg = load_config(allow_missing=True)
    app = create_app(config=cfg)

    # Defense-in-depth: assign this process to a Job Object (Windows) or
    # install atexit + signal handlers (POSIX) so any directly-spawned
    # subprocesses (Ollama, Playwright) are reaped on forced-kill.
    # install_kill_on_exit() returns None by design — the Job Object handle
    # is retained in module state inside _process_lifecycle_win32.  Idempotent.
    from job_finder.web import _process_lifecycle

    _process_lifecycle.install_kill_on_exit()

    server = cfg.get("server", {})
    host = server.get("host", DEFAULT_SERVER_HOST)
    port = server.get("port", DEFAULT_SERVER_PORT)
    debug = server.get("debug", DEFAULT_SERVER_DEBUG)

    # F2: surface the URL before any Werkzeug noise and (unless opted out)
    # kick off a delayed browser open. The print() lands in stdout before
    # logging is fully attached — this is the one user-facing print in the
    # whole project, justified because the alternative is a stranded user.
    url = f"http://{host}:{port}"
    no_browser = bool(os.environ.get("JOB_CANNON_NO_BROWSER"))

    print(f"Job Cannon is starting on {url}")
    if not no_browser:
        print("Opening your browser…  (Ctrl+C to stop)")
        # webbrowser.open is documented as thread-safe; firing from a Timer
        # avoids racing app.run() and keeps the open non-blocking. We use
        # use_reloader=False below, so this Timer fires exactly once.
        threading.Timer(_BROWSER_OPEN_DELAY_SEC, _open_browser, args=(url,)).start()

    app.run(
        host=host,
        port=port,
        debug=debug,
        use_reloader=False,
        # threaded=True is required for the SSE live-update stream (/events):
        # each open EventSource holds one worker thread for the life of the
        # connection, and the single-threaded default would let one stream
        # block every other request. Safe at single-user/local scale.
        threaded=True,
    )


if __name__ == "__main__":
    main()
