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
from datetime import datetime

import psutil
import requests

from job_finder.web._pidfile import (
    ExistingInstanceAction,
    acquire_pidfile,
)

logger = logging.getLogger(__name__)

# Delay before opening the browser. Flask's dev server binds in well under
# this on every machine we have tested. If Flask is not up yet the browser
# sees a connection error, the user reloads, and they are still ahead of
# where they started (no URL to copy from a Werkzeug log).
_BROWSER_OPEN_DELAY_SEC = 1.5

# Retry parameters for _retry_lock_or_fail().
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
    parser.add_argument(
        "--terminal",
        action="store_true",
        help="Force terminal mode (no system tray). Default is tray mode. "
        "Equivalent to setting JOB_CANNON_NO_TRAY=1.",
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


def probe_existing_jc(url: str, timeout: float = 1.0) -> dict | None:
    """Return parsed ``/__jc_health`` payload iff a Job Cannon instance is responding.

    Returns None for: connection failure, timeout, non-200 status, non-JSON body,
    or JSON missing the identity marker ``app == 'job-cannon'``.

    Args:
        url:     Base URL of the candidate instance (e.g. ``http://127.0.0.1:5000``).
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed health-check dict on success; None on any failure.
    """
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
    """Return True if a process is accepting TCP connections on ``host:port``.

    Args:
        host:    Host to probe (e.g. ``"127.0.0.1"``).
        port:    TCP port number.
        timeout: Connection timeout in seconds.

    Returns:
        True if the connection succeeded; False on any error.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def _listener_looks_like_jc(host: str, port: int) -> tuple[bool, str | None, int | None]:
    """Identify the process listening on ``host:port`` via psutil.net_connections.

    Cmdline substring check (``job-cannon`` / ``job_finder``) matches both
    ``uv run job-cannon`` and ``python -m job_finder``. Interface-matching
    avoids mis-claiming a sibling app on a different interface.

    Args:
        host: Client-facing host (e.g. ``"127.0.0.1"`` or ``"localhost"``).
        port: Port to inspect.

    Returns:
        Tuple of (looks_like_jc, cmdline_or_None, pid_or_None).
        ``looks_like_jc`` is True only when the listener's cmdline contains
        a recognisable Job Cannon marker AND the bound interface matches.
    """
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            if laddr.port != port:
                continue
            # Interface check: if the caller expects loopback (127.0.0.1 or
            # localhost), listeners on non-loopback IPs are treated as foreign.
            if host in ("127.0.0.1", "localhost") and laddr.ip not in (
                "127.0.0.1",
                "::1",
                "",
            ):
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
    """Retry ``acquire_pidfile`` up to ``_LOCK_RETRY_COUNT`` times with backoff.

    Used when the lock is held by a process that may be dead (dead-PID race,
    PID-reuse, mid-startup, etc.).

    Args:
        reason:    Short label for diagnostic messages (e.g. ``"dead_pid"``).
        lock_path: Path to the lock file.
        meta_path: Path to the metadata sidecar.
        metadata:  Metadata dict to write on successful re-acquisition.

    Returns:
        ``CONTINUE_STARTUP`` on successful re-acquisition.
        ``EXIT_FAILURE`` after exhausting retries.
    """
    for attempt in range(_LOCK_RETRY_COUNT):
        time.sleep(_LOCK_RETRY_DELAY_SEC)
        result = acquire_pidfile(lock_path, meta_path, metadata)
        if result.acquired:
            logger.info(
                "Main process: lock re-acquired after %d attempt(s) (reason=%s)",
                attempt + 1,
                reason,
            )
            return ExistingInstanceAction.CONTINUE_STARTUP

    print(
        f"Job Cannon: lock contention unresolved (reason={reason}). "
        f"Stop any running instance manually and try again. Lock: {lock_path}",
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
    """Dispatch on the lock-contention scenario and return the action to take.

    Called when ``acquire_pidfile`` returns ``acquired=False``.  Implements
    the decision tree in §8.2 of the Process Lifecycle Plan.

    Args:
        existing_meta: Parsed metadata from the lock holder's sidecar
                       (may be None if missing or corrupt).
        default_url:   URL to use if the metadata doesn't contain one.
        lock_path:     Path to the lock file (for diagnostic messages).
        meta_path:     Path to the metadata sidecar (for re-acquisition).
        metadata:      New metadata dict for this process (used on retry).

    Returns:
        ``EXIT_SUCCESS``    — existing live JC instance confirmed; browser opened.
        ``EXIT_FAILURE``    — unresolvable state; caller should sys.exit(1).
        ``CONTINUE_STARTUP``— lock re-acquired after retrying; caller should continue.
    """
    # Case 1: metadata missing / unparseable — holder may be mid-startup.
    if existing_meta is None:
        return _retry_lock_or_fail("no_metadata", lock_path, meta_path, metadata)

    pid = existing_meta.get("pid")
    url = existing_meta.get("url", default_url)

    # Case 2: corrupt PID field.
    if not isinstance(pid, int):
        print(
            "Job Cannon: server.json is corrupt and the lock is held. "
            "Stop the running instance manually and try again.",
            file=sys.stderr,
        )
        return ExistingInstanceAction.EXIT_FAILURE

    # Case 3: PID is dead (lock-held race, microseconds wide after process exit).
    if not psutil.pid_exists(pid):
        return _retry_lock_or_fail("dead_pid", lock_path, meta_path, metadata)

    # Case 4–7: PID is alive — inspect its cmdline.
    try:
        cmdline = " ".join(psutil.Process(pid).cmdline())
    except psutil.AccessDenied:
        # Case 4: different OS user holds the lock.
        print(
            f"Job Cannon: another instance is running as a different user "
            f"(PID {pid}). Cannot manage it. Stop it manually and try again.",
            file=sys.stderr,
        )
        return ExistingInstanceAction.EXIT_FAILURE
    except psutil.NoSuchProcess:
        # Case 5: process died between pid_exists and cmdline (race).
        return _retry_lock_or_fail("race_death", lock_path, meta_path, metadata)

    # Case 6: PID reuse — process is alive but isn't Job Cannon.
    if ("job-cannon" not in cmdline) and ("job_finder" not in cmdline):
        return _retry_lock_or_fail("pid_reuse", lock_path, meta_path, metadata)

    # Case 7: confirmed live Job Cannon instance.
    print(f"Job Cannon is already running at {url}")
    if not os.environ.get("JOB_CANNON_NO_BROWSER"):
        webbrowser.open(url, new=2)
    return ExistingInstanceAction.EXIT_SUCCESS


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
    #
    # CRITICAL: console handlers run in LIFO order, and returning True means
    # "handled — do not call the next handler in the chain." CPython installs
    # its own console handler at startup that converts CTRL_C_EVENT /
    # CTRL_BREAK_EVENT into SIGINT (→ _signal_handler above → sys.exit). Our
    # handler is registered later, so it runs FIRST; if it returned True for
    # Ctrl+C it would suppress CPython's handler, runtime_shutdown() would run
    # but sys.exit() never would, and the Werkzeug server would keep serving
    # forever — an orphan process holding the port. So we ONLY claim the events
    # that genuinely bypass Python signals (close / logoff / shutdown) and let
    # Ctrl+C / Ctrl+Break fall through to CPython's SIGINT path by returning False.
    try:
        import win32api  # type: ignore[import]  # pywin32 transitive dep
        import win32con  # type: ignore[import]  # pywin32 transitive dep

        # Events delivered to the process before the OS terminates it, which do
        # NOT raise a Python signal — cleanup must happen here or not at all.
        _CONSOLE_TERMINATION_EVENTS = frozenset(
            {
                win32con.CTRL_CLOSE_EVENT,
                win32con.CTRL_LOGOFF_EVENT,
                win32con.CTRL_SHUTDOWN_EVENT,
            }
        )

        def _ctrl_handler(ctrl_type):
            if ctrl_type in _CONSOLE_TERMINATION_EVENTS:
                runtime_shutdown()
                return True  # we handled the terminal/session teardown
            # CTRL_C_EVENT / CTRL_BREAK_EVENT: defer to CPython's handler so the
            # normal SIGINT path (signal handler → sys.exit) runs and the
            # process actually exits.
            return False

        win32api.SetConsoleCtrlHandler(_ctrl_handler, True)
    except ImportError:
        pass  # pywin32 not available — fine on non-Windows and some Windows configs


def _run_terminal_mode(cfg: dict, bind_host: str, port: int, debug: bool, url: str) -> None:
    """Build the app and serve it on the main thread (terminal mode).

    This is the pre-Issue-#40 launch path, extracted verbatim so the new tray
    dispatch in :func:`main` can route to it for ``--terminal`` /
    ``JOB_CANNON_NO_TRAY=1`` and so the asymmetric tray fallback can reuse the
    same banner + browser-Timer + signal-handler wiring.
    """
    from job_finder.web import create_app
    from job_finder.web._runtime import runtime_shutdown

    app = create_app(config=cfg)

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
    """Resolve config, build the Flask app, and start the dev server."""
    # SHORT-CIRCUIT: parse --help / --version BEFORE any config / Flask imports.
    # argparse calls sys.exit(0) on --help and --version, so we never touch
    # load_config() if those flags are passed. This is what makes
    # `pipx install job-cannon && job-cannon --help` work without config.yaml.
    args = _build_parser().parse_args()

    # Lazy imports so a --help invocation doesn't pay the Flask import cost.
    from job_finder.config import (
        DEFAULT_SERVER_DEBUG,
        DEFAULT_SERVER_HOST,
        DEFAULT_SERVER_PORT,
        load_config,
    )
    from job_finder.web.user_data_dirs import user_data_root

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

    # --- Step 1: HTTP probe — matches post-plan instances responding at /__jc_health.
    if probe_existing_jc(url) is not None:
        print(f"Job Cannon is already running at {url}")
        if not os.environ.get("JOB_CANNON_NO_BROWSER"):
            webbrowser.open(url, new=2)
        sys.exit(0)

    # --- Step 2: port-listening + psutil cmdline — matches pre-plan instances
    # during the upgrade window (no /__jc_health endpoint yet).
    if _port_is_listening(client_host, port):
        looks_like_jc, cmdline, listener_pid = _listener_looks_like_jc(client_host, port)
        if looks_like_jc:
            print(f"Job Cannon (pre-upgrade instance, PID {listener_pid}) is running at {url}")
            if not os.environ.get("JOB_CANNON_NO_BROWSER"):
                webbrowser.open(url, new=2)
            sys.exit(0)
        listener_desc = (
            cmdline if cmdline else (f"PID {listener_pid}" if listener_pid else "unknown process")
        )
        print(
            f"Job Cannon: port {port} is occupied by `{listener_desc}`. "
            f"Configure a different port in config.yaml > server.port, or stop the other process.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Step 3: acquire the split-file advisory lock.
    logs_dir = user_data_root() / "logs"
    lock_path = logs_dir / "server.lock"
    meta_path = logs_dir / "server.json"
    metadata = {
        "pid": os.getpid(),
        "url": url,
        "start_time_utc": datetime.utcnow().isoformat() + "Z",
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

    # --- Step 4: process-level orphan guard, then dispatch to a launch mode.
    from job_finder.web import _process_lifecycle

    # install_kill_on_exit returns None by design — the Job Object handle is
    # retained in module state inside _process_lifecycle_win32.  Idempotent.
    # Installed before dispatch so BOTH tray and terminal modes get Win32
    # orphan-on-hard-kill protection for the spawned Ollama subprocess.
    _process_lifecycle.install_kill_on_exit()

    # Tray mode is the default (Issue #40) — an end user never sees a terminal.
    # --terminal / JOB_CANNON_NO_TRAY=1 force the existing terminal path; the
    # tray app also auto-falls back to terminal mode if the icon can't be built.
    if args.terminal or os.environ.get("JOB_CANNON_NO_TRAY"):
        _run_terminal_mode(cfg, bind_host, port, debug, url)
    else:
        # Importing job_finder.tray pulls in pystray, whose Linux backend
        # connects to the X display AT IMPORT TIME — on a headless box ($DISPLAY
        # unset) that raises before TrayApp can even be constructed. Treat an
        # import failure the same as an Icon-construction failure: fall back to
        # terminal mode rather than crashing. (TrayApp.run() handles the
        # post-import Icon / event-loop failures itself.)
        try:
            from job_finder.tray import TrayApp
        except Exception as exc:
            logger.warning("Tray mode unavailable (%s); falling back to terminal mode", exc)
            _run_terminal_mode(cfg, bind_host, port, debug, url)
        else:
            TrayApp(cfg).run()


if __name__ == "__main__":
    main()
