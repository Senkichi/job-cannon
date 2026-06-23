"""Tests for the probe-then-psutil-then-lock decision tree in ``job_finder/__main__.py``.

Acceptance criteria — all 14+ branches in §8.5:
1.  probe returns jc dict → exit 0, browser opened
2.  probe=None AND listener cmdline is jc → exit 0 (pre-plan handoff via psutil fallback)
3.  probe=None AND listener cmdline is foreign → exit 1 with clear diagnostic
4.  probe=None AND listener on wrong interface → treated as foreign → exit 1
5.  probe=None AND port free AND lock free → continue startup (no exit)
6.  probe=None AND port free AND lock busy: dead-PID retry success → CONTINUE_STARTUP
7.  probe=None AND port free AND lock busy: dead-PID retry exhaustion → EXIT_FAILURE
8.  probe=None AND port free AND lock busy: alive different user (AccessDenied) → EXIT_FAILURE
9.  probe=None AND port free AND lock busy: alive different cmdline (PID reuse) → retry
10. probe=None AND port free AND lock busy: alive matching cmdline → EXIT_SUCCESS
11. probe=None AND port free AND lock busy: missing metadata → retry
12. probe=None AND port free AND lock busy: corrupt metadata → EXIT_FAILURE
13. probe=None AND port free AND lock busy: mid-startup no metadata yet → retry then success
14. probe=None AND wildcard bind host (0.0.0.0) → probe uses 127.0.0.1 → exit 0
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

import job_finder.__main__ as main_mod
from job_finder.web._pidfile import ExistingInstanceAction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_URL = "http://127.0.0.1:5000"
_DEFAULT_LOCK = Path("/tmp/fake/server.lock")
_DEFAULT_META = Path("/tmp/fake/server.json")
_DEFAULT_METADATA = {"pid": os.getpid(), "url": _DEFAULT_URL}


def _make_existing(pid=None, url=_DEFAULT_URL, **extra):
    """Build a fake existing_meta dict."""
    d = {"url": url}
    if pid is not None:
        d["pid"] = pid
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# probe_existing_jc
# ---------------------------------------------------------------------------


class TestProbeExistingJc:
    def test_returns_dict_on_success(self):
        payload = {"app": "job-cannon", "version": "5.0.0", "pid": 1234}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = payload

        with patch("job_finder.__main__.requests.get", return_value=mock_resp):
            result = main_mod.probe_existing_jc("http://127.0.0.1:5000")

        assert result == payload

    def test_returns_none_on_connection_error(self):
        with patch(
            "job_finder.__main__.requests.get",
            side_effect=main_mod.requests.ConnectionError,
        ):
            assert main_mod.probe_existing_jc("http://127.0.0.1:5000") is None

    def test_returns_none_on_timeout(self):
        with patch(
            "job_finder.__main__.requests.get",
            side_effect=main_mod.requests.Timeout,
        ):
            assert main_mod.probe_existing_jc("http://127.0.0.1:5000") is None

    def test_returns_none_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("job_finder.__main__.requests.get", return_value=mock_resp):
            assert main_mod.probe_existing_jc("http://127.0.0.1:5000") is None

    def test_returns_none_if_not_jc(self):
        """Wrong identity marker → not Job Cannon."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"app": "something-else"}
        with patch("job_finder.__main__.requests.get", return_value=mock_resp):
            assert main_mod.probe_existing_jc("http://127.0.0.1:5000") is None

    def test_returns_none_on_non_json_body(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch("job_finder.__main__.requests.get", return_value=mock_resp):
            assert main_mod.probe_existing_jc("http://127.0.0.1:5000") is None

    def test_probes_correct_path(self):
        """URL must end with /__jc_health (trailing-slash stripped from base)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"app": "job-cannon"}

        with patch("job_finder.__main__.requests.get", return_value=mock_resp) as mock_get:
            main_mod.probe_existing_jc("http://127.0.0.1:5000/")

        mock_get.assert_called_once_with("http://127.0.0.1:5000/__jc_health", timeout=1.0)


# ---------------------------------------------------------------------------
# _listener_looks_like_jc
# ---------------------------------------------------------------------------


class TestListenerLooksLikeJc:
    def _make_conn(self, ip, port, pid, status=None):
        conn = MagicMock()
        conn.status = status or psutil.CONN_LISTEN
        conn.laddr = MagicMock()
        conn.laddr.ip = ip
        conn.laddr.port = port
        conn.pid = pid
        return conn

    def test_matches_job_cannon_cmdline(self):
        conn = self._make_conn("127.0.0.1", 5000, pid=1234)
        with (
            patch("job_finder.__main__.psutil.net_connections", return_value=[conn]),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["uv", "run", "job-cannon"]),
            ),
        ):
            looks, cmdline, pid = main_mod._listener_looks_like_jc("127.0.0.1", 5000)
        assert looks is True
        assert pid == 1234
        assert "job-cannon" in cmdline

    def test_matches_job_finder_cmdline(self):
        conn = self._make_conn("127.0.0.1", 5000, pid=5678)
        with (
            patch("job_finder.__main__.psutil.net_connections", return_value=[conn]),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["python", "-m", "job_finder"]),
            ),
        ):
            looks, cmdline, pid = main_mod._listener_looks_like_jc("127.0.0.1", 5000)
        assert looks is True

    def test_foreign_cmdline_returns_false(self):
        conn = self._make_conn("127.0.0.1", 5000, pid=9999)
        with (
            patch("job_finder.__main__.psutil.net_connections", return_value=[conn]),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["python", "-m", "http.server"]),
            ),
        ):
            looks, cmdline, pid = main_mod._listener_looks_like_jc("127.0.0.1", 5000)
        assert looks is False
        assert "http.server" in cmdline

    def test_wrong_interface_treated_as_foreign(self):
        """Listener on non-loopback IP when host=127.0.0.1 → skipped → returns False."""
        conn = self._make_conn("10.0.0.1", 5000, pid=1111)
        with patch("job_finder.__main__.psutil.net_connections", return_value=[conn]):
            looks, cmdline, pid = main_mod._listener_looks_like_jc("127.0.0.1", 5000)
        assert looks is False
        assert cmdline is None
        assert pid is None

    def test_no_listener_on_port(self):
        conn = self._make_conn("127.0.0.1", 9999, pid=1)  # different port
        with patch("job_finder.__main__.psutil.net_connections", return_value=[conn]):
            looks, cmdline, pid = main_mod._listener_looks_like_jc("127.0.0.1", 5000)
        assert looks is False


# ---------------------------------------------------------------------------
# handle_existing_instance
# ---------------------------------------------------------------------------


class TestHandleExistingInstance:
    """Tests for handle_existing_instance() decision tree."""

    def _call(self, existing_meta, default_url=_DEFAULT_URL):
        return main_mod.handle_existing_instance(
            existing_meta,
            default_url,
            _DEFAULT_LOCK,
            _DEFAULT_META,
            _DEFAULT_METADATA,
        )

    # Branch: alive matching cmdline → EXIT_SUCCESS
    def test_live_jc_instance_returns_exit_success(self, monkeypatch):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        existing = _make_existing(pid=1234)

        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["uv", "run", "job-cannon"]),
            ),
        ):
            action = self._call(existing)

        assert action == ExistingInstanceAction.EXIT_SUCCESS

    def test_live_jc_opens_browser(self, monkeypatch, capsys):
        monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)
        existing = _make_existing(pid=1234, url=_DEFAULT_URL)

        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["uv", "run", "job-cannon"]),
            ),
            patch("job_finder.__main__.webbrowser.open") as mock_open,
        ):
            action = self._call(existing)

        assert action == ExistingInstanceAction.EXIT_SUCCESS
        mock_open.assert_called_once_with(_DEFAULT_URL, new=2)

    # Branch: corrupt metadata (non-int pid) → EXIT_FAILURE
    def test_corrupt_pid_field_exits_failure(self, capsys):
        existing = {"pid": "not-an-int", "url": _DEFAULT_URL}
        action = self._call(existing)
        assert action == ExistingInstanceAction.EXIT_FAILURE
        assert "corrupt" in capsys.readouterr().err.lower()

    # Branch: missing metadata → retry
    def test_missing_metadata_triggers_retry(self):
        """existing_meta=None → _retry_lock_or_fail(reason='no_metadata')."""
        with patch(
            "job_finder.__main__._retry_lock_or_fail",
            return_value=ExistingInstanceAction.CONTINUE_STARTUP,
        ) as mock_retry:
            action = self._call(None)

        mock_retry.assert_called_once()
        call_kwargs = mock_retry.call_args
        assert call_kwargs.args[0] == "no_metadata"
        assert action == ExistingInstanceAction.CONTINUE_STARTUP

    # Branch: dead PID → retry
    def test_dead_pid_triggers_retry(self):
        existing = _make_existing(pid=999999999)
        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=False),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            action = self._call(existing)

        mock_retry.assert_called_once()
        assert mock_retry.call_args.args[0] == "dead_pid"
        assert action == ExistingInstanceAction.CONTINUE_STARTUP

    # Branch: AccessDenied (different user) → EXIT_FAILURE
    def test_access_denied_exits_failure(self, capsys):
        existing = _make_existing(pid=1234)
        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(
                    cmdline=MagicMock(side_effect=psutil.AccessDenied(pid=1234))
                ),
            ),
        ):
            action = self._call(existing)

        assert action == ExistingInstanceAction.EXIT_FAILURE
        err = capsys.readouterr().err
        assert "different user" in err.lower() or "access" in err.lower()

    # Branch: NoSuchProcess (race death) → retry
    def test_race_death_triggers_retry(self):
        existing = _make_existing(pid=1234)
        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(
                    cmdline=MagicMock(side_effect=psutil.NoSuchProcess(pid=1234))
                ),
            ),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            action = self._call(existing)

        assert mock_retry.call_args.args[0] == "race_death"

    # Branch: PID reuse (alive but foreign cmdline) → retry
    def test_pid_reuse_triggers_retry(self):
        existing = _make_existing(pid=1234)
        with (
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["python", "-m", "http.server"]),
            ),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ) as mock_retry,
        ):
            action = self._call(existing)

        assert mock_retry.call_args.args[0] == "pid_reuse"


# ---------------------------------------------------------------------------
# _retry_lock_or_fail
# ---------------------------------------------------------------------------


class TestRetryLockOrFail:
    def test_success_on_first_retry(self):
        """If acquire_pidfile succeeds on first retry → CONTINUE_STARTUP."""
        success_result = MagicMock()
        success_result.acquired = True

        with (
            patch("job_finder.__main__.acquire_pidfile", return_value=success_result),
            patch("job_finder.__main__.time.sleep"),
        ):
            action = main_mod._retry_lock_or_fail(
                "dead_pid", _DEFAULT_LOCK, _DEFAULT_META, _DEFAULT_METADATA
            )

        assert action == ExistingInstanceAction.CONTINUE_STARTUP

    def test_exhaustion_returns_exit_failure(self, capsys):
        """If all retries fail → EXIT_FAILURE with diagnostic message."""
        failed_result = MagicMock()
        failed_result.acquired = False

        with (
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch("job_finder.__main__.time.sleep"),
        ):
            action = main_mod._retry_lock_or_fail(
                "dead_pid", _DEFAULT_LOCK, _DEFAULT_META, _DEFAULT_METADATA
            )

        assert action == ExistingInstanceAction.EXIT_FAILURE
        err = capsys.readouterr().err
        assert "contention" in err.lower() or "lock" in err.lower()

    def test_retry_count_matches_constant(self):
        """Exactly _LOCK_RETRY_COUNT acquire attempts are made on exhaustion."""
        failed = MagicMock(acquired=False)
        with (
            patch("job_finder.__main__.acquire_pidfile", return_value=failed) as mock_acq,
            patch("job_finder.__main__.time.sleep"),
        ):
            main_mod._retry_lock_or_fail("test", _DEFAULT_LOCK, _DEFAULT_META, _DEFAULT_METADATA)

        assert mock_acq.call_count == main_mod._LOCK_RETRY_COUNT


# ---------------------------------------------------------------------------
# main() integration: full probe-then-psutil-then-lock sequence
# ---------------------------------------------------------------------------


class TestMainSequence:
    """End-to-end tests for the probe-then-psutil-then-lock logic inside main()."""

    # Branch 1: probe returns jc dict → exit 0
    def test_branch1_probe_returns_jc_dict_exits_0(self, monkeypatch):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        jc_payload = {"app": "job-cannon", "version": "5.0.0"}

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=jc_payload),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 0

    def test_branch1_probe_returns_jc_dict_opens_browser(self, monkeypatch):
        monkeypatch.delenv("JOB_CANNON_NO_BROWSER", raising=False)
        jc_payload = {"app": "job-cannon"}

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=jc_payload),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.__main__.webbrowser.open") as mock_open,
            pytest.raises(SystemExit),
        ):
            main_mod.main()

        mock_open.assert_called_once()

    # Branch 2: probe=None, JC listener still serving HTTP → exit 0 (pre-plan handoff)
    def test_branch2_psutil_jc_listener_exits_0(self, monkeypatch):
        """Load-bearing: a pre-/__jc_health JC instance that is still SERVING is
        deferred-to (focus + exit 0), not reaped. The health-gated takeover only
        reaps a wedged orphan (see test_branch2b)."""
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            # Still serving HTTP (no /__jc_health, but alive) → defer, do not reap.
            patch("job_finder.web._takeover._port_serves_http", return_value=True),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 0

    # Branch 2b: probe=None, JC listener WEDGED (no HTTP) → reap orphan, continue
    def test_branch2b_wedged_jc_orphan_reaped_then_continues(self, monkeypatch, tmp_path):
        """THE CORE FIX: bare launch reclaims a wedged JC orphan (socket held, no
        HTTP) and proceeds to bind, instead of deferring to it forever."""
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        fake_app = MagicMock()

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            patch("job_finder.web._takeover._port_serves_http", return_value=False),
            patch("job_finder.web.supervisor.free_jc_port", return_value=True) as mock_reap,
            patch(
                "job_finder.__main__.acquire_pidfile",
                return_value=MagicMock(acquired=True),
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            main_mod.main()

        mock_reap.assert_called_once()  # the wedged orphan was reaped
        fake_app.run.assert_called_once()  # and the launch proceeded to bind

    # Branch 3: probe=None, listener is foreign → exit 1
    def test_branch3_foreign_listener_exits_1(self, monkeypatch, capsys):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, "python -m http.server", 9999),
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "http.server" in err

    # Branch 4: listener on wrong interface → treated as foreign
    def test_branch4_wrong_interface_exits_1(self, monkeypatch, capsys):
        """Listener bound to non-loopback IP is treated as foreign."""
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            # _listener_looks_like_jc returns False because the interface check
            # skipped the non-loopback listener — treated as foreign.
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, None, None),
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 1

    # Branch 5: probe=None, port free, lock free → continue startup (no exit)
    def test_branch5_port_free_lock_free_continues(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        fake_app = MagicMock()
        fake_app.run = MagicMock()  # Don't actually start a server

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            # --terminal: tray mode is the default since Issue #40; this branch
            # asserts the lock logic reaches app.run, which is the terminal path.
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            # main() should NOT call sys.exit() — it should reach app.run().
            main_mod.main()

        fake_app.run.assert_called_once()

    # Branch 6: lock busy, dead-PID retry success → CONTINUE_STARTUP
    def test_branch6_dead_pid_retry_success_continues(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing={"pid": 999999999, "url": _DEFAULT_URL})
        fake_app = MagicMock()

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ),
            # --terminal: tray is the default since Issue #40; assert app.run.
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            main_mod.main()

        # Must continue to create_app and app.run.
        fake_app.run.assert_called_once()

    # Branch 7: lock busy, retry exhaustion → EXIT_FAILURE
    def test_branch7_retry_exhaustion_exits_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing=None)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.EXIT_FAILURE,
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 1

    # Branch 8: alive different user (AccessDenied) → EXIT_FAILURE with diagnostic
    def test_branch8_access_denied_exits_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing={"pid": 1234, "url": _DEFAULT_URL})

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(
                    cmdline=MagicMock(side_effect=psutil.AccessDenied(pid=1234))
                ),
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "different user" in err.lower() or "access" in err.lower()

    # Branch 9: alive different cmdline (PID reuse) → retry
    def test_branch9_pid_reuse_retries(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing={"pid": 1234, "url": _DEFAULT_URL})

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["python", "-m", "http.server"]),
            ),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.EXIT_FAILURE,
            ) as mock_retry,
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert mock_retry.call_args.args[0] == "pid_reuse"

    # Branch 10: alive matching cmdline → EXIT_SUCCESS
    def test_branch10_live_jc_exits_0(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing={"pid": 1234, "url": _DEFAULT_URL})

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch("job_finder.__main__.psutil.pid_exists", return_value=True),
            patch(
                "job_finder.__main__.psutil.Process",
                return_value=MagicMock(cmdline=lambda: ["uv", "run", "job-cannon"]),
            ),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 0

    # Branch 11: missing metadata → retry
    def test_branch11_missing_metadata_retries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing=None)

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch(
                "job_finder.__main__._retry_lock_or_fail",
                return_value=ExistingInstanceAction.EXIT_FAILURE,
            ) as mock_retry,
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit),
        ):
            main_mod.main()

        assert mock_retry.call_args.args[0] == "no_metadata"

    # Branch 12: corrupt metadata → EXIT_FAILURE
    def test_branch12_corrupt_metadata_exits_1(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(
            acquired=False, existing={"pid": "not-an-int", "url": _DEFAULT_URL}
        )

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value={}),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 1
        assert "corrupt" in capsys.readouterr().err.lower()

    # Branch 13: mid-startup no metadata yet → retry then success
    def test_branch13_mid_startup_retry_then_success(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        failed_result = MagicMock(acquired=False, existing=None)
        fake_app = MagicMock()

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.__main__.acquire_pidfile", return_value=failed_result),
            patch(
                "job_finder.__main__.handle_existing_instance",
                return_value=ExistingInstanceAction.CONTINUE_STARTUP,
            ),
            # --terminal: tray is the default since Issue #40; assert app.run.
            patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
            patch("job_finder.config.load_config", return_value={}),
            patch("job_finder.web.create_app", return_value=fake_app),
            patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
            patch("job_finder.web._runtime.runtime_shutdown"),
        ):
            main_mod.main()

        fake_app.run.assert_called_once()

    # Branch 14: wildcard bind host (0.0.0.0) → probe uses 127.0.0.1
    def test_branch14_wildcard_host_probes_loopback(self, monkeypatch):
        """When bind_host=0.0.0.0, probe_existing_jc must be called with 127.0.0.1."""
        monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
        cfg = {"server": {"host": "0.0.0.0", "port": 5000}}
        jc_payload = {"app": "job-cannon"}

        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=jc_payload) as mock_probe,
            patch("job_finder.__main__.sys.argv", ["job-cannon"]),
            patch("job_finder.config.load_config", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            main_mod.main()

        assert exc.value.code == 0
        # The probe URL must use 127.0.0.1, not 0.0.0.0.
        probe_url = mock_probe.call_args.args[0]
        assert "127.0.0.1" in probe_url
        assert "0.0.0.0" not in probe_url
