"""Tests for ``job_finder/web/_takeover.py`` — the unified pre-bind authority.

PR1 is behaviour-neutral: ``mode="bare"`` reproduces the legacy focus-and-exit
Steps 1-2 and ``mode="serve"`` reproduces the legacy ``free_jc_port`` reclaim.
These tests pin both modes' branches so the later (PR2+) convergence onto a
single health-gated takeover is a deliberate, visible behaviour change.
"""

from __future__ import annotations

from unittest.mock import patch

from job_finder.web._takeover import TakeoverAction, claim_or_takeover

_URL = "http://127.0.0.1:5000"


# ---------------------------------------------------------------------------
# bare mode — legacy Steps 1-2 (focus-and-exit, no reaping)
# ---------------------------------------------------------------------------
class TestBareMode:
    def test_healthy_instance_focuses_and_exits_success(self):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value={"app": "job-cannon"}),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_called_once()

    def test_healthy_instance_respects_no_browser(self):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value={"app": "job-cannon"}),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare", no_browser=True)
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_not_called()

    def test_pre_upgrade_jc_listener_focuses_and_exits_success(self):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(True, "uv run job-cannon", 1234),
            ),
            patch("job_finder.web._takeover.webbrowser.open") as mock_open,
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_SUCCESS
        mock_open.assert_called_once()

    def test_foreign_listener_exits_failure_with_cmdline(self, capsys):
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=True),
            patch(
                "job_finder.__main__._listener_looks_like_jc",
                return_value=(False, "python -m http.server", 9999),
            ),
        ):
            action = claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        assert action == TakeoverAction.EXIT_FAILURE
        err = capsys.readouterr().err
        assert "http.server" in err
        assert "different port" in err

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

    def test_bare_never_reaps(self):
        """PR1 contract: bare mode must NOT call free_jc_port (no reaping yet)."""
        with (
            patch("job_finder.__main__.probe_existing_jc", return_value=None),
            patch("job_finder.__main__._port_is_listening", return_value=False),
            patch("job_finder.web.supervisor.free_jc_port") as mock_reap,
        ):
            claim_or_takeover("127.0.0.1", 5000, _URL, mode="bare")
        mock_reap.assert_not_called()


# ---------------------------------------------------------------------------
# serve mode — legacy Step 0 (free_jc_port reclaim)
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
