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

import logging
import os
import threading
import webbrowser

from job_finder.config import (
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    load_config,
)
from job_finder.web import create_app

logger = logging.getLogger(__name__)

# Delay before opening the browser. Flask's dev server binds in well under
# this on every machine we have tested. If Flask is not up yet the browser
# sees a connection error, the user reloads, and they are still ahead of
# where they started (no URL to copy from a Werkzeug log).
_BROWSER_OPEN_DELAY_SEC = 1.5


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
    cfg = load_config(allow_missing=True)
    app = create_app(config=cfg)
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
    )


if __name__ == "__main__":
    main()
