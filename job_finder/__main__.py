"""Console-script entry point: ``python -m job_finder`` or ``job-cannon``.

Exposes :func:`main` for the ``[project.scripts]`` entry registered in
``pyproject.toml``. Equivalent to the legacy ``python run.py`` invocation
but resolves ``config.yaml`` via :func:`job_finder.config.resolve_config_path`
so the binary can be launched from any working directory once installed.

UAT 2026-05-21 F2 / Issue #290: prints a "Job Cannon is starting on <url>"
banner and opens the user's default browser ~1.5 s after launch in **both**
terminal mode and tray mode, so a neophyte running ``job-cannon`` is not
stranded regardless of the launch path.  Disable with
``JOB_CANNON_NO_BROWSER=1`` for headless / CI use.

In tray mode the banner also says "look for the tray icon" so the user
understands why no terminal keeps running.  On first run (no config.yaml
yet), opening the browser is what lands the user in the onboarding wizard —
the ``gate_onboarding`` redirect handles routing once the browser opens.
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
from datetime import UTC, datetime

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


def _reconfigure_stdio_utf8() -> None:
    """Force ``sys.stdout`` / ``sys.stderr`` to utf-8 (issue #234).

    Single point of enforcement for the invariant "console log output is encoded
    losslessly regardless of OS console codepage." On Windows, the default
    console codec is cp1252, which crashes on the non-Latin-1 characters (`→`,
    `—`, `·`, `…`, accented company/title text) that pepper our INFO logs.
    Two stream handlers write to these streams without overriding the encoding:
    Werkzeug's auto-attached ``_ColorStreamHandler`` and the stdlib
    ``logging.lastResort`` fallback. Reconfiguring the underlying streams once
    at process start fixes both at the source, no per-handler patching to
    maintain.

    ``errors="backslashreplace"`` so the destination console can never drop a
    line — un-renderable glyphs degrade to ``\\uXXXX`` escapes instead of a
    traceback. The narrow exception set covers streams that don't support
    reconfigure (pipes / redirected files / wrapped non-text streams).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


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
    parser.add_argument(
        "--print-example-config",
        action="store_true",
        help="Print the bundled example config to stdout and exit. "
        "Redirect to a file to bootstrap: job-cannon --print-example-config > config.yaml",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Port to listen on. Overrides config.yaml > server.port and "
        "the JOB_CANNON_PORT env var (precedence: --port > JOB_CANNON_PORT > config > default 5000).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Launch with ~30 sample scored jobs in a throwaway database. "
        "No config.yaml, no API keys, no background jobs. Your real data is untouched. "
        "Runs alongside a real instance (picks the next free port automatically).",
    )

    # Subcommands. The parser stays backward-compatible: with NO subcommand
    # (``args.command is None``) the bare ``job-cannon`` invocation launches the
    # server exactly as before. Only an explicit subcommand diverges.
    subparsers = parser.add_subparsers(dest="command")
    healthcheck = subparsers.add_parser(
        "healthcheck",
        help="Print a machine-readable health verdict (JSON) and exit 0/1/2 "
        "(ok/degraded/down). For OS-scheduler liveness probes.",
        description="Out-of-process health verdict. Reads the on-disk liveness "
        "marker and the database directly — it does NOT start the app, a "
        "scheduler, or acquire the pidfile lock.",
    )
    healthcheck.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit the verdict as JSON on stdout (default, and the only format).",
    )
    healthcheck.add_argument(
        "--heartbeat-max-age-hours",
        type=float,
        default=26.0,
        metavar="HOURS",
        help="Age beyond which the daily health heartbeat is treated as stale "
        "(degrades the verdict). Default: 26 (matches the in-process check).",
    )
    healthcheck.add_argument(
        "--user-data-dir",
        type=str,
        default=None,
        metavar="PATH",
        help="Override the user-data directory (else JOB_CANNON_USER_DATA_DIR "
        "or the platformdirs default).",
    )

    # ``serve`` — like the bare invocation, but first reclaims :5000 from a
    # crashed Job Cannon orphan (the Werkzeug reloader parent AND worker that
    # APScheduler/Ollama keep alive past a hard kill). Used by the OS-native
    # supervisor so a self-restart can always rebind the port. Uses distinct
    # dests so the subparser defaults never clobber the top-level flags when no
    # subcommand is given (the classic argparse same-dest gotcha).
    serve = subparsers.add_parser(
        "serve",
        help="Launch the app, first reclaiming the port from a crashed instance "
        "(for use under the OS supervisor). Bare `job-cannon` does NOT kill.",
        description="Free :5000 from a confirmed Job Cannon orphan (reloader "
        "parent + worker), then launch exactly as the bare invocation does. A "
        "foreign listener is never killed — serve exits non-zero instead.",
    )
    serve.add_argument(
        "--terminal",
        action="store_true",
        dest="serve_terminal",
        help="Force terminal mode (no system tray) for this serve launch.",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        dest="serve_port",
        metavar="PORT",
        help="Port to listen on (same precedence as the top-level --port).",
    )

    # ``supervisor-install`` — generate + register the per-OS keepalive manifest
    # (Scheduled Task / launchd LaunchAgent / systemd --user). ``--uninstall``
    # reverses it. Out-of-process: needs no config, Flask, or pidfile lock.
    supervisor = subparsers.add_parser(
        "supervisor-install",
        help="Install an OS-native keepalive supervisor (Scheduled Task / "
        "launchd / systemd) that relaunches Job Cannon at logon and on crash.",
        description="Render and register a per-OS keepalive manifest so a "
        "crashed/killed instance self-restarts. Per-user, no admin. Idempotent.",
    )
    supervisor.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the installed supervisor manifest and deregister it "
        "(no-op success if nothing is installed).",
    )
    return parser


def _print_example_config() -> None:
    """Print the bundled config.example.yaml to stdout and exit 0.

    The file is shipped inside the wheel under job_finder/assets/ so that
    pipx users who have no repo checkout can still bootstrap a config.yaml.
    """
    import importlib.resources

    # importlib.resources.files() is the modern (3.9+) traversal API; it works
    # for both editable installs and installed wheels without __file__ tricks.
    pkg_assets = importlib.resources.files("job_finder.assets")
    example = pkg_assets.joinpath("config.example.yaml")
    print(example.read_text(encoding="utf-8"), end="")
    sys.exit(0)


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


def _print_startup_banner(url: str, *, tray_mode: bool = False) -> threading.Timer | None:
    """Print the user-facing startup banner and (unless opted out) schedule a
    delayed browser open.

    Called from **both** terminal mode and tray mode so neither path strands
    the user.  In tray mode the banner includes a hint about the system-tray
    icon.  ``JOB_CANNON_NO_BROWSER=1`` suppresses the Timer and the
    "Opening your browser…" line; the URL banner always prints so the user
    has something to copy even in headless sessions.

    Args:
        url:       Client-accessible base URL (e.g. ``http://127.0.0.1:5000``).
        tray_mode: If True, append the tray-icon hint to the first line.

    Returns:
        The scheduled :class:`threading.Timer` if one was started, or None.
    """
    if tray_mode:
        print(f"Job Cannon is starting on {url} — look for the tray icon")
    else:
        print(f"Job Cannon is starting on {url}")

    no_browser = bool(os.environ.get("JOB_CANNON_NO_BROWSER"))
    if not no_browser:
        print("Opening your browser…  (Ctrl+C to stop)")
        timer = threading.Timer(_BROWSER_OPEN_DELAY_SEC, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()
        return timer
    return None


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


def _first_free_port(host: str, start: int, span: int = 10) -> int | None:
    """Return the first port in ``[start, start + span]`` with no listener.

    Demo mode must coexist with a live real instance, so instead of exiting
    on an occupied port it shifts to the next free one. The listen-probe has
    an inherent TOCTOU race; acceptable for a local single-user launcher.
    """
    for candidate in range(start, start + span + 1):
        if not _port_is_listening(host, candidate):
            return candidate
    return None


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

    # SIGBREAK — Windows-only Ctrl+Break. Without a handler the OS default
    # terminates the process with no cleanup; with one, Ctrl+Break gets the
    # same graceful teardown as Ctrl+C (the win32 console handler below
    # returns False for CTRL_BREAK_EVENT, so CPython converts it to SIGBREAK).
    _signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        _signals.append(signal.SIGBREAK)
    for sig in _signals:
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


def _print_migration_error(exc: Exception) -> None:
    """Print a friendly, actionable migration error message to stderr.

    Follows the same pattern as the ConfigError handler in :func:`main`.
    Called for both :exc:`~job_finder.web.db_migrate.DatabaseNewerThanCodeError`
    and :exc:`~job_finder.web.migrations._gate.MigrationBlockedError`.
    """
    # Use the exception class name as a short label so the output identifies
    # the error type without requiring the user to parse a traceback.
    label = type(exc).__name__
    print(
        f"job-cannon: {label}\n\n{exc}\n",
        file=sys.stderr,
    )


def _run_terminal_mode(cfg: dict, bind_host: str, port: int, debug: bool, url: str) -> None:
    """Build the app and serve it on the main thread (terminal mode).

    This is the pre-Issue-#40 launch path, extracted verbatim so the new tray
    dispatch in :func:`main` can route to it for ``--terminal`` /
    ``JOB_CANNON_NO_TRAY=1`` and so the asymmetric tray fallback can reuse the
    same banner + browser-Timer + signal-handler wiring.
    """
    from job_finder.web import create_app
    from job_finder.web._runtime import runtime_shutdown
    from job_finder.web.db_migrate import DatabaseNewerThanCodeError
    from job_finder.web.migrations._gate import MigrationBlockedError

    try:
        app = create_app(config=cfg)
    except (DatabaseNewerThanCodeError, MigrationBlockedError) as exc:
        _print_migration_error(exc)
        sys.exit(1)

    # F2 / Issue #290: surface the URL before any Werkzeug noise and
    # (unless opted out) kick off a delayed browser open.  The shared helper
    # _print_startup_banner handles both the print and the Timer so terminal
    # and tray paths behave identically. The print() lands in stdout before
    # logging is fully attached — this is the one user-facing print in the
    # whole project, justified because the alternative is a stranded user.
    # webbrowser.open is documented as thread-safe; firing from a Timer
    # avoids racing app.run() and keeps the open non-blocking. We use
    # use_reloader=False below, so this Timer fires exactly once.
    _print_startup_banner(url, tray_mode=False)

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
    # Force stdio to utf-8 BEFORE any logging or Flask import (issue #234).
    # Werkzeug's auto-handler and logging.lastResort both write to the OS
    # console encoding (cp1252 on Windows) and crash on the non-Latin-1 chars
    # our INFO logs routinely emit (→, —, accented titles). Reconfiguring
    # the underlying streams here makes the invalid state unrepresentable
    # for any future console handler in this process.
    _reconfigure_stdio_utf8()

    # SHORT-CIRCUIT: parse --help / --version BEFORE any config / Flask imports.
    # argparse calls sys.exit(0) on --help and --version, so we never touch
    # load_config() if those flags are passed. This is what makes
    # `pipx install job-cannon && job-cannon --help` work without config.yaml.
    args = _build_parser().parse_args()

    # SHORT-CIRCUIT: `healthcheck` is an out-of-process probe — it must NOT build
    # the Flask app, start a scheduler, or acquire the pidfile lock. Route it
    # here, before any of that machinery (mirrors --print-example-config). It
    # reads the on-disk liveness marker + DB directly and exits 0/1/2.
    if getattr(args, "command", None) == "healthcheck":
        from job_finder.web.healthcheck import run_healthcheck

        sys.exit(run_healthcheck(args))

    # SHORT-CIRCUIT: `supervisor-install` only writes/registers an OS keepalive
    # manifest — no config, Flask app, scheduler, or pidfile lock. Route it here
    # (mirrors healthcheck), before any of that machinery.
    if getattr(args, "command", None) == "supervisor-install":
        from job_finder.web.supervisor import cmd_supervisor_install

        sys.exit(cmd_supervisor_install(args))

    # `serve` shares the entire launch flow below; it differs only by a pre-bind
    # port reclaim (inserted after host/port resolution). Fold its own
    # --port/--terminal (distinct dests) back onto the top-level flags so the
    # shared resolution sees them.
    if getattr(args, "command", None) == "serve":
        if getattr(args, "serve_port", None) is not None:
            args.port = args.serve_port
        if getattr(args, "serve_terminal", False):
            args.terminal = True

    # SHORT-CIRCUIT: --print-example-config also needs no config or Flask.
    if args.print_example_config:
        _print_example_config()  # prints + sys.exit(0)

    # Demo mode: route ALL user-data side effects (database, logs, pidfile
    # lock, update-check cache) into a throwaway temp dir via the existing
    # JOB_CANNON_USER_DATA_DIR override — one mechanism, set before anything
    # resolves a user-data path, so nothing of the user's is ever touched.
    # The OS cleans the temp dir up on its own schedule; each launch is fresh.
    demo_dir: str | None = None
    if args.demo:
        import tempfile

        demo_dir = tempfile.mkdtemp(prefix="job-cannon-demo-")
        os.environ["JOB_CANNON_USER_DATA_DIR"] = demo_dir

    # Lazy imports so a --help invocation doesn't pay the Flask import cost.
    from job_finder.config import (
        DEFAULT_SERVER_DEBUG,
        DEFAULT_SERVER_HOST,
        DEFAULT_SERVER_PORT,
        ConfigError,
        load_config,
    )
    from job_finder.web.user_data_dirs import user_data_root

    if args.demo:
        # Demo never reads or writes the user's config.yaml.
        from job_finder.demo_seed import build_demo_config

        cfg = build_demo_config(demo_dir)
    else:
        try:
            cfg = load_config(allow_missing=True)
        except (ConfigError, ValueError) as exc:
            # Partial or malformed config — print a friendly, actionable message
            # instead of a raw traceback.  The docstring at config.py:259-262
            # promises this: "the onboarding wizard handles ConfigError by routing
            # to the migration UI" — that path is load_config(allow_missing=True)
            # returning {}; if validation raises we land here instead.
            print(
                f"job-cannon: config error\n\n"
                f"  {exc}\n\n"
                f"To see the full expected structure, run:\n"
                f"  job-cannon --print-example-config\n",
                file=sys.stderr,
            )
            sys.exit(1)

    server = cfg.get("server", {})
    bind_host = server.get("host", DEFAULT_SERVER_HOST)

    # Port resolution — precedence: --port CLI > JOB_CANNON_PORT env > config > default
    _port_env = os.environ.get("JOB_CANNON_PORT")
    if args.port is not None:
        port = args.port
    elif _port_env is not None:
        try:
            port = int(_port_env)
        except ValueError:
            print(
                f"job-cannon: JOB_CANNON_PORT={_port_env!r} is not a valid integer port number.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        port = server.get("port", DEFAULT_SERVER_PORT)

    debug = server.get("debug", DEFAULT_SERVER_DEBUG)

    # Bind-host / client-host split (§7.3): wildcard binds must not leak
    # ``http://0.0.0.0:5000`` into any user-visible URL or HTTP probe.
    if bind_host in ("0.0.0.0", "::", ""):  # noqa: S104
        client_host = "127.0.0.1"
    else:
        client_host = bind_host
    url = f"http://{client_host}:{port}"

    # Demo mode must run alongside a live real instance: shift to the next
    # free port instead of tripping the already-running probes below (which
    # would detect the REAL instance and exit). After the shift the chosen
    # port has no listener, so Steps 1–2 fall through naturally; the pidfile
    # lock in Step 3 lives under the demo temp dir and cannot collide.
    if args.demo:
        free_port = _first_free_port(client_host, port)
        if free_port is None:
            print(
                f"job-cannon: no free port found in {port}-{port + 10} for demo mode.",
                file=sys.stderr,
            )
            sys.exit(1)
        if free_port != port:
            print(f"Demo mode: port {port} is in use — using {free_port} instead.")
            port = free_port
            url = f"http://{client_host}:{port}"

    # --- Step 0 (serve only): reclaim the port from a crashed Job Cannon orphan.
    # Bare `job-cannon` treats a live instance as "already running" (Steps 1-2
    # focus it and exit). `serve` is the supervisor entry — it must TAKE OVER, so
    # it kills a confirmed-JC listener (reloader parent + worker) here, before
    # the focus-and-exit probes can see it. A foreign listener is never killed:
    # free_jc_port returns False and we surface the existing port-occupied guidance.
    if getattr(args, "command", None) == "serve":
        from job_finder.web.supervisor import free_jc_port

        if not free_jc_port(client_host, port):
            print(
                f"job-cannon serve: port {port} is occupied by a process that is "
                f"not Job Cannon — refusing to kill it.\n"
                f"  Use a different port:  job-cannon serve --port 5001\n"
                f"  Or stop the other process.",
                file=sys.stderr,
            )
            sys.exit(1)

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
            f"Job Cannon: port {port} is occupied by `{listener_desc}`.\n"
            f"  Use a different port:  job-cannon --port 5001\n"
            f"  Or set env var:        JOB_CANNON_PORT=5001 job-cannon\n"
            f"  Or edit config.yaml:   server.port: 5001\n"
            f"  Or stop the other process.",
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
        "start_time_utc": datetime.now(UTC).isoformat(),
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

    # --- Demo seeding: migrate the throwaway DB and populate sample data
    # BEFORE the app builds, so the first page load is already a full board.
    # create_app re-runs migrations idempotently; a fresh temp DB cannot hit
    # the downgrade guard, so no migration-error handling is needed here.
    if args.demo:
        from job_finder.demo_seed import seed_demo_db
        from job_finder.web.db_migrate import run_migrations

        demo_db_path = cfg["db"]["path"]
        run_migrations(demo_db_path)
        seed_demo_db(demo_db_path)
        print(
            f"Demo mode: ~30 sample jobs in a throwaway database ({demo_dir}). "
            f"Your real data is untouched."
        )

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
            from job_finder.web.db_migrate import DatabaseNewerThanCodeError
            from job_finder.web.migrations._gate import MigrationBlockedError

            # Issue #290: print banner + schedule browser open BEFORE the icon
            # event loop blocks the main thread.  The tray banner includes a
            # hint about the tray icon so the user isn't puzzled by a silent
            # terminal.  _print_startup_banner honours JOB_CANNON_NO_BROWSER.
            _print_startup_banner(url, tray_mode=True)
            try:
                # Pass the RESOLVED host/port — TrayApp's cfg-derived defaults
                # ignore --port / JOB_CANNON_PORT (WP9 frozen-smoke finding).
                TrayApp(cfg, bind_host=bind_host, port=port).run()
            except (DatabaseNewerThanCodeError, MigrationBlockedError) as exc:
                _print_migration_error(exc)
                sys.exit(1)


if __name__ == "__main__":
    main()
