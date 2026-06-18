"""Tests for the ``supervisor-install`` / ``serve`` CLI surface (#439).

Coverage:
- parser wiring — the new subcommands parse and ``--help`` exits 0; the bare
  invocation and the existing ``healthcheck`` subcommand are unregressed.
- manifest renderers — each carries the locked restart-governance keys and the
  no-SYSTEM / least-privilege guarantees.
- install/uninstall — the three platforms render-and-write the correct manifest,
  registration commands are issued (mocked), and both are idempotent.
- pre-bind port reclaim — a confirmed-JC listener's parent AND worker are both
  terminated; a foreign listener is never killed and the function reports
  non-zero (abort).

No real processes are spawned and no real scheduler/launchctl/systemctl runs:
``subprocess.run`` and ``psutil`` are monkeypatched, and ``JOB_CANNON_USER_DATA_DIR``
plus an indirected ``_home()`` isolate every filesystem write under ``tmp_path``.
"""

from __future__ import annotations

import argparse

import pytest

from job_finder import __main__ as main_mod
from job_finder.web import supervisor

# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------


def test_bare_invocation_has_no_command():
    args = main_mod._build_parser().parse_args([])
    assert args.command is None
    # Top-level flags still resolve on the default path.
    assert args.terminal is False
    assert args.port is None


def test_healthcheck_subcommand_still_registered():
    args = main_mod._build_parser().parse_args(["healthcheck"])
    assert args.command == "healthcheck"


def test_serve_subcommand_parses_own_flags():
    args = main_mod._build_parser().parse_args(["serve", "--terminal", "--port", "5050"])
    assert args.command == "serve"
    assert args.serve_terminal is True
    assert args.serve_port == 5050


def test_supervisor_install_subcommand_parses_uninstall():
    parser = main_mod._build_parser()
    assert parser.parse_args(["supervisor-install"]).uninstall is False
    assert parser.parse_args(["supervisor-install", "--uninstall"]).uninstall is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["--version"],
        ["serve", "--help"],
        ["healthcheck", "--help"],
        ["supervisor-install", "--help"],
    ],
)
def test_help_and_version_exit_zero(argv):
    """--help / --version on every subcommand exit cleanly (SystemExit code 0)."""
    with pytest.raises(SystemExit) as exc:
        main_mod._build_parser().parse_args(argv)
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Manifest renderers — locked governance keys
# ---------------------------------------------------------------------------


def test_windows_task_xml_governance():
    xml = supervisor.render_windows_task_xml()
    # Logon trigger + restart governance (RestartCount / RestartInterval).
    assert "<LogonTrigger>" in xml
    assert "<RestartOnFailure>" in xml
    assert f"<Count>{supervisor._MAX_RESTARTS}</Count>" in xml
    assert "<Interval>PT5M</Interval>" in xml
    # Per-user, no admin: least privilege, interactive token, never SYSTEM.
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "InteractiveToken" in xml
    assert "SYSTEM" not in xml


def test_launchd_plist_governance():
    plist = supervisor.render_launchd_plist()
    assert "<key>KeepAlive</key>" in plist
    assert "<key>ThrottleInterval</key>" in plist
    assert f"<integer>{supervisor._RESTART_BACKOFF_SEC}</integer>" in plist
    assert "<key>RunAtLoad</key>" in plist
    assert supervisor._LAUNCHD_LABEL in plist


def test_systemd_unit_governance():
    unit = supervisor.render_systemd_unit()
    assert "Restart=always" in unit
    assert f"StartLimitBurst={supervisor._MAX_RESTARTS}" in unit
    assert f"StartLimitIntervalSec={supervisor._RESTART_WINDOW_SEC}" in unit
    assert f"RestartSec={supervisor._RESTART_BACKOFF_SEC}" in unit
    assert "WantedBy=default.target" in unit


# ---------------------------------------------------------------------------
# install / uninstall — render-and-write + registration (mocked), idempotency
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def captured_run(monkeypatch):
    """Record every registration command instead of executing it."""
    calls: list[list[str]] = []

    def _fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(supervisor.subprocess, "run", _fake_run)
    return calls


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Route every supervisor filesystem write under tmp_path."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path / "ud"))
    monkeypatch.setattr(supervisor, "_home", lambda: tmp_path / "home")
    return tmp_path


def _args(uninstall=False):
    return argparse.Namespace(uninstall=uninstall)


def test_install_windows_writes_xml_and_registers(captured_run, isolated_paths):
    code = supervisor.cmd_supervisor_install(_args(), platform="win32")
    assert code == 0
    path = supervisor._windows_task_xml_path()
    assert path.exists()
    xml = path.read_text(encoding="utf-16")
    assert "<LogonTrigger>" in xml
    # Registered via schtasks /create /xml, never with /ru SYSTEM.
    create = next(c for c in captured_run if "/create" in c)
    assert create[:3] == ["schtasks", "/create", "/tn"]
    assert "/xml" in create
    assert "SYSTEM" not in " ".join(create)


def test_install_macos_writes_plist_and_loads(captured_run, isolated_paths):
    code = supervisor.cmd_supervisor_install(_args(), platform="darwin")
    assert code == 0
    path = supervisor._launchd_plist_path()
    assert path.exists()
    assert "<key>KeepAlive</key>" in path.read_text(encoding="utf-8")
    assert any(c[:2] == ["launchctl", "load"] for c in captured_run)


def test_install_linux_writes_unit_and_enables(captured_run, isolated_paths):
    code = supervisor.cmd_supervisor_install(_args(), platform="linux")
    assert code == 0
    path = supervisor._systemd_unit_path()
    assert path.exists()
    assert "Restart=always" in path.read_text(encoding="utf-8")
    assert any(c[:4] == ["systemctl", "--user", "enable", "--now"] for c in captured_run)
    assert any("daemon-reload" in c for c in captured_run)


def test_uninstall_removes_manifest_and_deregisters(captured_run, isolated_paths):
    supervisor.cmd_supervisor_install(_args(), platform="linux")
    path = supervisor._systemd_unit_path()
    assert path.exists()
    code = supervisor.cmd_supervisor_install(_args(uninstall=True), platform="linux")
    assert code == 0
    assert not path.exists()
    assert any(c[:3] == ["systemctl", "--user", "disable"] for c in captured_run)


def test_uninstall_on_missing_manifest_is_noop_success(captured_run, isolated_paths):
    # Nothing installed: uninstall must still succeed (idempotent no-op).
    code = supervisor.cmd_supervisor_install(_args(uninstall=True), platform="darwin")
    assert code == 0
    assert not supervisor._launchd_plist_path().exists()


def test_reinstall_overwrites_cleanly(captured_run, isolated_paths):
    assert supervisor.cmd_supervisor_install(_args(), platform="linux") == 0
    # Second install is a clean overwrite, not an error.
    assert supervisor.cmd_supervisor_install(_args(), platform="linux") == 0
    assert supervisor._systemd_unit_path().exists()


def test_unsupported_platform_returns_nonzero(captured_run, isolated_paths, capsys):
    code = supervisor.cmd_supervisor_install(_args(), platform="sunos5")
    assert code == 1
    assert "unsupported platform" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Pre-bind port reclaim — kill BOTH, never kill foreign
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal psutil.Process stand-in recording terminate/kill/wait."""

    def __init__(self, pid, cmdline, *, parent=None, children=(), registry=None):
        self.pid = pid
        self._cmdline = cmdline
        self._parent = parent
        self._children = list(children)
        self.terminated = False
        self.killed = False
        if registry is not None:
            registry[pid] = self

    def cmdline(self):
        return self._cmdline

    def parent(self):
        return self._parent

    def children(self, recursive=False):
        return list(self._children)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_free_jc_port_already_free(monkeypatch):
    monkeypatch.setattr(main_mod, "_port_is_listening", lambda h, p: False)
    assert supervisor.free_jc_port("127.0.0.1", 5000) is True


def test_free_jc_port_kills_both_parent_and_worker(monkeypatch):
    registry: dict[int, _FakeProcess] = {}
    parent = _FakeProcess(99, ["python", "-m", "job_finder", "serve"], registry=registry)
    worker = _FakeProcess(100, ["python", "-m", "job_finder"], parent=parent, registry=registry)

    monkeypatch.setattr(main_mod, "_port_is_listening", lambda h, p: True)
    # Listener identified as JC, pid = the worker.
    monkeypatch.setattr(
        main_mod,
        "_listener_looks_like_jc",
        lambda h, p: (True, "python -m job_finder", 100),
    )
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: registry[pid])

    assert supervisor.free_jc_port("127.0.0.1", 5000, grace_sec=0.01) is True
    # BOTH the worker and its reloader parent were terminated.
    assert worker.terminated is True
    assert parent.terminated is True


def test_free_jc_port_does_not_kill_foreign(monkeypatch):
    registry: dict[int, _FakeProcess] = {}
    foreign = _FakeProcess(200, ["nginx", "-g", "daemon off;"], registry=registry)

    monkeypatch.setattr(main_mod, "_port_is_listening", lambda h, p: True)
    monkeypatch.setattr(main_mod, "_listener_looks_like_jc", lambda h, p: (False, "nginx", 200))

    def _boom(pid):  # pragma: no cover - must never be reached
        raise AssertionError("psutil.Process must not be touched for a foreign listener")

    monkeypatch.setattr(supervisor.psutil, "Process", _boom)

    assert supervisor.free_jc_port("127.0.0.1", 5000) is False
    assert foreign.terminated is False
    assert foreign.killed is False


def test_collect_tree_filters_non_jc_children(monkeypatch):
    """A spawned ollama child of the JC worker is NOT collected for termination."""
    registry: dict[int, _FakeProcess] = {}
    ollama = _FakeProcess(101, ["ollama", "serve"], registry=registry)
    worker = _FakeProcess(
        100, ["python", "-m", "job_finder"], children=[ollama], registry=registry
    )
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: registry[pid])

    collected = supervisor._collect_jc_process_tree(100)
    pids = {p.pid for p in collected}
    assert 100 in pids
    assert 101 not in pids  # ollama filtered out
    assert worker in collected
