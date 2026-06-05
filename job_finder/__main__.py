"""Console-script entry point: ``python -m job_finder`` or ``job-cannon``.

Exposes :func:`main` for the ``[project.scripts]`` entry registered in
``pyproject.toml``. Equivalent to the legacy ``python run.py`` invocation
but resolves ``config.yaml`` via :func:`job_finder.config.resolve_config_path`
so the binary can be launched from any working directory once installed.

Also (UAT 2026-05-21 F2): prints a "Job Cannon is starting on <url>" banner
and opens the user's default browser ~1.5 s after launch, so a neophyte
running ``uv run job-cannon`` is not stranded at the Werkzeug log line.
Disable with ``JOB_CANNON_NO_BROWSER=1`` for headless / CI use.

Before starting the Flask server, the launcher runs a three-step detection
sequence (§8.2.5 of .planning/PROCESS-LIFECYCLE-PLAN.md) that prevents a
second invocation from crashing with ``OSError: address already in use``:

  Step 1 — HTTP probe at ``/__jc_health``: matches any running instance that
    exposes the endpoint (post-plan instances).
  Step 2 — ``psutil.net_connections`` cmdline check: matches pre-plan
    instances that bind the port but don't yet expose ``/__jc_health``.
  Step 3 — ``portalocker`` advisory lock: detects same-PID-reuse races and
    ensures at most one scheduler runs.

A foreign port owner (e.g. ``python -m http.server``) at step 2 emits a
clear diagnostic and exits 1 instead of crashing on EADDRINUSE.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_finder.web._pidfile import ExistingInstanceAction

logger = logging.getLogger(__name__)

# Delay before opening the browser. Flask's dev server binds in well under
# this on every machine we have tested. If Flask is not up yet the browser
# sees a connection error, the user reloads, and they are still ahead of
# where they started (no URL to copy from a Werkzeug log).
_BROWSER_OPEN_DELAY_SEC = 1.5

# Retry parameters for _retry_lock_or_fail()
_LOCK_RETRY_COUNT = 3
_LOCK_RETRY_DELAY_SEC = 0.2


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


# ---------------------------------------------------------------------------
# Already-running detection helpers (§8.2.5)
# ---------------------------------------------------------------------------


def probe_existing_jc(url: str, timeout: float = 1.0) -> dict | None:
    """Return parsed ``/__jc_health`` payload iff a Job Cannon instance responds.

    Returns ``None`` for: connection failure, timeout, non-200 status,
    non-JSON body, or JSON missing the identity marker ``app == 'job-cannon'``.

    The identity marker check is load-bearing — it prevents a foreign process
    that happens to respond on the same port from being mis-identified as Job
    Cannon (false handoff).

    Args:
        url: Base URL of the candidate instance (e.g. ``http://127.0.0.1:5000``).
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed JSON dict on success, ``None`` on any failure.
    """
    import requests  # lazy import — not needed for --help / --version

    try:
        r = requests.get(f"{url.rstrip('/')}/__jc_health", timeout=timeout)
    except (requests.ConnectionError, requests.Timeout, OSError):
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("app") != "job-cannon":
        return None
    return data


def _port_is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to *host*:*port* succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def _listener_looks_like_jc(host: str, port: int) -> tuple[bool, str | None, int | None]:
    """Identify the process listening on *host*:*port* via ``psutil.net_connections``.

    Cmdline substring check (``job-cannon`` / ``job_finder``) matches both
    ``uv run job-cannon`` and ``python -m job_finder``.

    Interface matching: when *host* is ``127.0.0.1`` or ``localhost``, only
    connections bound to a loopback address (``127.0.0.1`` / ``::1``) qualify.
    This avoids mis-claiming a sibling app bound to a non-loopback interface.

    Returns:
        ``(looks_like_jc, cmdline_str_or_None, pid_or_None)``

        - ``looks_like_jc`` True iff the listener's cmdline contains
          ``job-cannon`` or ``job_finder``.
        - ``cmdline_str_or_None`` is the raw joined cmdline (for diagnostics),
          or ``None`` when the process is inaccessible.
        - ``pid_or_None`` is the listener PID, or ``None`` when unavailable.

    Cross-user limitation: ``psutil.net_connections`` without elevation on
    Linux/macOS only returns connections owned by the current user.  A
    foreign-user listener appears as ``conn.pid = None`` here and is treated
    as foreign (returns ``(False, None, None)``).
    """
    import psutil  # lazy import

    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            if laddr.port != port:
                continue
            # Interface filter: localhost probe must only match loopback binds.
            if host in ("127.0.0.1", "localhost") and laddr.ip not in ("127.0.0.1", "::1"):
                continue
            pid = conn.pid
            if pid is None:
                continue
            try:
                cmdline = " ".join(psutil.Process(pid).cmdline())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                return False, None, pid
            looks_like_jc = ("job-cannon" in cmdline) or ("job_finder" in cmdline)
            return looks_like_jc, cmdline, pid
    except psutil.AccessDenied:
        return False, None, None
    return False, None, None


def _retry_lock_or_fail(
    reason: str,
    lock_path,
    meta_path,
    metadata: dict,
) -> ExistingInstanceAction:
    """Retry ``acquire_pidfile`` up to ``_LOCK_RETRY_COUNT`` times.

    On success returns ``CONTINUE_STARTUP`` (caller proceeds to ``create_app``).
    On exhaustion prints a diagnostic and returns ``EXIT_FAILURE``.

    Args:
        reason: Short token describing why we are retrying (for the diagnostic).
        lock_path: Lock file path forwarded to ``acquire_pidfile``.
        meta_path: Metadata sidecar path forwarded to ``acquire_pidfile``.
        metadata: Metadata dict forwarded to ``acquire_pidfile``.
    """
    from job_finder.web._pidfile import ExistingInstanceAction, acquire_pidfile

    for _attempt in range(_LOCK_RETRY_COUNT):
        time.sleep(_LOCK_RETRY_DELAY_SEC)
        result = acquire_pidfile(lock_path, meta_path, metadata)
        if result.acquired:
            return ExistingInstanceAction.CONTINUE_STARTUP

    print(
        f"Job Cannon: lock contention unresolved (reason={reason}). "
        f"Stop any running instance manually and try again. "
        f"Lock: {lock_path}",
        file=sys.stderr,
    )
    return ExistingInstanceAction.EXIT_FAILURE


def handle_existing_instance(
    existing_meta: dict | None,
    default_url: str,
    lock_path,
    meta_path,
    metadata: dict,
) -> ExistingInstanceAction:
    """Decide what to do when ``acquire_pidfile`` reports contention.

    Dispatches on the metadata and psutil process state of the lock holder
    to distinguish: live same-instance, dead-lock-still-held race, PID reuse,
    different user, corrupt metadata, and holder-still-starting cases.

    Args:
        existing_meta: Parsed server.json content (may be ``None`` — holder
            mid-startup or wrote corrupt JSON).
        default_url: URL to display/open when the instance is confirmed live.
        lock_path: Forwarded to ``_retry_lock_or_fail`` retry attempts.
        meta_path: Forwarded to ``_retry_lock_or_fail`` retry attempts.
        metadata: Our own startup metadata, forwarded to retry attempts.

    Returns:
        ``ExistingInstanceAction`` directing the caller to exit 0, exit 1,
        or continue startup (dead-PID retry succeeded).
    """
    import psutil

    from job_finder.web._pidfile import ExistingInstanceAction

    # --- No metadata (holder mid-startup or corrupt write) ---
    if existing_meta is None:
        return _retry_lock_or_fail("no_metadata", lock_path, meta_path, metadata)

    pid = existing_meta.get("pid")
    url = existing_meta.get("url", default_url)

    # --- Corrupt PID field ---
    if not isinstance(pid, int):
        print(
            "Job Cannon: server.json is corrupt and lock is held. "
            "Stop the running instance manually and try again.",
            file=sys.stderr,
        )
        return ExistingInstanceAction.EXIT_FAILURE

    # --- Dead PID (lock still held — microsecond-wide race) ---
    if not psutil.pid_exists(pid):
        return _retry_lock_or_fail("dead_pid", lock_path, meta_path, metadata)

    # --- Alive PID — inspect cmdline ---
    try:
        cmdline = " ".join(psutil.Process(pid).cmdline())
    except psutil.AccessDenied:
        print(
            f"Job Cannon: another instance is running as a different user "
            f"(PID {pid}). Cannot manage.",
            file=sys.stderr,
        )
        return ExistingInstanceAction.EXIT_FAILURE
    except psutil.NoSuchProcess:
        # Race: process died between pid_exists and cmdline
        return _retry_lock_or_fail("race_death", lock_path, meta_path, metadata)

    # --- PID reuse: alive but cmdline doesn't match ---
    if ("job-cannon" not in cmdline) and ("job_finder" not in cmdline):
        return _retry_lock_or_fail("pid_reuse", lock_path, meta_path, metadata)

    # --- Confirmed live Job Cannon instance ---
    print(f"Job Cannon is already running at {url}")
    if not os.environ.get("JOB_CANNON_NO_BROWSER"):
        _open_browser(url)
    return ExistingInstanceAction.EXIT_SUCCESS


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

    cfg = load_config(allow_missing=True)
    server = cfg.get("server", {})
    bind_host = server.get("host", DEFAULT_SERVER_HOST)
    port = server.get("port", DEFAULT_SERVER_PORT)
    debug = server.get("debug", DEFAULT_SERVER_DEBUG)

    # Bind-host / client-host split (§7.3): wildcard binds must not leak
    # ``http://0.0.0.0:5000`` into any user-visible URL or HTTP probe.
    if bind_host in ("0.0.0.0", "::", ""):  # noqa: S104
        client_host = "127.0.0.1"
    else:
        client_host = bind_host
    url = f"http://{client_host}:{port}"

    # --- Already-running detection sequence (§8.2.5) ---

    # Step 1: HTTP probe — matches post-plan instances exposing /__jc_health.
    if probe_existing_jc(url) is not None:
        print(f"Job Cannon is already running at {url}")
        if not os.environ.get("JOB_CANNON_NO_BROWSER"):
            _open_browser(url)
        sys.exit(0)

    # Step 2: psutil port+cmdline check — matches pre-plan instances (no health
    # endpoint) during the upgrade window, and catches foreign port owners.
    if _port_is_listening(client_host, port):
        looks_like_jc, cmdline, listener_pid = _listener_looks_like_jc(client_host, port)
        if looks_like_jc:
            print(f"Job Cannon (pre-upgrade instance, PID {listener_pid}) is running at {url}")
            if not os.environ.get("JOB_CANNON_NO_BROWSER"):
                _open_browser(url)
            sys.exit(0)
        listener_desc = (
            cmdline if cmdline else (f"PID {listener_pid}" if listener_pid else "unknown process")
        )
        print(
            f"Job Cannon: port {port} is occupied by `{listener_desc}`. "
            f"Configure a different port in config.yaml > server.port, "
            f"or stop the other process.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 3: advisory lock — detects dead-PID/same-PID-reuse races and ensures
    # at most one scheduler instance runs.
    from datetime import UTC, datetime

    from job_finder.web._pidfile import ExistingInstanceAction, acquire_pidfile
    from job_finder.web.user_data_dirs import user_data_root

    logs_dir = user_data_root() / "logs"
    lock_path = logs_dir / "server.lock"
    meta_path = logs_dir / "server.json"
    metadata = {
        "pid": os.getpid(),
        "url": url,
        "start_time_utc": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "lock_path": str(lock_path),
    }

    result = acquire_pidfile(lock_path, meta_path, metadata)
    if not result.acquired:
        action = handle_existing_instance(result.existing, url, lock_path, meta_path, metadata)
        if action == ExistingInstanceAction.EXIT_SUCCESS:
            sys.exit(0)
        if action == ExistingInstanceAction.EXIT_FAILURE:
            sys.exit(1)
        # CONTINUE_STARTUP: dead-PID retry succeeded inside handle_existing_instance

    # --- Launch Flask app ---
    from job_finder.web import create_app

    app = create_app(config=cfg)

    from job_finder.web import _process_lifecycle

    # install_kill_on_exit returns None by design — the Job Object handle is
    # retained in module state inside _process_lifecycle_win32.  Idempotent.
    _process_lifecycle.install_kill_on_exit()

    # F2: surface the URL before any Werkzeug noise and (unless opted out)
    # kick off a delayed browser open. The print() lands in stdout before
    # logging is fully attached — this is the one user-facing print in the
    # whole project, justified because the alternative is a stranded user.
    no_browser = bool(os.environ.get("JOB_CANNON_NO_BROWSER"))

    print(f"Job Cannon is starting on {url}")
    if not no_browser:
        print("Opening your browser…  (Ctrl+C to stop)")
        # webbrowser.open is documented as thread-safe; firing from a Timer
        # avoids racing app.run() and keeps the open non-blocking. We use
        # use_reloader=False below, so this Timer fires exactly once.
        # daemon=True: if the main thread exits before the Timer fires (e.g.
        # very fast crash at startup), the Timer thread does not keep the
        # process alive.
        timer = threading.Timer(_BROWSER_OPEN_DELAY_SEC, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()

    _install_terminal_shutdown(app)

    from job_finder.web._runtime import runtime_shutdown

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


if __name__ == "__main__":
    main()
