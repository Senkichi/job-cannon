"""Tests for the already-running detection sequence in ``job_finder.__main__``.

Covers all branches of §8.5 (probe → psutil-listener → lock tree):

  Step 1 — probe_existing_jc():
    1a.  probe returns jc-dict → exit 0, browser opened
    1b.  probe returns None, port-free → continue to lock step

  Step 2 — _port_is_listening() + _listener_looks_like_jc():
    2a.  port busy, listener cmdline is jc → exit 0 (pre-plan handoff)
    2b.  port busy, listener cmdline is foreign → exit 1 with diagnostic
    2c.  port busy, listener on wrong interface (localhost probe, non-loopback bind)
         → treat as foreign
    2d.  port free → skip to lock step

  Step 3 — acquire_pidfile() + handle_existing_instance():
    3a.  lock free → CONTINUE_STARTUP
    3b.  lock busy + dead PID → retry → success → CONTINUE_STARTUP
    3c.  lock busy + dead PID → retry exhaustion → EXIT_FAILURE
    3d.  lock busy + alive different user (AccessDenied) → EXIT_FAILURE
    3e.  lock busy + alive different cmdline (PID reuse) → retry
    3f.  lock busy + alive matching cmdline → EXIT_SUCCESS
    3g.  lock busy + missing metadata → retry then success
    3h.  lock busy + corrupt metadata (non-int PID) → EXIT_FAILURE
    3i.  mid-startup no metadata yet → retry then success
    3j.  wildcard bind host (0.0.0.0) → probe uses 127.0.0.1 → exit 0

Also validates main() end-to-end for the exit-0 and exit-1 paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _sample_meta(pid: int | None = None, url: str = "http://127.0.0.1:5000") -> dict:
    return {
        "pid": pid if pid is not None else os.getpid(),
        "url": url,
        "start_time_utc": "2026-01-01T00:00:00Z",
        "lock_path": "/tmp/server.lock",
    }


@pytest.fixture()
def no_browser(monkeypatch):
    """Suppress real browser opens in all tests."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")


# ---------------------------------------------------------------------------
# probe_existing_jc() unit tests
# ---------------------------------------------------------------------------


class TestProbeExistingJc:
    def test_returns_dict_on_success(self) -> None:
        """probe_existing_jc returns parsed dict when /__jc_health responds."""
        from job_finder.__main__ import probe_existing_jc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"app": "job-cannon", "version": "1.0"}

        with patch("requests.get", return_value=mock_resp):
            result = probe_existing_jc("http://127.0.0.1:5000")

        assert result == {"app": "job-cannon", "version": "1.0"}

    def test_returns_none_on_connection_error(self) -> None:
        import requests

        from job_finder.__main__ import probe_existing_jc

        with patch("requests.get", side_effect=requests.ConnectionError):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None

    def test_returns_none_on_timeout(self) -> None:
        import requests

        from job_finder.__main__ import probe_existing_jc

        with patch("requests.get", side_effect=requests.Timeout):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None

    def test_returns_none_on_non_200(self) -> None:
        from job_finder.__main__ import probe_existing_jc

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None

    def test_returns_none_when_app_field_missing(self) -> None:
        from job_finder.__main__ import probe_existing_jc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}  # no "app" field
        with patch("requests.get", return_value=mock_resp):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None

    def test_returns_none_when_app_field_wrong(self) -> None:
        from job_finder.__main__ import probe_existing_jc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"app": "something-else"}
        with patch("requests.get", return_value=mock_resp):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None

    def test_returns_none_on_invalid_json(self) -> None:
        from job_finder.__main__ import probe_existing_jc

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch("requests.get", return_value=mock_resp):
            result = probe_existing_jc("http://127.0.0.1:5000")
        assert result is None


# ---------------------------------------------------------------------------
# _listener_looks_like_jc() unit tests
# ---------------------------------------------------------------------------


class TestListenerLooksLikeJc:
    def _make_conn(self, port, ip, pid, status=None):
        conn = MagicMock()
        conn.laddr = MagicMock()
        conn.laddr.port = port
        conn.laddr.ip = ip
        conn.pid = pid
        conn.status = status or psutil.CONN_LISTEN
        return conn

    def test_jc_listener_detected(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(5000, "127.0.0.1", 42)
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["uv", "run", "job-cannon"]

        with (
            patch("psutil.net_connections", return_value=[conn]),
            patch("psutil.Process", return_value=mock_proc),
        ):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is True
        assert "job-cannon" in cmdline
        assert pid == 42

    def test_job_finder_module_cmdline_detected(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(5000, "127.0.0.1", 99)
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "job_finder"]

        with (
            patch("psutil.net_connections", return_value=[conn]),
            patch("psutil.Process", return_value=mock_proc),
        ):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is True

    def test_foreign_listener_on_loopback(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(5000, "127.0.0.1", 77)
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "http.server", "5000"]

        with (
            patch("psutil.net_connections", return_value=[conn]),
            patch("psutil.Process", return_value=mock_proc),
        ):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is False
        assert "http.server" in cmdline

    def test_wrong_interface_skipped(self) -> None:
        """localhost probe must not claim a non-loopback bind as the listener."""
        from job_finder.__main__ import _listener_looks_like_jc

        # Listener is bound to 192.168.1.5, but we're probing 127.0.0.1.
        conn = self._make_conn(5000, "192.168.1.5", 55)
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["uv", "run", "job-cannon"]

        with (
            patch("psutil.net_connections", return_value=[conn]),
            patch("psutil.Process", return_value=mock_proc),
        ):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        # No matching connection found (wrong interface) → all None/False
        assert looks_like is False
        assert cmdline is None
        assert pid is None

    def test_port_mismatch_skipped(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(9999, "127.0.0.1", 11)  # different port
        with patch("psutil.net_connections", return_value=[conn]):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is False
        assert pid is None

    def test_non_listen_status_skipped(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(5000, "127.0.0.1", 11)
        conn.status = psutil.CONN_ESTABLISHED  # not LISTEN
        with patch("psutil.net_connections", return_value=[conn]):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is False

    def test_access_denied_on_cmdline_returns_false(self) -> None:
        from job_finder.__main__ import _listener_looks_like_jc

        conn = self._make_conn(5000, "127.0.0.1", 33)
        mock_proc = MagicMock()
        mock_proc.cmdline.side_effect = psutil.AccessDenied(33)

        with (
            patch("psutil.net_connections", return_value=[conn]),
            patch("psutil.Process", return_value=mock_proc),
        ):
            looks_like, cmdline, pid = _listener_looks_like_jc("127.0.0.1", 5000)

        assert looks_like is False
        assert cmdline is None
        assert pid == 33


# ---------------------------------------------------------------------------
# handle_existing_instance() unit tests — all §8.5 lock-busy branches
# ---------------------------------------------------------------------------


class TestHandleExistingInstance:
    """Tests for handle_existing_instance() dispatching logic."""

    def test_none_metadata_calls_retry(self, tmp_path: Path) -> None:
        """No metadata → _retry_lock_or_fail(reason='no_metadata')."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"
        metadata = _sample_meta()

        with patch(
            "job_finder.__main__._retry_lock_or_fail",
            return_value=ExistingInstanceAction.CONTINUE_STARTUP,
        ) as mock_retry:
            result = handle_existing_instance(
                None, "http://127.0.0.1:5000", lock_path, meta_path, metadata
            )

        assert result == ExistingInstanceAction.CONTINUE_STARTUP
        mock_retry.assert_called_once()
        assert mock_retry.call_args[0][0] == "no_metadata"

    def test_corrupt_pid_field_exits_failure(self, tmp_path: Path, capsys) -> None:
        """Non-int PID in metadata → EXIT_FAILURE with clear diagnostic."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"
        meta_with_bad_pid = {"pid": "not-an-int", "url": "http://127.0.0.1:5000"}

        result = handle_existing_instance(
            meta_with_bad_pid,
            "http://127.0.0.1:5000",
            lock_path,
            meta_path,
            _sample_meta(),
        )

        assert result == ExistingInstanceAction.EXIT_FAILURE
        captured = capsys.readouterr()
        assert "corrupt" in captured.err.lower()

    def test_dead_pid_calls_retry(self, tmp_path: Path) -> None:
        """PID not in process table → _retry_lock_or_fail(reason='dead_pid')."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"

        with (
            patch("psutil.pid_exists", return_value=False),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            result = handle_existing_instance(
                _sample_meta(pid=99999),
                "http://127.0.0.1:5000",
                lock_path,
                meta_path,
                _sample_meta(),
            )

        assert result == ExistingInstanceAction.CONTINUE_STARTUP
        assert mock_retry.call_args[0][0] == "dead_pid"

    def test_access_denied_exits_failure(self, tmp_path: Path, capsys) -> None:
        """psutil.AccessDenied on cmdline() → EXIT_FAILURE, diagnostic mentions PID."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"
        pid = 5050

        mock_proc = MagicMock()
        mock_proc.cmdline.side_effect = psutil.AccessDenied(pid)

        with (
            patch("psutil.pid_exists", return_value=True),
            patch("psutil.Process", return_value=mock_proc),
        ):
            result = handle_existing_instance(
                _sample_meta(pid=pid),
                "http://127.0.0.1:5000",
                lock_path,
                meta_path,
                _sample_meta(),
            )

        assert result == ExistingInstanceAction.EXIT_FAILURE
        captured = capsys.readouterr()
        assert str(pid) in captured.err
        assert "different user" in captured.err

    def test_pid_reuse_calls_retry(self, tmp_path: Path) -> None:
        """Alive PID with foreign cmdline → _retry_lock_or_fail(reason='pid_reuse')."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["nginx", "-g", "daemon off;"]

        with (
            patch("psutil.pid_exists", return_value=True),
            patch("psutil.Process", return_value=mock_proc),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            result = handle_existing_instance(
                _sample_meta(pid=100),
                "http://127.0.0.1:5000",
                lock_path,
                meta_path,
                _sample_meta(),
            )

        assert result == ExistingInstanceAction.CONTINUE_STARTUP
        assert mock_retry.call_args[0][0] == "pid_reuse"

    def test_confirmed_live_jc_instance_exits_success(
        self, tmp_path: Path, capsys, no_browser
    ) -> None:
        """Alive PID with matching cmdline → EXIT_SUCCESS, 'already running' message."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"
        url = "http://127.0.0.1:5000"

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["uv", "run", "job-cannon"]

        with (
            patch("psutil.pid_exists", return_value=True),
            patch("psutil.Process", return_value=mock_proc),
        ):
            result = handle_existing_instance(
                _sample_meta(pid=os.getpid(), url=url),
                url,
                lock_path,
                meta_path,
                _sample_meta(),
            )

        assert result == ExistingInstanceAction.EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "already running" in captured.out
        assert url in captured.out

    def test_race_death_calls_retry(self, tmp_path: Path) -> None:
        """Process dies between pid_exists and cmdline() → race_death retry."""
        from job_finder.__main__ import handle_existing_instance
        from job_finder.web._pidfile import ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"

        mock_proc = MagicMock()
        mock_proc.cmdline.side_effect = psutil.NoSuchProcess(pid=123)

        with (
            patch("psutil.pid_exists", return_value=True),
            patch("psutil.Process", return_value=mock_proc),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            result = handle_existing_instance(
                _sample_meta(pid=123),
                "http://127.0.0.1:5000",
                lock_path,
                meta_path,
                _sample_meta(),
            )

        assert result == ExistingInstanceAction.CONTINUE_STARTUP
        assert mock_retry.call_args[0][0] == "race_death"


# ---------------------------------------------------------------------------
# _retry_lock_or_fail() unit tests
# ---------------------------------------------------------------------------


class TestRetryLockOrFail:
    def test_success_on_first_retry(self, tmp_path: Path) -> None:
        from job_finder.__main__ import _retry_lock_or_fail
        from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"

        successful_result = AcquireResult(acquired=True)
        with (
            patch("time.sleep"),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=successful_result,
            ),
        ):
            result = _retry_lock_or_fail("dead_pid", lock_path, meta_path, _sample_meta())

        assert result == ExistingInstanceAction.CONTINUE_STARTUP

    def test_exhaustion_returns_exit_failure(self, tmp_path: Path, capsys) -> None:
        from job_finder.__main__ import _retry_lock_or_fail
        from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction

        lock_path = tmp_path / "server.lock"
        meta_path = tmp_path / "server.json"

        failed_result = AcquireResult(acquired=False, existing=None)
        with (
            patch("time.sleep"),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=failed_result,
            ),
        ):
            result = _retry_lock_or_fail("no_metadata", lock_path, meta_path, _sample_meta())

        assert result == ExistingInstanceAction.EXIT_FAILURE
        captured = capsys.readouterr()
        assert "contention unresolved" in captured.err
        assert "no_metadata" in captured.err


# ---------------------------------------------------------------------------
# Integration: main() decision paths (exit codes)
# ---------------------------------------------------------------------------


class TestMainDecisionPaths:
    """Verify that main() wires probe → psutil → lock → exits correctly."""

    def _patch_config(self, monkeypatch, bind_host="127.0.0.1", port=5000):
        """Patch load_config to return a minimal server config."""
        monkeypatch.setattr(
            "job_finder.config.load_config",
            lambda **kw: {"server": {"host": bind_host, "port": port, "debug": False}},
        )

    # --- Branch 1a: probe returns jc-dict → exit 0, browser opened ---

    def test_probe_success_exits_0(self, monkeypatch, no_browser) -> None:
        """Probe returns a JC dict → sys.exit(0)."""
        self._patch_config(monkeypatch)

        probe_data = {"app": "job-cannon", "version": "1.0", "pid": 1}
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=probe_data),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 0

    # --- Branch 2a: probe=None, port busy, listener is jc → exit 0 ---

    def test_psutil_jc_listener_exits_0(self, monkeypatch, no_browser) -> None:
        """Port busy, listener is Job Cannon (pre-plan) → exit 0."""
        self._patch_config(monkeypatch)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 42),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 0

    # --- Branch 2b: probe=None, port busy, listener is foreign → exit 1 ---

    def test_foreign_port_owner_exits_1(self, monkeypatch, capsys, no_browser) -> None:
        """Port busy, foreign process → exit 1 with clear diagnostic."""
        self._patch_config(monkeypatch)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, "python -m http.server 5000", 77),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "http.server" in captured.err

    # --- Branch 2c: listener on wrong interface → treat as foreign ---

    def test_wrong_interface_listener_exits_1(self, monkeypatch, capsys, no_browser) -> None:
        """Listener on non-loopback interface when probing localhost → foreign."""
        self._patch_config(monkeypatch)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            # _listener_looks_like_jc interface filter returns (False, None, None)
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, None, None),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "port" in captured.err.lower()

    # --- Branch 3a: lock free → CONTINUE_STARTUP → flask starts ---

    def test_lock_free_continues_startup(self, monkeypatch, tmp_path: Path) -> None:
        """When the lock is free, main() proceeds to create_app."""
        self._patch_config(monkeypatch)
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.web._pidfile import AcquireResult

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=AcquireResult(acquired=True),
            ),
            patch("job_finder.web.create_app") as mock_create_app,
        ):
            mock_app = MagicMock()
            mock_create_app.return_value = mock_app
            mock_app.run.side_effect = KeyboardInterrupt  # stop the server loop

            with patch("job_finder.web._process_lifecycle.install_kill_on_exit"):
                with patch("job_finder.web._runtime.runtime_shutdown"):
                    try:
                        from job_finder.__main__ import main

                        main()
                    except (KeyboardInterrupt, SystemExit):
                        pass

        mock_create_app.assert_called_once()

    # --- Branch 3f: lock busy, confirmed live jc → EXIT_SUCCESS → exit 0 ---

    def test_lock_busy_live_jc_exits_0(self, monkeypatch, no_browser, tmp_path) -> None:
        """Lock held by confirmed live JC instance → exit 0."""
        self._patch_config(monkeypatch)
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction

        existing = _sample_meta(pid=os.getpid())
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=AcquireResult(acquired=False, existing=existing),
            ),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.EXIT_SUCCESS,
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 0

    # --- Branch 3h: corrupt metadata → EXIT_FAILURE → exit 1 ---

    def test_lock_busy_corrupt_meta_exits_1(self, monkeypatch, no_browser, tmp_path) -> None:
        """Corrupt metadata → exit 1."""
        self._patch_config(monkeypatch)
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=AcquireResult(acquired=False, existing=None),
            ),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.EXIT_FAILURE,
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 1

    # --- Branch 3j: wildcard bind → probe uses 127.0.0.1 → exit 0 ---

    def test_wildcard_bind_probes_localhost(self, monkeypatch, no_browser) -> None:
        """0.0.0.0 bind host → probe url uses 127.0.0.1."""
        self._patch_config(monkeypatch, bind_host="0.0.0.0", port=5000)

        probe_data = {"app": "job-cannon", "pid": 1, "version": "1.0"}
        probe_calls: list[str] = []

        def _mock_probe(url, **kw):
            probe_calls.append(url)
            return probe_data

        with (
            patch("job_finder.__main__.probe_existing_jc", side_effect=_mock_probe),
            pytest.raises(SystemExit) as exc_info,
        ):
            from job_finder.__main__ import main

            main()

        assert exc_info.value.code == 0
        # The URL probed must use 127.0.0.1, not 0.0.0.0
        assert probe_calls, "probe_existing_jc should have been called"
        assert "0.0.0.0" not in probe_calls[0]
        assert "127.0.0.1" in probe_calls[0]

    # --- Mid-startup: no metadata yet → retry then success (branch 3i) ---

    def test_mid_startup_retry_then_success(self, monkeypatch, no_browser, tmp_path) -> None:
        """Lock held, no metadata (holder mid-startup) → retry → CONTINUE_STARTUP."""
        self._patch_config(monkeypatch)
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.web._pidfile import AcquireResult, ExistingInstanceAction

        # First acquire fails with no metadata
        contention_result = AcquireResult(acquired=False, existing=None)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch(
                "job_finder.web._pidfile.acquire_pidfile",
                return_value=contention_result,
            ),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ),
            patch("job_finder.web.create_app") as mock_create_app,
        ):
            mock_app = MagicMock()
            mock_create_app.return_value = mock_app
            mock_app.run.side_effect = KeyboardInterrupt

            with patch("job_finder.web._process_lifecycle.install_kill_on_exit"):
                with patch("job_finder.web._runtime.runtime_shutdown"):
                    try:
                        from job_finder.__main__ import main

                        main()
                    except (KeyboardInterrupt, SystemExit):
                        pass

        # CONTINUE_STARTUP: create_app was called
        mock_create_app.assert_called_once()
