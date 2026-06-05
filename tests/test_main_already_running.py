"""Tests for the already-running detection decision tree (Issue #38, Commit B).

Covers all 14+ branches described in the acceptance criteria §8.5:

Probe layer:
  1.  probe_existing_jc returns jc-dict  → main() exits 0, opens browser
  2.  probe=None, listener cmdline is JC → main() exits 0 (pre-plan handoff)
  3.  probe=None, listener cmdline is foreign → main() exits 1 with diagnostic
  4.  probe=None, listener on wrong interface → treated as foreign
  5.  probe=None, port free, lock free → main() continues to create_app

Lock-contention layer (handle_existing_instance):
  6.  dead-PID, retry succeeds        → CONTINUE_STARTUP
  7.  dead-PID, retry exhausted       → EXIT_FAILURE
  8.  alive, AccessDenied             → EXIT_FAILURE with diagnostic
  9.  alive, PID-reuse cmdline        → retry (same as dead-pid path)
  10. alive, matching cmdline         → EXIT_SUCCESS (browser opened)
  11. missing metadata               → retry
  12. corrupt metadata               → EXIT_FAILURE
  13. mid-startup (no meta yet), then retry succeeds → CONTINUE_STARTUP
  14. wildcard bind host (0.0.0.0)   → probe uses 127.0.0.1, exits 0
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import psutil
import pytest

import job_finder.__main__ as main_mod
from job_finder.__main__ import (
    _listener_looks_like_jc,
    _port_is_listening,
    _retry_lock_or_fail,
    handle_existing_instance,
    probe_existing_jc,
)
from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _meta(pid: int = 12345, url: str = "http://127.0.0.1:5000") -> dict:
    return {"pid": pid, "url": url, "start_time_utc": "2026-01-01T00:00:00Z"}


def _fake_lock_paths(tmp_path: Path):
    return tmp_path / "server.lock", tmp_path / "server.json"


# ---------------------------------------------------------------------------
# 1. probe_existing_jc unit tests
# ---------------------------------------------------------------------------


def test_probe_returns_jc_dict_on_healthy_response():
    """Returns the parsed dict when app == 'job-cannon'."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"app": "job-cannon", "pid": 99, "version": "1.0"}

    with patch("job_finder.__main__.requests.get", return_value=mock_resp):
        result = probe_existing_jc("http://127.0.0.1:5000")

    assert result is not None
    assert result["app"] == "job-cannon"


def test_probe_returns_none_on_connection_error():
    import requests

    with patch("job_finder.__main__.requests.get", side_effect=requests.ConnectionError):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_returns_none_on_timeout():
    import requests

    with patch("job_finder.__main__.requests.get", side_effect=requests.Timeout):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_returns_none_on_non_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("job_finder.__main__.requests.get", return_value=mock_resp):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_returns_none_on_non_json():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("no JSON")
    with patch("job_finder.__main__.requests.get", return_value=mock_resp):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_returns_none_when_identity_marker_missing():
    """Endpoint up but ``app`` field is absent or wrong → not JC."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"app": "something-else"}
    with patch("job_finder.__main__.requests.get", return_value=mock_resp):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_returns_none_on_oserror():
    """OSError during connect (e.g. ENETDOWN) is treated as None."""
    with patch("job_finder.__main__.requests.get", side_effect=OSError):
        assert probe_existing_jc("http://127.0.0.1:5000") is None


def test_probe_strips_trailing_slash():
    """URL with trailing slash must still reach /__jc_health without double slash."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"app": "job-cannon"}
    with patch("job_finder.__main__.requests.get", return_value=mock_resp) as mock_get:
        probe_existing_jc("http://127.0.0.1:5000/")
    url_called = mock_get.call_args[0][0]
    assert "/__jc_health" in url_called
    assert "//" not in url_called.replace("://", "")


# ---------------------------------------------------------------------------
# 2. _port_is_listening unit tests
# ---------------------------------------------------------------------------


def test_port_is_listening_returns_true_on_success():
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    with patch("job_finder.__main__.socket.create_connection", return_value=mock_sock):
        assert _port_is_listening("127.0.0.1", 5000) is True


def test_port_is_listening_returns_false_on_refused():
    with patch(
        "job_finder.__main__.socket.create_connection",
        side_effect=ConnectionRefusedError,
    ):
        assert _port_is_listening("127.0.0.1", 5000) is False


def test_port_is_listening_returns_false_on_oserror():
    with patch("job_finder.__main__.socket.create_connection", side_effect=OSError):
        assert _port_is_listening("127.0.0.1", 5000) is False


def test_port_is_listening_returns_false_on_timeout():
    with patch(
        "job_finder.__main__.socket.create_connection",
        side_effect=socket.timeout,
    ):
        assert _port_is_listening("127.0.0.1", 5000) is False


# ---------------------------------------------------------------------------
# 3. _listener_looks_like_jc unit tests
# ---------------------------------------------------------------------------


def _make_conn(ip: str, port: int, pid: int, cmdline: list[str]):
    """Build a mock psutil net_connection-like object."""
    conn = MagicMock()
    conn.status = psutil.CONN_LISTEN
    conn.laddr = MagicMock()
    conn.laddr.ip = ip
    conn.laddr.port = port
    conn.pid = pid

    proc = MagicMock()
    proc.cmdline.return_value = cmdline
    return conn, proc


def test_listener_looks_like_jc_job_cannon_cmdline():
    """Listener with 'job-cannon' in cmdline → looks_like_jc=True."""
    conn, proc = _make_conn("127.0.0.1", 5000, 111, ["uv", "run", "job-cannon"])
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=proc),
    ):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is True
    assert "job-cannon" in cmdline
    assert pid == 111


def test_listener_looks_like_jc_job_finder_cmdline():
    """Listener with 'job_finder' in cmdline → looks_like_jc=True."""
    conn, proc = _make_conn("127.0.0.1", 5000, 222, ["python", "-m", "job_finder"])
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=proc),
    ):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is True


def test_listener_is_foreign_process():
    """Listener cmdline does not match JC → looks_like_jc=False."""
    conn, proc = _make_conn("127.0.0.1", 5000, 333, ["python", "-m", "http.server", "5000"])
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=proc),
    ):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is False
    assert cmdline is not None  # diagnostic string available
    assert pid == 333


def test_listener_on_wrong_interface_skipped():
    """Branch 4: loopback-targeted probe, listener on non-loopback IP → treat as no match.

    This is the 'listener-on-wrong-interface' acceptance-criteria branch.
    The probe targets localhost/127.0.0.1, so a listener bound to 10.0.0.1
    (a non-loopback interface) must NOT be claimed as JC.
    """
    # Listener is on 10.0.0.1 (non-loopback), but we're probing 127.0.0.1
    conn_wrong, proc_wrong = _make_conn("10.0.0.1", 5000, 444, ["job-cannon"])
    with (
        patch("psutil.net_connections", return_value=[conn_wrong]),
        patch("psutil.Process", return_value=proc_wrong),
    ):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    # The non-loopback listener is skipped entirely → returns (False, None, None)
    assert looks is False
    assert cmdline is None
    assert pid is None


def test_listener_wildcard_bind_matches_loopback_probe():
    """Wildcard bind (0.0.0.0) is treated as matching a loopback probe."""
    conn, proc = _make_conn("0.0.0.0", 5000, 555, ["job-cannon"])
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=proc),
    ):
        looks, _, _ = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is True


def test_listener_access_denied_on_cmdline():
    """psutil.AccessDenied on cmdline → returns (False, None, pid)."""
    conn, proc = _make_conn("127.0.0.1", 5000, 999, [])
    proc.cmdline.side_effect = psutil.AccessDenied(999)
    with (
        patch("psutil.net_connections", return_value=[conn]),
        patch("psutil.Process", return_value=proc),
    ):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is False
    assert cmdline is None
    assert pid == 999


def test_listener_no_connections_returns_false():
    """No connections on the port → (False, None, None)."""
    with patch("psutil.net_connections", return_value=[]):
        looks, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)
    assert looks is False
    assert cmdline is None
    assert pid is None


# ---------------------------------------------------------------------------
# 4. handle_existing_instance unit tests (branches 6-13)
# ---------------------------------------------------------------------------


def _patch_retry(acquired: bool, tmp_path: Path):
    """Patch acquire_pidfile to return acquired=True or False on retry."""
    from job_finder.web._pidfile import AcquireResult

    return patch(
        "job_finder.__main__.acquire_pidfile",
        return_value=AcquireResult(acquired=acquired),
    )


def test_handle_missing_metadata_retries_and_succeeds(tmp_path):
    """Branch 11/13: existing_meta=None → _retry_lock_or_fail → CONTINUE_STARTUP on success."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)

    with (
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=True),
        ),
        patch("time.sleep"),
    ):
        action = handle_existing_instance(None, "http://127.0.0.1:5000", lock_path, meta_path, _meta())

    assert action == ExistingInstanceAction.CONTINUE_STARTUP


def test_handle_corrupt_pid_field_exits_failure(tmp_path, capsys):
    """Branch 12: PID is not an int → EXIT_FAILURE with diagnostic."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    bad_meta = {"pid": "not-an-int", "url": "http://127.0.0.1:5000"}

    action = handle_existing_instance(bad_meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta())

    assert action == ExistingInstanceAction.EXIT_FAILURE
    captured = capsys.readouterr()
    assert "corrupt" in captured.err.lower()


def test_handle_dead_pid_retry_succeeds(tmp_path):
    """Branch 6: PID not alive → _retry_lock_or_fail → CONTINUE_STARTUP."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    dead_meta = _meta(pid=99999999)

    with (
        patch("psutil.pid_exists", return_value=False),
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=True),
        ),
        patch("time.sleep"),
    ):
        action = handle_existing_instance(
            dead_meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.CONTINUE_STARTUP


def test_handle_dead_pid_retry_exhausted(tmp_path, capsys):
    """Branch 7: PID dead, retry exhausted → EXIT_FAILURE with lock diagnostic."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    dead_meta = _meta(pid=99999999)

    with (
        patch("psutil.pid_exists", return_value=False),
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=False, existing=None),
        ),
        patch("time.sleep"),
    ):
        action = handle_existing_instance(
            dead_meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.EXIT_FAILURE
    captured = capsys.readouterr()
    assert "lock contention" in captured.err.lower() or "contention" in captured.err


def test_handle_access_denied_exits_failure(tmp_path, capsys):
    """Branch 8: alive but AccessDenied on cmdline (different user) → EXIT_FAILURE."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    live_meta = _meta(pid=12345)

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
    ):
        mock_proc_cls.return_value.cmdline.side_effect = psutil.AccessDenied(12345)
        action = handle_existing_instance(
            live_meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.EXIT_FAILURE
    captured = capsys.readouterr()
    assert "different user" in captured.err


def test_handle_no_such_process_retries(tmp_path):
    """NoSuchProcess (died between pid_exists and cmdline) → retry → CONTINUE_STARTUP."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    meta = _meta(pid=12345)

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=True),
        ),
        patch("time.sleep"),
    ):
        mock_proc_cls.return_value.cmdline.side_effect = psutil.NoSuchProcess(12345)
        action = handle_existing_instance(
            meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.CONTINUE_STARTUP


def test_handle_pid_reuse_retries(tmp_path):
    """Branch 9: alive, cmdline belongs to unrelated process → retry → CONTINUE_STARTUP."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    meta = _meta(pid=12345)

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=True),
        ),
        patch("time.sleep"),
    ):
        mock_proc_cls.return_value.cmdline.return_value = ["python", "unrelated_app.py"]
        action = handle_existing_instance(
            meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.CONTINUE_STARTUP


def test_handle_live_jc_instance_exits_success(tmp_path, capsys):
    """Branch 10: alive, cmdline is job-cannon → EXIT_SUCCESS, browser opened."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    meta = _meta(pid=12345, url="http://127.0.0.1:5000")

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
        patch("job_finder.__main__._open_browser") as mock_browser,
        patch.dict("os.environ", {}, clear=False),
    ):
        # Remove JOB_CANNON_NO_BROWSER if set
        import os
        os.environ.pop("JOB_CANNON_NO_BROWSER", None)
        mock_proc_cls.return_value.cmdline.return_value = ["uv", "run", "job-cannon"]
        action = handle_existing_instance(
            meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.EXIT_SUCCESS
    captured = capsys.readouterr()
    assert "already running" in captured.out
    mock_browser.assert_called_once_with("http://127.0.0.1:5000")


def test_handle_live_jc_no_browser(tmp_path, monkeypatch):
    """EXIT_SUCCESS with JOB_CANNON_NO_BROWSER=1 → browser NOT opened."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    meta = _meta(pid=12345)
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
        patch("job_finder.__main__._open_browser") as mock_browser,
    ):
        mock_proc_cls.return_value.cmdline.return_value = ["job-cannon"]
        action = handle_existing_instance(
            meta, "http://127.0.0.1:5000", lock_path, meta_path, _meta()
        )

    assert action == ExistingInstanceAction.EXIT_SUCCESS
    mock_browser.assert_not_called()


def test_handle_live_jc_uses_url_from_metadata(tmp_path, capsys):
    """The URL shown to the user comes from existing_meta['url'], not default_url."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)
    meta_with_custom_url = _meta(pid=12345, url="http://127.0.0.1:8080")

    with (
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process") as mock_proc_cls,
        patch("job_finder.__main__._open_browser"),
        patch.dict("os.environ", {"JOB_CANNON_NO_BROWSER": "1"}),
    ):
        mock_proc_cls.return_value.cmdline.return_value = ["job-cannon"]
        handle_existing_instance(
            meta_with_custom_url,
            "http://127.0.0.1:5000",  # fallback default
            lock_path,
            meta_path,
            _meta(),
        )

    captured = capsys.readouterr()
    assert "8080" in captured.out


# ---------------------------------------------------------------------------
# 5. main() integration tests (all heavy mocking)
# ---------------------------------------------------------------------------


def _common_patches(
    *,
    probe_result=None,
    port_listening=False,
    listener_result=(False, None, None),
    acquire_result=None,
    handle_result=ExistingInstanceAction.CONTINUE_STARTUP,
    cfg=None,
):
    """Return a context-manager stack that mocks all I/O in main()."""
    if acquire_result is None:
        acquire_result = AcquireResult(acquired=True)
    if cfg is None:
        cfg = {}
    fake_app = MagicMock()

    return (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.probe_existing_jc", return_value=probe_result),
        patch("job_finder.__main__._port_is_listening", return_value=port_listening),
        patch("job_finder.__main__._listener_looks_like_jc", return_value=listener_result),
        # acquire_pidfile is imported lazily inside main(); patch at the source so
        # the local `from job_finder.web._pidfile import acquire_pidfile` picks it up.
        patch("job_finder.web._pidfile.acquire_pidfile", return_value=acquire_result),
        patch("job_finder.__main__.handle_existing_instance", return_value=handle_result),
        patch("job_finder.__main__._open_browser"),
        patch("job_finder.__main__.sys.argv", ["job-cannon"]),
        # user_data_root is imported lazily inside main(); patch at the source.
        patch(
            "job_finder.web.user_data_dirs.user_data_root",
            return_value=Path("/tmp/jc-test"),
        ),
    )


def test_main_probe_returns_jc_exits_0_opens_browser(monkeypatch, capsys):
    """Branch 1: probe finds JC → sys.exit(0), browser opened."""
    monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)
    jc_health = {"app": "job-cannon", "pid": 99}

    patches = _common_patches(probe_result=jc_health)
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as mock_browser, patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 0
    mock_browser.assert_called_once()


def test_main_probe_returns_jc_no_browser(monkeypatch, capsys):
    """JOB_CANNON_NO_BROWSER=1 suppresses browser open even on probe hit."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    jc_health = {"app": "job-cannon", "pid": 99}

    patches = _common_patches(probe_result=jc_health)
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as mock_browser, patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 0
    mock_browser.assert_not_called()


def test_main_psutil_fallback_jc_exits_0(monkeypatch, capsys):
    """Branch 2: probe=None, port busy, listener is JC → exit 0 (pre-plan handoff).

    This is the load-bearing pre-plan-handoff test: a legacy instance without
    /__jc_health is detected via psutil cmdline, and the new launcher opens
    the browser and exits 0 without crashing on EADDRINUSE.
    """
    monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)

    patches = _common_patches(
        probe_result=None,
        port_listening=True,
        listener_result=(True, "uv run job-cannon", 42),
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as mock_browser, patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 0
    mock_browser.assert_called_once()
    captured = capsys.readouterr()
    assert "pre-upgrade" in captured.out or "running" in captured.out


def test_main_foreign_port_exits_1(capsys):
    """Branch 3: probe=None, port busy, listener is NOT JC → exit 1 with diagnostic."""
    patches = _common_patches(
        probe_result=None,
        port_listening=True,
        listener_result=(False, "python -m http.server 5000", 77),
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "occupied" in captured.err or "port" in captured.err.lower()
    # Diagnostic must name the foreign process
    assert "http.server" in captured.err or "python" in captured.err


def test_main_foreign_port_no_cmdline_exits_1(capsys):
    """Foreign port owner but cmdline is None (cross-user on Windows)."""
    patches = _common_patches(
        probe_result=None,
        port_listening=True,
        listener_result=(False, None, 77),  # cmdline=None
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # Diagnostic should mention PID 77
    assert "77" in captured.err or "port" in captured.err.lower()


def test_main_port_free_lock_free_continues(monkeypatch, capsys):
    """Branch 5: probe=None, port free, lock acquired → create_app called."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    patches = _common_patches(
        probe_result=None,
        port_listening=False,
        acquire_result=AcquireResult(acquired=True),
    )
    with patches[0], patches[1] as mock_create_app, patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        main_mod.main()

    mock_create_app.assert_called_once()


def test_main_lock_busy_handle_exits_success(monkeypatch):
    """Lock busy, handle_existing_instance → EXIT_SUCCESS → sys.exit(0)."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    patches = _common_patches(
        probe_result=None,
        port_listening=False,
        acquire_result=AcquireResult(acquired=False, existing={"pid": 12345}),
        handle_result=ExistingInstanceAction.EXIT_SUCCESS,
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 0


def test_main_lock_busy_handle_exits_failure(monkeypatch):
    """Lock busy, handle_existing_instance → EXIT_FAILURE → sys.exit(1)."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    patches = _common_patches(
        probe_result=None,
        port_listening=False,
        acquire_result=AcquireResult(acquired=False, existing=None),
        handle_result=ExistingInstanceAction.EXIT_FAILURE,
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 1


def test_main_lock_busy_continue_startup(monkeypatch, capsys):
    """Lock busy, handle_existing_instance → CONTINUE_STARTUP → create_app called."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

    patches = _common_patches(
        probe_result=None,
        port_listening=False,
        acquire_result=AcquireResult(acquired=False, existing=None),
        handle_result=ExistingInstanceAction.CONTINUE_STARTUP,
    )
    with patches[0], patches[1] as mock_create_app, patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        main_mod.main()

    mock_create_app.assert_called_once()


def test_main_wildcard_host_probes_127_0_0_1(monkeypatch):
    """Branch 14: bind_host=0.0.0.0 → probe uses 127.0.0.1 (not 0.0.0.0)."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    jc_health = {"app": "job-cannon", "pid": 99}

    patches = _common_patches(
        probe_result=jc_health,
        cfg={"server": {"host": "0.0.0.0", "port": 5000}},
    )
    with pytest.raises(SystemExit) as exc_info:
        with patches[0], patches[1], patches[2] as mock_probe, patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            main_mod.main()

    assert exc_info.value.code == 0
    # Probe must be called with 127.0.0.1 URL, not 0.0.0.0
    probe_url = mock_probe.call_args[0][0]
    assert "127.0.0.1" in probe_url
    assert "0.0.0.0" not in probe_url


# ---------------------------------------------------------------------------
# 6. _retry_lock_or_fail unit tests
# ---------------------------------------------------------------------------


def test_retry_lock_or_fail_succeeds_on_first_retry(tmp_path):
    """Lock acquired on first retry → CONTINUE_STARTUP."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)

    with (
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=True),
        ),
        patch("time.sleep") as mock_sleep,
    ):
        action = _retry_lock_or_fail("test_reason", lock_path, meta_path, _meta())

    assert action == ExistingInstanceAction.CONTINUE_STARTUP
    mock_sleep.assert_called()


def test_retry_lock_or_fail_exhausted_exits(tmp_path, capsys):
    """All retries fail → EXIT_FAILURE with reason and lock path in message."""
    lock_path, meta_path = _fake_lock_paths(tmp_path)

    with (
        patch(
            "job_finder.web._pidfile.acquire_pidfile",
            return_value=AcquireResult(acquired=False, existing=None),
        ),
        patch("time.sleep"),
    ):
        action = _retry_lock_or_fail("dead_pid", lock_path, meta_path, _meta())

    assert action == ExistingInstanceAction.EXIT_FAILURE
    captured = capsys.readouterr()
    assert "dead_pid" in captured.err
    assert str(lock_path) in captured.err
