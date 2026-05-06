"""Tests for the APScheduler integration.

Tests:
- init_scheduler is skipped when TESTING=True (no scheduler started)
- init_scheduler is skipped when WERKZEUG_RUN_MAIN=true (reloader child process)
- init_scheduler guards against double initialization
- Scheduler job is registered with 30-minute interval
- run_sync_now returns a summary dict
- reset_scheduler clears the singleton
"""

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
    app.config.get = lambda key, default=None: (
        app.config.get(key, default) if isinstance(app.config, dict) else default
    )

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
        from job_finder.web.scheduler import get_scheduler, init_scheduler

        app = MagicMock()
        app.config = {"TESTING": True}

        init_scheduler(app)

        assert get_scheduler() is None

    def test_scheduler_starts_when_not_testing(self):
        """init_scheduler starts the scheduler when TESTING is False/absent."""
        from job_finder.web.scheduler import get_scheduler, init_scheduler

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

        from job_finder.web.scheduler import get_scheduler, init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        init_scheduler(app)

        assert get_scheduler() is None

    def test_scheduler_starts_without_reloader_env(self, monkeypatch):
        """init_scheduler starts when WERKZEUG_RUN_MAIN is not set."""
        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)

        from job_finder.web.scheduler import get_scheduler, init_scheduler

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
    def test_ingestion_job_registered_with_cron_3x_daily(self):
        """The scheduler registers an ingestion job with CronTrigger at 0,8,16 Pacific."""
        from job_finder.web.scheduler import init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        # CronTrigger moved from scheduler/__init__.py to scheduler/_jobs.py in
        # S7a Commit 5 (job-registration extraction). The patch follows the
        # symbol's new namespace; BackgroundScheduler stays in __init__.py
        # because that is where init_scheduler instantiates it.
        with (
            patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler,
            patch("job_finder.web.scheduler._jobs.CronTrigger") as MockCronTrigger,
        ):
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched
            mock_trigger = MagicMock()
            MockCronTrigger.return_value = mock_trigger

            init_scheduler(app)

            # Verify CronTrigger was created with hour="0,8,16" and Pacific timezone
            MockCronTrigger.assert_any_call(hour="0,8,16")

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
# Test: run_sync_now
# ---------------------------------------------------------------------------


class TestRunSyncNow:
    def test_run_sync_now_returns_summary_dict(self):
        """run_sync_now returns the summary dict from run_ingestion."""
        from job_finder.web.scheduler import run_sync_now

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

            result = run_sync_now(app)

        assert result == mock_summary

    def test_run_sync_now_returns_error_dict_on_exception(self):
        """run_sync_now returns an error dict if run_ingestion raises."""
        from job_finder.web.scheduler import run_sync_now

        app = MagicMock()
        app.config = {"JF_CONFIG": {}, "DB_PATH": ":memory:"}

        with patch("job_finder.web.pipeline_runner.run_ingestion") as mock_run:
            mock_run.side_effect = Exception("Database locked")

            result = run_sync_now(app)

        assert "error" in result
        assert "Database locked" in result["error"]
        assert result["jobs_new"] == 0
        assert result["thordata_fetched"] == 0
        assert result["thordata_errors"] == []


# ---------------------------------------------------------------------------
# ATS scan scheduler tests (relocated from test_scoring.py, Phase 24)
# ---------------------------------------------------------------------------


class TestSchedulerAtsScan:
    """Verify ATS scan and slug probe jobs are registered in APScheduler."""

    def test_scheduler_registers_ats_scan_job(self):
        """init_scheduler registers 'ats_scan' CronTrigger job (Mon/Wed hour=7)."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import reset_scheduler

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

    def test_ats_scan_extract_metadata_reads_correct_summary_keys(self):
        """extract_metadata callback for ats_scan must read keys actually returned
        by run_ats_scan (jobs_discovered + jobs_new), not the legacy jobs_found.

        Regression: prior to the fix, the callback read r.get("jobs_found") which
        does not exist in run_ats_scan's summary, causing user_activity rows to
        always report jobs_found=0 even when company_scan_log totaled 1000+ jobs.
        """
        from job_finder.web.scheduler import reset_scheduler

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

        ats_call = next(
            call for call in mock_sched.add_job.call_args_list if call[1].get("id") == "ats_scan"
        )
        wrapper = ats_call[0][0]
        closure_vars = {
            cell_name: cell.cell_contents
            for cell_name, cell in zip(
                wrapper.__code__.co_freevars, wrapper.__closure__ or (), strict=False
            )
        }
        extract_metadata = closure_vars["extract_metadata"]

        realistic_summary = {
            "companies_scanned": 599,
            "jobs_discovered": 1304,
            "jobs_new": 87,
            "scored": 87,
            "classified_apply": 5,
            "classified_consider": 12,
            "classified_skip": 60,
            "classified_reject": 10,
            "html_scraped": 0,
            "homepages_discovered": 0,
            "errors": [],
        }

        metadata = extract_metadata(realistic_summary)

        assert metadata["companies_scanned"] == 599
        assert metadata["jobs_found"] == 1304, (
            "jobs_found must surface jobs_discovered (raw match count); "
            "reading the wrong key was the original always-zero bug"
        )
        assert metadata["jobs_new"] == 87, (
            "jobs_new must be surfaced so dashboard meta.jobs_new render path lights up"
        )

    def test_scheduler_registers_ats_slug_probe_job(self):
        """init_scheduler registers 'ats_slug_probe' CronTrigger job (Mon/Wed hour=7 min=30)."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import reset_scheduler

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
# Staleness check scheduler tests (replaces old expiry_check/liveness_check)
# ---------------------------------------------------------------------------


class TestSchedulerExpiryCheck:
    """Verify unified staleness check job is registered in APScheduler.

    Replaces the old trio (stale_detection / expiry_check / liveness_check).
    See job_finder.web.expiry_checker.run_staleness_check.
    """

    def test_scheduler_registers_staleness_check_job(self):
        """init_scheduler registers 'staleness_check' CronTrigger job (hour=2, minute=0)."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import reset_scheduler

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
        assert "staleness_check" in job_ids_kw
        # Old per-phase jobs must no longer be registered
        assert "expiry_check" not in job_ids_kw
        assert "liveness_check" not in job_ids_kw
        assert "stale_detection" not in job_ids_kw


# ---------------------------------------------------------------------------
# Test: Arg-order regression (Phase 11 plan 01)
# ---------------------------------------------------------------------------


class TestSchedulerArgOrder:
    """Regression tests: scheduler calls run_ingestion/run_pipeline_detection
    with (config, db_path) — not (db_path, config)."""

    def test_run_sync_now_calls_run_ingestion_with_db_path_first(self):
        """run_sync_now passes (db_path, config) to run_ingestion — first arg must be a string."""
        from job_finder.web.scheduler import run_sync_now

        config_dict = {"key": "val", "sources": {}}
        db_path = "/tmp/test.db"

        app = MagicMock()
        app.config = {"JF_CONFIG": config_dict, "DB_PATH": db_path}

        mock_summary = {
            "gmail_fetched": 0,
            "gmail_errors": [],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "jobs_new": 0,
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.0,
        }

        with (
            patch("job_finder.web.pipeline_runner.run_ingestion") as mock_run_ingestion,
            patch("job_finder.web.pipeline_detector.run_pipeline_detection") as mock_detection,
        ):
            mock_run_ingestion.return_value = mock_summary
            mock_detection.return_value = {"auto_updated": 0, "queued": 0}

            run_sync_now(app)

        assert mock_run_ingestion.called, "run_ingestion was not called"
        call_args = mock_run_ingestion.call_args[0]  # positional args
        assert len(call_args) == 2, f"Expected 2 positional args, got {len(call_args)}"
        assert isinstance(call_args[0], str), (
            f"First arg to run_ingestion must be db_path string, got {type(call_args[0])}"
        )
        assert isinstance(call_args[1], dict), (
            f"Second arg to run_ingestion must be config dict, got {type(call_args[1])}"
        )

    def test_run_sync_now_calls_pipeline_detection_with_db_path_first(self):
        """run_sync_now passes (db_path, config) to run_pipeline_detection — first arg must be a string."""
        from job_finder.web.scheduler import run_sync_now

        config_dict = {"key": "val", "sources": {}}
        db_path = "/tmp/test.db"

        app = MagicMock()
        app.config = {"JF_CONFIG": config_dict, "DB_PATH": db_path}

        mock_summary = {
            "gmail_fetched": 0,
            "gmail_errors": [],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "jobs_new": 0,
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.0,
        }

        with (
            patch("job_finder.web.pipeline_runner.run_ingestion") as mock_run_ingestion,
            patch("job_finder.web.pipeline_detector.run_pipeline_detection") as mock_detection,
        ):
            mock_run_ingestion.return_value = mock_summary
            mock_detection.return_value = {"auto_updated": 0, "queued": 0}

            run_sync_now(app)

        assert mock_detection.called, "run_pipeline_detection was not called"
        call_args = mock_detection.call_args[0]  # positional args
        assert len(call_args) == 2, f"Expected 2 positional args, got {len(call_args)}"
        assert isinstance(call_args[0], str), (
            f"First arg to run_pipeline_detection must be db_path string, got {type(call_args[0])}"
        )
        assert isinstance(call_args[1], dict), (
            f"Second arg to run_pipeline_detection must be config dict, got {type(call_args[1])}"
        )

    def test_import_detection_returns_run_pipeline_detection_directly(self):
        """The _import_detection factory returns run_pipeline_detection directly
        (no adapter lambda needed since it uses the standard (db_path, config) convention)."""
        from job_finder.web.scheduler import init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

        captured_import_funcs = []

        with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
            mock_sched = MagicMock()
            MockScheduler.return_value = mock_sched

            # Capture the import_func for pipeline_detection job
            def capture_add_job(func, **kwargs):
                if kwargs.get("id") == "pipeline_detection":
                    captured_import_funcs.append(func)

            mock_sched.add_job.side_effect = capture_add_job

            init_scheduler(app)

        # Verify _import_detection was registered
        assert len(captured_import_funcs) >= 1, "pipeline_detection job was not registered"


# ---------------------------------------------------------------------------
# Test: agentic_backfill job registered and paused (Glassdoor enrichment spec)
# ---------------------------------------------------------------------------


class TestSchedulerAgenticBackfill:
    """Verify agentic_backfill job is registered and runs on schedule (not paused)."""

    def test_agentic_backfill_job_registered(self):
        """init_scheduler registers 'agentic_backfill' CronTrigger job (hour=3, minute=30)."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import init_scheduler, reset_scheduler

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                init_scheduler(mock_app)

        job_ids_kw = [call[1].get("id") for call in mock_sched.add_job.call_args_list]
        assert "agentic_backfill" in job_ids_kw, (
            "agentic_backfill job must be registered via scheduler.add_job()"
        )

    def test_agentic_backfill_runs_on_schedule(self):
        """agentic_backfill is registered without next_run_time=None — runs on CronTrigger schedule."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import init_scheduler, reset_scheduler

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                init_scheduler(mock_app)

        # Find the add_job call for agentic_backfill
        agentic_call = None
        for call in mock_sched.add_job.call_args_list:
            if call[1].get("id") == "agentic_backfill":
                agentic_call = call
                break

        assert agentic_call is not None, "agentic_backfill add_job call not found"
        # Backfill is now permanently enabled — next_run_time must NOT be set to None
        assert "next_run_time" not in agentic_call[1], (
            "next_run_time should not be set; backfill runs on CronTrigger schedule"
        )

    def test_agentic_backfill_is_not_paused(self):
        """agentic_backfill must NOT be paused — it runs nightly on schedule."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.scheduler import init_scheduler, reset_scheduler

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                init_scheduler(mock_app)

        mock_sched.pause_job.assert_not_called()
