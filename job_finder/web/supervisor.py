"""OS-native keepalive supervisor for ``job-cannon serve`` (#439, epic #433).

This is the heaviest child of the out-of-process reliability (C2) track. It adds
two capabilities behind the additive ``serve`` / ``supervisor-install`` CLI
subcommands (``job_finder/__main__.py``):

1. **Pre-bind port reclaim** (:func:`free_jc_port`) — before ``serve`` binds
   ``:5000`` it kills the documented orphan hazard: a Werkzeug-reloader
   **parent AND worker** pair that survived a hard kill and still holds the
   port (APScheduler/Ollama prevent a clean Ctrl+C, so orphans linger). We only
   ever terminate a listener we have positively identified as Job Cannon via
   :func:`job_finder.__main__._listener_looks_like_jc`; a foreign listener is
   never touched (the caller aborts with the existing "port occupied" guidance).

2. **Per-OS keepalive manifest install** (:func:`cmd_supervisor_install`) —
   generates and registers an OS-native supervisor so a crashed/killed instance
   self-restarts at logon and on failure:

   ===========  ==================================  ============================
   OS           mechanism                           restart governance
   ===========  ==================================  ============================
   Windows      Scheduled Task at logon (per-user,  ``RestartOnFailure`` Count +
                NO admin / NO ``/ru SYSTEM``)       Interval (RestartCount /
                                                    RestartInterval)
   macOS        launchd LaunchAgent (~/Library)     ``KeepAlive`` +
                                                    ``ThrottleInterval``
   Linux        systemd ``--user`` service          ``Restart=always`` +
                (~/.config/systemd/user)            ``StartLimitIntervalSec`` /
                                                    ``StartLimitBurst``
   ===========  ==================================  ============================

   Windows uses a Task Scheduler **XML** manifest registered via
   ``schtasks /create /xml`` rather than the inline ``schtasks /sc ONLOGON``
   form: only the XML schema can encode the locked ``RestartOnFailure``
   governance (the inline CLI cannot express RestartCount/RestartInterval). The
   XML ``<LogonTrigger>`` is the faithful realization of the locked "logon
   trigger" decision, and ``<RunLevel>LeastPrivilege</RunLevel>`` +
   ``InteractiveToken`` keep it per-user with no SYSTEM principal.

The renderers are pure (return a new string; no in-place mutation). Install /
uninstall are idempotent: re-install overwrites cleanly, and uninstall on a
missing manifest is a no-op success.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import psutil

from job_finder.web.user_data_dirs import logs_path, user_data_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest identity + locked restart-governance constants
# ---------------------------------------------------------------------------

# Windows Scheduled Task name (per-user store).
_TASK_NAME = "JobCannon"
# macOS launchd LaunchAgent label (reverse-DNS; also the plist filename stem).
_LAUNCHD_LABEL = "com.senkichi.jobcannon"
# Linux systemd --user unit filename.
_SYSTEMD_UNIT = "job-cannon.service"

# Restart governance (the same ceiling expressed per-OS): allow at most
# _MAX_RESTARTS automatic restarts within a _RESTART_WINDOW_SEC window, with a
# _RESTART_BACKOFF_SEC pause between attempts so a crash-loop cannot busy-spin.
_MAX_RESTARTS = 5
_RESTART_WINDOW_SEC = 300
_RESTART_BACKOFF_SEC = 10


# ---------------------------------------------------------------------------
# Pre-bind port reclaim (serve)
# ---------------------------------------------------------------------------


def free_jc_port(host: str, port: int, *, grace_sec: float = 5.0) -> bool:
    """Free ``host:port`` iff a confirmed Job Cannon instance holds it.

    Returns True when the port is safe to bind — it was already free, or the
    listener was positively identified as Job Cannon and its process tree
    (Werkzeug reloader parent **and** worker) was terminated. Returns False when
    a **foreign** process holds the port; the caller must abort and NEVER kill
    it.

    The kill targets both the reloader parent and the worker because either one
    surviving keeps ``:5000`` bound — the exact orphan hazard documented in
    CLAUDE.md's restart procedure.
    """
    # Imported lazily so this module stays importable without paying __main__'s
    # cost, and so tests can monkeypatch the identity primitives on __main__.
    from job_finder.__main__ import _listener_looks_like_jc, _port_is_listening

    if not _port_is_listening(host, port):
        # Port already free, but a prior instance's owned child (e.g. an Ollama
        # reparented by an unclean death) may still be alive — reap it by record.
        _reap_recorded_owned_orphans(host, port, grace_sec=grace_sec)
        return True  # nothing bound — port is already free

    looks_like_jc, _cmdline, pid = _listener_looks_like_jc(host, port)
    if not looks_like_jc or pid is None:
        # Foreign or unidentifiable listener: refuse to kill. The serve caller
        # surfaces the existing "port occupied" guidance and exits non-zero.
        return False

    # Best-effort graceful teardown of THIS process's runtime first (a no-op in
    # a fresh serve process; idempotent). The remote orphan's own graceful path
    # is SIGTERM via terminate() below, which its signal handler converts into
    # runtime_shutdown().
    try:
        from job_finder.web._runtime import runtime_shutdown

        runtime_shutdown()
    except Exception:  # pragma: no cover - teardown must never block reclaim
        logger.debug("runtime_shutdown() during port reclaim raised", exc_info=True)

    targets = _collect_jc_process_tree(pid)
    _terminate_procs(targets, grace_sec=grace_sec)
    # Killing the job owner reaps job-member children transitively (KILL_ON_JOB_
    # CLOSE); this additionally sweeps any owned child the metadata recorded —
    # the case where the Job Object had degraded and the child was reparented.
    _reap_recorded_owned_orphans(host, port, grace_sec=grace_sec)
    return True


def _reap_recorded_owned_orphans(host: str, port: int, *, grace_sec: float = 5.0) -> None:
    """Terminate recorded owned children (e.g. a spawned Ollama) that outlived
    their launcher, identified from the ``(host, port)`` metadata sidecar.

    The whole reason ``free_jc_port`` does NOT kill ``ollama.exe`` from the
    process tree (it cannot tell *our* spawned Ollama from a user's own) is
    resolved here: ``owned_pids`` lists only children *this app* spawned, and
    each is re-validated by ``create_time`` before termination, so a recycled
    PID is never mistaken for our child. Entries without a recorded
    ``create_time`` are skipped — we never kill without a reuse guard.
    """
    from job_finder.web._pidfile import claim_paths, read_owned_pids

    logs_dir = user_data_root() / "logs"
    _lock_path, meta_path = claim_paths(logs_dir, host, port)
    victims: list[psutil.Process] = []
    for entry in read_owned_pids(meta_path):
        recorded_ct = entry.get("create_time")
        if recorded_ct is None:
            continue  # no PID-reuse guard recorded — refuse to kill blindly
        try:
            proc = psutil.Process(entry["pid"])
            if abs(proc.create_time() - recorded_ct) > 1.0:
                continue  # PID was recycled by an unrelated process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        victims.append(proc)
    if victims:
        _terminate_procs(victims, grace_sec=grace_sec)
        logger.info("Reaped %d recorded owned orphan(s) for %s:%d", len(victims), host, port)


def _collect_jc_process_tree(pid: int) -> list[psutil.Process]:
    """Return the Job-Cannon-looking processes among ``pid``, its parent, children.

    Werkzeug's reloader runs as a parent + worker pair; either holding the port
    is enough to block a rebind, so both must be collected. Non-JC relatives
    (e.g. a spawned ``ollama.exe`` child) are filtered out by cmdline so the
    reclaim never reaps an unrelated process.
    """
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []

    candidates: list[psutil.Process] = [proc]
    try:
        parent = proc.parent()
        if parent is not None:
            candidates.append(parent)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    try:
        candidates.extend(proc.children(recursive=False))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    selected: dict[int, psutil.Process] = {}
    for candidate in candidates:
        try:
            candidate_pid = candidate.pid
            cmdline = " ".join(candidate.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if candidate_pid in selected:
            continue
        if ("job-cannon" in cmdline) or ("job_finder" in cmdline):
            selected[candidate_pid] = candidate
    return list(selected.values())


def _terminate_procs(procs: list[psutil.Process], *, grace_sec: float = 5.0) -> None:
    """SIGTERM each process, wait up to ``grace_sec``, then SIGKILL stragglers."""
    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for proc in procs:
        try:
            proc.wait(timeout=grace_sec)
        except psutil.TimeoutExpired:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


# ---------------------------------------------------------------------------
# Invocation resolution (frozen exe vs python -m)
# ---------------------------------------------------------------------------


def _resolve_invocation(subcommand: str) -> list[str]:
    """Return the argv that launches ``job-cannon <subcommand>`` at supervise time.

    A PyInstaller-frozen build IS ``job-cannon.exe`` (``sys.frozen`` set), so it
    takes the subcommand directly. A pip/uv install exposes the ``job-cannon``
    console script on PATH. A bare source checkout falls back to
    ``python -m job_finder``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, subcommand]
    console_script = shutil.which("job-cannon")
    if console_script:
        return [console_script, subcommand]
    return [sys.executable, "-m", "job_finder", subcommand]


def _quote_argv(argv: list[str]) -> str:
    """Join argv into a single command string, double-quoting parts with spaces."""
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


def _supervisor_log_path() -> Path:
    """Per-OS supervisor stdout/stderr log path (under the user-data logs dir)."""
    return logs_path().parent / "supervisor.log"


# ---------------------------------------------------------------------------
# Manifest renderers (pure — return a new string)
# ---------------------------------------------------------------------------


def render_windows_task_xml() -> str:
    """Render the Windows Task Scheduler XML manifest.

    Per-user (``InteractiveToken`` + ``LeastPrivilege`` — never SYSTEM), a
    ``<LogonTrigger>`` for start-at-logon, and ``<RestartOnFailure>`` encoding
    the locked restart governance (RestartCount = ``_MAX_RESTARTS``,
    RestartInterval = ``_RESTART_WINDOW_SEC``).
    """
    argv = _resolve_invocation("serve")
    command = argv[0]
    arguments = _quote_argv(argv[1:])
    restart_interval = f"PT{_RESTART_WINDOW_SEC // 60}M"
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Job Cannon keepalive — launches at logon and "
        "restarts on failure (per-user, no admin).</Description>\n"
        "    <URI>\\JobCannon</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RestartOnFailure>\n"
        f"      <Interval>{restart_interval}</Interval>\n"
        f"      <Count>{_MAX_RESTARTS}</Count>\n"
        "    </RestartOnFailure>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{command}</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def render_launchd_plist() -> str:
    """Render the macOS launchd LaunchAgent plist.

    ``KeepAlive`` gives restart-always; ``ThrottleInterval`` is the per-restart
    backoff that bounds a crash-loop (launchd's governance lever).
    """
    argv = _resolve_invocation("serve")
    program_args = "\n".join(f"        <string>{part}</string>" for part in argv)
    log = str(_supervisor_log_path())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{_LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{program_args}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "    <key>ThrottleInterval</key>\n"
        f"    <integer>{_RESTART_BACKOFF_SEC}</integer>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{log}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{log}</string>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        "        <key>JOB_CANNON_NO_BROWSER</key>\n"
        "        <string>1</string>\n"
        "    </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_systemd_unit() -> str:
    """Render the Linux systemd ``--user`` unit.

    ``Restart=always`` + ``RestartSec`` (backoff) with
    ``StartLimitIntervalSec`` / ``StartLimitBurst`` in ``[Unit]`` (systemd
    v230+ placement) capping restarts per window — the locked governance.
    """
    exec_start = _quote_argv(_resolve_invocation("serve"))
    return (
        "[Unit]\n"
        "Description=Job Cannon — personal job search command center\n"
        "After=network.target\n"
        f"StartLimitIntervalSec={_RESTART_WINDOW_SEC}\n"
        f"StartLimitBurst={_MAX_RESTARTS}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        f"RestartSec={_RESTART_BACKOFF_SEC}\n"
        "Environment=JOB_CANNON_NO_BROWSER=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# ---------------------------------------------------------------------------
# Manifest paths
# ---------------------------------------------------------------------------


def _home() -> Path:
    """User home directory. Indirected so tests can isolate agent dirs."""
    return Path.home()


def _windows_task_xml_path() -> Path:
    """Transient XML we feed to ``schtasks /xml`` (under the user-data logs dir)."""
    return logs_path().parent / "JobCannon-task.xml"


def _launchd_plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return _home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a registration command, capturing output. Never raises on non-zero."""
    logger.info("supervisor: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _install_windows() -> int:
    path = _windows_task_xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # schtasks /xml is happiest with a UTF-16 file matching the XML declaration.
    path.write_text(render_windows_task_xml(), encoding="utf-16")
    proc = _run(["schtasks", "/create", "/tn", _TASK_NAME, "/xml", str(path), "/f"])
    if proc.returncode != 0:
        print(f"supervisor-install: schtasks failed: {proc.stderr.strip()}", file=sys.stderr)
        return proc.returncode
    print(f"Installed Job Cannon supervisor (Scheduled Task '{_TASK_NAME}', at logon).")
    return 0


def _uninstall_windows() -> int:
    # /delete on a missing task returns non-zero — ignore for idempotency.
    _run(["schtasks", "/delete", "/tn", _TASK_NAME, "/f"])
    _windows_task_xml_path().unlink(missing_ok=True)
    print(f"Removed Job Cannon supervisor (Scheduled Task '{_TASK_NAME}').")
    return 0


def _install_macos() -> int:
    path = _launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_launchd_plist(), encoding="utf-8")
    # Unload any previous registration first so re-install is clean; ignore the
    # "not loaded" failure on a fresh install.
    _run(["launchctl", "unload", str(path)])
    proc = _run(["launchctl", "load", str(path)])
    if proc.returncode != 0:
        print(f"supervisor-install: launchctl load failed: {proc.stderr.strip()}", file=sys.stderr)
        return proc.returncode
    print(f"Installed Job Cannon supervisor (launchd LaunchAgent at {path}).")
    return 0


def _uninstall_macos() -> int:
    path = _launchd_plist_path()
    _run(["launchctl", "unload", str(path)])  # ignore failure when not loaded
    path.unlink(missing_ok=True)
    print("Removed Job Cannon supervisor (launchd LaunchAgent).")
    return 0


def _install_linux() -> int:
    path = _systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_systemd_unit(), encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    proc = _run(["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT])
    if proc.returncode != 0:
        print(f"supervisor-install: systemctl failed: {proc.stderr.strip()}", file=sys.stderr)
        return proc.returncode
    print(f"Installed Job Cannon supervisor (systemd --user unit at {path}).")
    return 0


def _uninstall_linux() -> int:
    _run(["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT])  # ignore failure
    _systemd_unit_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    print("Removed Job Cannon supervisor (systemd --user unit).")
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _supervisor_action(plat: str, *, uninstall: bool) -> int:
    """Install or uninstall the per-OS supervisor manifest. Returns an exit code.

    Shared by ``supervisor-install`` and ``stop`` (which disables the supervisor
    so it cannot relaunch the instance being stopped).
    """
    if plat.startswith("win"):
        return _uninstall_windows() if uninstall else _install_windows()
    if plat == "darwin":
        return _uninstall_macos() if uninstall else _install_macos()
    if plat.startswith("linux"):
        return _uninstall_linux() if uninstall else _install_linux()

    print(
        f"supervisor: unsupported platform {plat!r}. Supervisor manifests are "
        "only generated for Windows, macOS, and Linux.",
        file=sys.stderr,
    )
    return 1


def cmd_supervisor_install(args, *, platform: str | None = None) -> int:
    """Install (or, with ``--uninstall``, remove) the per-OS keepalive supervisor.

    ``platform`` is injectable for tests; production resolves it from
    ``sys.platform``. Returns a process exit code (0 = success).
    """
    plat = platform if platform is not None else sys.platform
    return _supervisor_action(plat, uninstall=bool(getattr(args, "uninstall", False)))


def is_supervisor_installed(platform: str | None = None) -> bool:
    """True if the per-OS keepalive supervisor manifest is currently installed.

    Windows queries the Scheduled Task; macOS / Linux check for the manifest
    file. Best-effort — any error is reported as "not installed".
    """
    plat = platform if platform is not None else sys.platform
    try:
        if plat.startswith("win"):
            return _run(["schtasks", "/query", "/tn", _TASK_NAME]).returncode == 0
        if plat == "darwin":
            return _launchd_plist_path().exists()
        if plat.startswith("linux"):
            return _systemd_unit_path().exists()
    except Exception:  # pragma: no cover - status probe must never raise
        logger.debug("is_supervisor_installed probe failed", exc_info=True)
    return False


def _iter_claim_markers() -> list[dict]:
    """Return parsed metadata for every ``server*.json`` claim marker on disk.

    Each dict is the raw sidecar content (``pid``/``host``/``port``/
    ``owned_pids``/…). Unreadable markers are skipped. The single source the
    ``stop`` and ``doctor`` commands use to discover what is (or was) running.
    """
    logs_dir = user_data_root() / "logs"
    if not logs_dir.exists():
        return []
    markers: list[dict] = []
    for meta in sorted(logs_dir.glob("server*.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            markers.append(data)
    return markers


def cmd_stop(args, *, platform: str | None = None) -> int:
    """Stop the running Job Cannon instance(s) and disable the keepalive supervisor.

    Order matters: the supervisor is disabled FIRST so it cannot relaunch the
    instance during the shutdown grace window. Then every ``(host, port)`` the
    claim metadata records is freed — terminating the JC process tree and
    sweeping recorded owned children (e.g. a spawned Ollama). Idempotent: with
    nothing running it still disables the supervisor and reports cleanly.
    """
    plat = platform if platform is not None else sys.platform

    # 1. Disable the supervisor so a self-restart cannot race the stop.
    _supervisor_action(plat, uninstall=True)

    # 2. Terminate each recorded instance.
    targets = [
        (m["host"], m["port"])
        for m in _iter_claim_markers()
        if isinstance(m.get("host"), str) and isinstance(m.get("port"), int)
    ]
    if not targets:
        print("Job Cannon: no running instance found (supervisor disabled).")
        return 0
    for host, port in targets:
        if free_jc_port(host, port):
            print(f"Stopped Job Cannon on {host}:{port}.")
        else:
            print(
                f"Job Cannon: {host}:{port} is held by a process that is not Job "
                f"Cannon — left untouched.",
                file=sys.stderr,
            )
    return 0


def cmd_doctor(args, *, platform: str | None = None) -> int:
    """Print read-only lifecycle diagnostics: claim markers, liveness, supervisor.

    Never builds the app, starts a scheduler, or acquires a lock — a passive
    observer like ``healthcheck``. Always exits 0 (it reports state; it does not
    judge health). Honors ``--user-data-dir`` if present on ``args``.
    """
    import os

    override = getattr(args, "user_data_dir", None)
    if override:
        os.environ["JOB_CANNON_USER_DATA_DIR"] = override

    plat = platform if platform is not None else sys.platform
    print("Job Cannon — doctor")
    print("===================")
    print(f"user data dir : {user_data_root()}")
    print(f"supervisor    : {'installed' if is_supervisor_installed(plat) else 'not installed'}")

    markers = _iter_claim_markers()
    if not markers:
        print("instances     : none (no server*.json claim marker on disk)")
        return 0

    print(f"instances     : {len(markers)} claim marker(s)")
    for m in markers:
        pid = m.get("pid")
        host, port = m.get("host", "?"), m.get("port", "?")
        try:
            alive = isinstance(pid, int) and psutil.pid_exists(pid)
        except Exception:
            alive = False
        state = "ALIVE" if alive else "dead"
        print(f"  - {host}:{port} pid={pid} [{state}] started={m.get('start_time_utc', '?')}")
        owned = m.get("owned_pids") or []
        for o in owned:
            if isinstance(o, dict):
                opid = o.get("pid")
                try:
                    oalive = isinstance(opid, int) and psutil.pid_exists(opid)
                except Exception:
                    oalive = False
                print(
                    f"      owned: pid={opid} ({o.get('name', '?')}) "
                    f"[{'ALIVE' if oalive else 'dead'}]"
                )
    return 0


# Re-exported so callers can resolve supervisor state paths consistently.
__all__ = [
    "cmd_doctor",
    "cmd_stop",
    "cmd_supervisor_install",
    "free_jc_port",
    "is_supervisor_installed",
    "render_launchd_plist",
    "render_systemd_unit",
    "render_windows_task_xml",
    "user_data_root",
]
