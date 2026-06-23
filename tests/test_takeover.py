"""Tests for ``job_finder/web/_takeover.py`` — the unified pre-bind authority.

The bare path is a health-gated takeover: a *serving* instance (current or
pre-``/__jc_health``) is focused-and-deferred-to; only a **wedged** Job Cannon
orphan (socket held, no HTTP response) is reaped. ``serve`` retains the
unconditional ``free_jc_port`` reclaim until PR5.
"""

from __future__ import annotations

from unittest.mock import patch

from job_finder.web._takeover import TakeoverAction, claim_or_takeover

_URL = "http://127.0.0.1:5000"


# ---------------------------------------------------------------------------
# bare mode — health-gated takeover
# ---------------------------------------------------------------------------
class TestBareMode:
    def test_healthy_instance_focuses_and_exits_success(self):
        """A current instance answering /__jc_health is focused, never reaped."""
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value={"app": "job-cannon"}),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
            patch("job_finder.web.supervisor.free_jc_port") as mock_reap,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_called_once()
        mock_reap.assert_not_called()  # a serving instance is never killed

    def test_healthy_instance_respects_no_browser(self):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value={"app": "job-cannon"}),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare", no_browser=True)
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_not_called()

    def test_pre_upgrade_serving_instance_focuses_and_defers(self):
        """A JC listener with no /__jc_health but live HTTP is a pre-upgrade
        instance — focused-and-deferred-to, NOT reaped."""
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            patch("job_finder.web._takeover._port_serves_http", return_value=True),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
            patch("job_finder.web.supervisor.free_jc_port") as mock_reap,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_called_once()
        mock_reap.assert_not_called()

    def test_wedged_orphan_is_reaped_then_proceeds(self):
        """THE CORE FIX: a JC listener holding the socket but answering no HTTP
        is reaped, and the launch proceeds to bind — instead of deferring."""
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            patch("job_finder.web._takeover._port_serves_http", return_value=False),
            patch("job_finder.web.supervisor.free_jc_port", return_value=True) as mock_reap,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.PROCEED
        mock_reap.assert_called_once_with("127.0.0.1", 5000)

    def test_wedged_orphan_changed_identity_mid_reap_exits_failure(self, capsys):
        """Race: the listener stopped looking like JC between checks (free_jc_port
        returns False) → treat as occupied, exit non-zero."""
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            patch("job_finder.web._takeover._port_serves_http", return_value=False),
            patch("job_finder.web.supervisor.free_jc_port", return_value=False),
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_FAILURE
        assert "occupied" in capsys.readouterr().err

    def test_foreign_listener_exits_failure_with_cmdline(self, capsys):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, "python -m http.server", 9999),
            ),
            patch("job_finder.web.supervisor.free_jc_port") as mock_reap,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_FAILURE
        err = capsys.readouterr().err
        assert "http.server" in err
        assert "different port" in err
        mock_reap.assert_not_called()  # a foreign process is never killed

    def test_foreign_listener_without_cmdline_says_unknown(self, capsys):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, None, None),
            ),
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_FAILURE
        assert "unknown process" in capsys.readouterr().err

    def test_port_free_proceeds(self):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.PROCEED


# ---------------------------------------------------------------------------
# serve mode — legacy Step 0 (free_jc_port reclaim, retained until PR5)
# ---------------------------------------------------------------------------
class TestServeMode:
    def test_reclaim_succeeds_proceeds(self):
        with patch("job_finder.web.supervisor.free_jc_port", return_value=True) as mock_reap:
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="serve")
        assert action == TakeoverAction.PROCEED
        mock_reap.assert_called_once_with("127.0.0.1", 5000)

    def test_foreign_listener_refuses_and_exits_failure(self, capsys):
        with patch("job_finder.web.supervisor.free_jc_port", return_value=False):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="serve")
        assert action == TakeoverAction.EXIT_FAILURE
        err = capsys.readouterr().err
        assert "serve" in err
        assert "refusing to kill" in err

    def test_serve_does_not_run_focus_probes(self):
        """serve reclaims via free_jc_port and never consults the focus probes."""
        with (
            patch("job_finder.web.supervisor.free_jc_port", return_value=True),
            patch("job_finder.__main__.probe_existing_jc") as mock_probe,
        ):
            claim_or_takeover("127.0.0.1", 5000, _URL, mode="serve")
        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _port_serves_http — wedged-vs-live discriminator
# ---------------------------------------------------------------------------
class TestPortServesHttp:
    def test_any_response_is_alive(self):
        from job_finder.web import _takeover

        with patch("job_finder.web._takeover.requests.get", return_value=object()):
            assert _takeover._port_serves_http(_URL) is True

    def test_connection_error_is_wedged(self):
        import requests

        from job_finder.web import _takeover

        with patch(
            "job_finder.web._takeover.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ):
            assert _takeover._port_serves_http(_URL) is False

    def test_timeout_is_wedged(self):
        import requests

        from job_finder.web import _takeover

        with patch(
            "job_finder.web._takeover.requests.get",
            side_effect=requests.Timeout("read timed out"),
        ):
            assert _takeover._port_serves_http(_URL) is False
