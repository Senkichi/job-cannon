"""Tests for the APScheduler integration.

Tests:
- init_scheduler is skipped when TESTING=True (no scheduler started)
- init_scheduler is skipped when WERKZEUG_RUN_MAIN=true (reloader child process)
- init_scheduler guards against double initialization
- Scheduler job is registered with 30-minute interval
- trigger_sync returns a summary dict
- reset_scheduler clears the singleton
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(testing=False, db_path=None):
    """Create a minimal Flask app dict-like mock for scheduler tests."""
    if db_path is None:
        db_path = ":memory:"

    app = MagicMock()
    app.config = {
        "TESTING": testing,
        "JF_CONFIG": {
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
            },
            "profile": {},
            "scoring": {"min_score_threshold": 40, "weights": {}},
            "db": {"path": db_path},
        },
        "DB_PATH": db_path,
    }
    app.config.get = lambda key, default=None: app.config.get(key, default) if isinstance(app.config, dict) else default

    # Make app.config behave like a real dict for .get() calls
    real_config = app.config
    app.config = real_config

    # app_context() needs to work as a context manager
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    app.app_context.return_value = ctx

    return app


# ---------------------------------------------------------------------------
# Fixture: reset scheduler singleton between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_scheduler_state():
    """Always reset the scheduler singleton before and after each test."""
    from job_finder.web.scheduler import reset_scheduler
    reset_scheduler()
    yield
    reset_scheduler()


# ---------------------------------------------------------------------------
# Test: TESTING=True skips scheduler
# ---------------------------------------------------------------------------

class TestSchedulerTesting:
    def test_scheduler_not_started_in_test_mode(self):
        """init_scheduler skips when TESTING=True."""
        from job_finder.web.scheduler import init_scheduler, get_scheduler

        app = MagicMock()
        app.config = {"TESTING": True}

        init_scheduler(app)

        assert get_scheduler() is None

    def test_scheduler_starts_when_not_testing(self):
        """init_scheduler starts the scheduler when TESTING is False/absent."""
        from job_finder.web.scheduler import init_scheduler, get_scheduler, reset_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched

            init_scheduler(app)

            mock_sched.start.assert_called_once()
            assert get_scheduler() is mock_sched


# ---------------------------------------------------------------------------
# Test: Werkzeug reloader guard
# ---------------------------------------------------------------------------

class TestReloaderGuard:
    def test_scheduler_skipped_in_reloader_child(self, monkeypatch):
        """init_scheduler skips when WERKZEUG_RUN_MAIN=true."""
        monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")

        from job_finder.web.scheduler import init_scheduler, get_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        init_scheduler(app)

        assert get_scheduler() is None

    def test_scheduler_starts_without_reloader_env(self, monkeypatch):
        """init_scheduler starts when WERKZEUG_RUN_MAIN is not set."""
        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)

        from job_finder.web.scheduler import init_scheduler, get_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched

            init_scheduler(app)

            assert get_scheduler() is mock_sched


# ---------------------------------------------------------------------------
# Test: Double-init guard
# ---------------------------------------------------------------------------

class TestDoubleInitGuard:
    def test_init_called_twice_only_starts_once(self):
        """Calling init_scheduler twice does not start the scheduler twice."""
        from job_finder.web.scheduler import init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched

            init_scheduler(app)
            init_scheduler(app)  # second call -- should be a no-op

            # BackgroundScheduler() should only be constructed once
            assert MockScheduler.call_count == 1
            assert mock_sched.start.call_count == 1


# ---------------------------------------------------------------------------
# Test: 30-minute interval job registration
# ---------------------------------------------------------------------------

class TestSchedulerJobConfig:
    def test_ingestion_job_registered_with_30min_interval(self):
        """The scheduler registers an ingestion job with a 30-minute interval."""
        from job_finder.web.scheduler import init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler, \
             patch("job_finder.web.scheduler.IntervalTrigger") as MockTrigger:

            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched
            mock_trigger = MagicMock()
            MockTrigger.return_value = mock_trigger

            init_scheduler(app)

            # Verify IntervalTrigger was created with minutes=30 (at least once;
            # pipeline_detection also uses IntervalTrigger(minutes=30))
            MockTrigger.assert_any_call(minutes=30)

            # Verify add_job was called at least 3 times (ingestion + stale + pipeline_detection)
            assert mock_sched.add_job.call_count >= 3

            # First call should be the ingestion_poll job
            first_call = mock_sched.add_job.call_args_list[0]
            kwargs = first_call.kwargs if first_call.kwargs else first_call[1]
            assert kwargs.get("max_instances") == 1
            assert kwargs.get("coalesce") is True
            assert kwargs.get("id") == "ingestion_poll"

    def test_ingestion_job_has_replace_existing(self):
        """The scheduler job uses replace_existing=True to prevent duplicate jobs."""
        from job_finder.web.scheduler import init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched

            init_scheduler(app)

            add_job_kwargs = mock_sched.add_job.call_args
            kwargs = add_job_kwargs.kwargs if add_job_kwargs.kwargs else add_job_kwargs[1]
            assert kwargs.get("replace_existing") is True


# ---------------------------------------------------------------------------
# Test: trigger_sync
# ---------------------------------------------------------------------------

class TestTriggerSync:
    def test_trigger_sync_returns_summary_dict(self):
        """trigger_sync returns the summary dict from run_ingestion."""
        from job_finder.web.scheduler import trigger_sync

        mock_summary = {
            "gmail_fetched": 5,
            "gmail_errors": [],
            "serpapi_fetched": 3,
            "serpapi_errors": [],
            "jobs_new": 2,
            "jobs_updated": 6,
            "jobs_scored": 8,
            "job_errors": [],
            "duration_seconds": 1.5,
        }

        app = MagicMock()
        app.config = {"JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.pipeline_runner.run_ingestion") as mock_run:
            mock_run.return_value = mock_summary

            result = trigger_sync(app)

        assert result == mock_summary

    def test_trigger_sync_returns_error_dict_on_exception(self):
        """trigger_sync returns an error dict if run_ingestion raises."""
        from job_finder.web.scheduler import trigger_sync

        app = MagicMock()
        app.config = {"JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.pipeline_runner.run_ingestion") as mock_run:
            mock_run.side_effect = Exception("Database locked")

            result = trigger_sync(app)

        assert "error" in result
        assert "Database locked" in result["error"]
        assert result["jobs_new"] == 0


# ---------------------------------------------------------------------------
# ATS scan scheduler tests (relocated from test_scoring.py, Phase 24)
# ---------------------------------------------------------------------------


class TestSchedulerAtsScan:
    """Verify ATS scan and slug probe jobs are registered in APScheduler."""

    def test_scheduler_registers_ats_scan_job(self):
        """init_scheduler registers 'ats_scan' CronTrigger job (Mon/Wed hour=7)."""
        from job_finder.web.scheduler import reset_scheduler
        from unittest.mock import MagicMock, patch

        # Reset so we can test init
        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        # Patch env guard so reloader check is bypassed
        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                from job_finder.web.scheduler import init_scheduler
                init_scheduler(mock_app)

        # Check that add_job was called with id='ats_scan'
        job_ids = [
            call[1].get("id") or call[0][1] if len(call[0]) > 1 else call[1].get("id")
            for call in mock_sched.add_job.call_args_list
        ]
        # Extract id from keyword args
        job_ids_kw = [call[1].get("id") for call in mock_sched.add_job.call_args_list]
        assert "ats_scan" in job_ids_kw

    def test_scheduler_registers_ats_slug_probe_job(self):
        """init_scheduler registers 'ats_slug_probe' CronTrigger job (Mon/Wed hour=7 min=30)."""
        from job_finder.web.scheduler import reset_scheduler
        from unittest.mock import MagicMock, patch

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                from job_finder.web.scheduler import init_scheduler
                init_scheduler(mock_app)

        job_ids_kw = [call[1].get("id") for call in mock_sched.add_job.call_args_list]
        assert "ats_slug_probe" in job_ids_kw


# ---------------------------------------------------------------------------
# Expiry check scheduler tests (Phase 31)
# ---------------------------------------------------------------------------


class TestSchedulerExpiryCheck:
    """Verify expiry check job is registered in APScheduler."""

    def test_scheduler_registers_expiry_check_job(self):
        """init_scheduler registers 'expiry_check' CronTrigger job (hour=2, minute=30)."""
        from job_finder.web.scheduler import reset_scheduler
        from unittest.mock import MagicMock, patch

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                from job_finder.web.scheduler import init_scheduler
                init_scheduler(mock_app)

        job_ids_kw = [call[1].get("id") for call in mock_sched.add_job.call_args_list]
        assert "expiry_check" in job_ids_kw
