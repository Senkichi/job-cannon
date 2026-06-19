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


def _chain_test_app():
    """App mock for completion-chaining tests.

    Disables the chained predecessors' real work (staleness + careers_crawl
    guards) so invoking their wrappers short-circuits before any DB/network I/O,
    while still exercising the on_complete chainer (it fires on the guard path).
    DB_PATH points at a non-existent file — run_events.db_counters degrades to an
    error dict rather than raising, so no real DB is needed.
    """
    app = MagicMock()
    app.config = {
        "JF_CONFIG": {
            "staleness": {"enabled": False},
            "careers_crawl": {"enabled": False},
        },
        "DB_PATH": "/nonexistent/chain_test.db",
    }
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

    def test_scheduler_not_started_when_skip_scheduler_flag_set(self):
        """init_scheduler skips when SKIP_SCHEDULER=True (without propagating TESTING into job functions)."""
        from job_finder.web.scheduler import get_scheduler, init_scheduler

        app = MagicMock()
        app.config = {"TESTING": False, "SKIP_SCHEDULER": True}

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
        assert result["serpapi_fetched"] == 0
        assert result["serpapi_errors"] == []


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

    def test_staleness_extract_metadata_surfaces_batch_unparseable(self):
        """Issue #218 / IA-13: the staleness `extract_metadata` lambda must
        pass `batch_unparseable` through from the Phase-B summary. Without it,
        `reconcile_all_companies` computes the unparseable count, sums it, and
        the scheduler then drops it before it reaches the activity-tracker
        metadata — silently hiding the cohort with no liveness signal.
        """
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

        staleness_call = next(
            call
            for call in mock_sched.add_job.call_args_list
            if call[1].get("id") == "staleness_check"
        )
        wrapper = staleness_call[0][0]
        closure_vars = {
            cell_name: cell.cell_contents
            for cell_name, cell in zip(
                wrapper.__code__.co_freevars, wrapper.__closure__ or (), strict=False
            )
        }
        extract_metadata = closure_vars["extract_metadata"]

        realistic_summary = {
            "phase_b": {
                "companies_checked": 50,
                "companies_skipped": 5,
                "checked": 847,
                "live": 60,
                "expired": 11,
                "unparseable": 776,
            },
            "phase_a": {"stale_marked": 0, "stale_cleared": 0, "archived": 0},
            "phase_c": {"checked": 0, "live": 0, "archived": 0, "inconclusive": 0},
        }

        metadata = extract_metadata(realistic_summary)

        assert metadata["batch_unparseable"] == 776, (
            "batch_unparseable must surface from phase_b.unparseable; "
            "dropping it silently hides the IA-13 blind spot"
        )
        # Sibling keys must continue to pass through.
        assert metadata["batch_live"] == 60
        assert metadata["batch_expired"] == 11
        assert metadata["batch_companies_checked"] == 50


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
# Test: agentic_backfill is completion-chained off staleness_check (#229).
# It no longer has a cron trigger of its own; it is released as a one-shot
# DateTrigger when staleness finishes (on success OR failure).
# ---------------------------------------------------------------------------


class TestSchedulerAgenticBackfill:
    """Verify agentic_backfill is chained off staleness_check, not cron-scheduled."""

    def test_agentic_backfill_not_cron_registered_at_boot(self):
        """agentic_backfill is NOT registered with a cron trigger at boot — it is
        released only when staleness_check completes (#229 design 3a)."""
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
        # company_linkage is likewise chained (off careers_crawl), so neither
        # successor appears among the boot-time add_job ids.
        assert "agentic_backfill" not in job_ids_kw, (
            "agentic_backfill must be completion-chained, not cron-registered at boot"
        )
        assert "company_linkage" not in job_ids_kw, (
            "company_linkage must be completion-chained, not cron-registered at boot"
        )
        # staleness_check itself IS registered (the chain predecessor).
        assert "staleness_check" in job_ids_kw

    def test_agentic_backfill_released_on_staleness_completion(self):
        """When staleness_check's wrapper finishes, agentic_backfill is scheduled
        as a one-shot DateTrigger via the chainer."""

        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.date import DateTrigger

        from job_finder.web.scheduler._jobs import register_all_jobs

        app = _chain_test_app()
        sched = BackgroundScheduler()  # not started
        register_all_jobs(sched, app)

        # No agentic_backfill before staleness runs.
        assert sched.get_job("agentic_backfill") is None

        # Invoke the staleness wrapper directly. Its guard short-circuits the real
        # work (staleness disabled in our config) but the on_complete chainer must
        # STILL release the successor (finally / guard path).
        staleness_job = sched.get_job("staleness_check")
        assert staleness_job is not None
        staleness_job.func()

        released = sched.get_job("agentic_backfill")
        assert released is not None, "agentic_backfill not released after staleness completion"
        assert isinstance(released.trigger, DateTrigger), (
            "successor must be a one-shot DateTrigger, not a recurring trigger"
        )

    def test_company_linkage_released_on_careers_crawl_completion(self):
        """careers_crawl completion chains company_linkage as a one-shot."""
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.date import DateTrigger

        from job_finder.web.scheduler._jobs import register_all_jobs

        app = _chain_test_app()
        sched = BackgroundScheduler()
        register_all_jobs(sched, app)

        assert sched.get_job("company_linkage") is None

        careers_job = sched.get_job("careers_crawl")
        assert careers_job is not None
        careers_job.func()

        released = sched.get_job("company_linkage")
        assert released is not None, "company_linkage not released after careers_crawl completion"
        assert isinstance(released.trigger, DateTrigger)


# ---------------------------------------------------------------------------
# Pidfile lock — portalocker self-release on process death (2026-05-17 Fix 6)
# ---------------------------------------------------------------------------


class TestPidfileSelfRelease:
    """Validates that the new portalocker-based scheduler lock is released
    by the OS when the holding process dies — including the unclean-shutdown
    case (Windows force-kill, atexit not firing) that the psutil-based
    pidfile self-heal mishandled due to PID reuse.

    Uses subprocess.Popen (not multiprocessing) because Windows
    multiprocessing has a different lifecycle that can mask the lock-release
    behavior we care about for the real scheduler-vs-Flask race.
    """

    def test_pidfile_self_release_on_process_death(self, tmp_path):
        import subprocess
        import sys

        # Patch _acquire_scheduler_pidfile's resolved pidfile path by
        # pointing the child's "DB_PATH" at our tmp_path. The pidfile
        # lives at <db_path_dir>/logs/scheduler.pid.
        pidfile = tmp_path / "logs" / "scheduler.pid"
        db_path = str(tmp_path / "dummy.db")

        # Child: acquire the lock, write a sentinel, exit cleanly.
        # Holding briefly then exit gives the OS a chance to release.
        child_script = f"""
import sys
sys.path.insert(0, {repr(str(__import__("pathlib").Path(__file__).resolve().parent.parent))!s})
from unittest.mock import MagicMock
from job_finder.web.scheduler._pidfile import _acquire_scheduler_pidfile

mock_app = MagicMock()
mock_app.config = {{"DB_PATH": {db_path!r}}}
ok = _acquire_scheduler_pidfile(mock_app)
print("CHILD_ACQUIRED" if ok else "CHILD_FAILED", flush=True)
"""

        result = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert "CHILD_ACQUIRED" in result.stdout, result.stdout + result.stderr

        # OS should have released the lock when the child exited. The
        # pidfile may still exist (diagnostic-only), but the lock is gone.
        assert pidfile.exists(), "pidfile should remain on disk for diagnostics"

        # Parent: now attempt to acquire the same lock. Should succeed —
        # if the OS didn't release on child exit, this would deadlock
        # or return False (the test would fail either way).
        from unittest.mock import MagicMock as _MagicMock

        from job_finder.web.scheduler._pidfile import _acquire_scheduler_pidfile

        mock_app = _MagicMock()
        mock_app.config = {"DB_PATH": db_path}
        acquired = _acquire_scheduler_pidfile(mock_app)
        assert acquired is True, "OS should have released lock when child exited"


# ---------------------------------------------------------------------------
# Scheduled-sync metadata: per-portal counts must reach the activity feed.
# Closes drift item #3 from the 2026-05-27 round-4 handoff — before the fix,
# only the cron/admin-triggered path (action='scheduled_sync') silently
# dropped portal_<name>_fetched keys, leaving the dashboard unable to show
# USAJobs/Adzuna/Jooble counts.
# ---------------------------------------------------------------------------


class TestScheduledSyncPortalMetadata:
    """run_pipeline closure copies portal_<name>_fetched keys into log_activity metadata."""

    def _minimal_app(self, db_path: str):
        """Hand-rolled app mock — the file-level _make_app helper has a broken
        ``app.config.get = lambda`` assignment that only fires with testing=False
        (no other test exercises that path). Avoiding it by building exactly the
        surface ``run_pipeline`` touches: ``app.app_context()`` + ``app.config``
        as a real dict for ``get_config_snapshot``.
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield None

        app = MagicMock()
        app.app_context = _ctx
        app.config = {"DB_PATH": db_path, "JF_CONFIG": {}}
        return app

    def _capture_run_pipeline(self, app):
        """Register ingestion against a mock scheduler and return the captured closure."""
        from job_finder.web.scheduler._jobs import register_ingestion

        mock_scheduler = MagicMock()
        captured: dict = {}

        def _capture_add_job(func, **kwargs):
            if kwargs.get("id") == "ingestion_poll":
                captured["fn"] = func

        mock_scheduler.add_job.side_effect = _capture_add_job
        register_ingestion(mock_scheduler, app)
        assert "fn" in captured, "register_ingestion did not add an ingestion_poll job"
        return captured["fn"]

    def test_per_portal_keys_appear_in_scheduled_sync_metadata(self):
        app = self._minimal_app(":memory:")
        run_pipeline = self._capture_run_pipeline(app)

        synthetic_summary = {
            "jobs_new": 10,
            "gmail_fetched": 5,
            "serpapi_fetched": 0,
            "thordata_fetched": 0,
            "dataforseo_fetched": 0,
            "portal_search_fetched": 5,
            "portal_usajobs_fetched": 2,
            "portal_adzuna_fetched": 3,
            "portal_jooble_fetched": 0,  # explicit zero — fetcher ran but yielded none
        }

        captured_metadata: dict = {}

        def _capture_log_activity(db_path, action, metadata=None, **kwargs):
            captured_metadata.update(metadata or {})

        with (
            patch("job_finder.web.pipeline_runner.run_ingestion", return_value=synthetic_summary),
            patch(
                "job_finder.web.activity_tracker.log_activity",
                side_effect=_capture_log_activity,
            ),
        ):
            run_pipeline()

        # Per-portal keys must propagate so the dashboard can show them.
        assert captured_metadata["portal_search_fetched"] == 5
        assert captured_metadata["portal_usajobs_fetched"] == 2
        assert captured_metadata["portal_adzuna_fetched"] == 3
        # Explicit zero values are preserved (scoop loop does not filter on truthiness).
        assert captured_metadata["portal_jooble_fetched"] == 0
        # Status + jobs_new still present (regression guard for the rest of the dict).
        assert captured_metadata["status"] == "success"
        assert captured_metadata["jobs_new"] == 10

    def test_no_portal_keys_when_summary_has_none(self):
        """No portal_*_fetched keys → metadata only has the aggregate (0)."""
        app = self._minimal_app(":memory:")
        run_pipeline = self._capture_run_pipeline(app)

        synthetic_summary = {
            "jobs_new": 0,
            "gmail_fetched": 0,
            "serpapi_fetched": 0,
            "thordata_fetched": 0,
            "dataforseo_fetched": 0,
            "portal_search_fetched": 0,
        }

        captured_metadata: dict = {}

        def _capture_log_activity(db_path, action, metadata=None, **kwargs):
            captured_metadata.update(metadata or {})

        with (
            patch("job_finder.web.pipeline_runner.run_ingestion", return_value=synthetic_summary),
            patch(
                "job_finder.web.activity_tracker.log_activity",
                side_effect=_capture_log_activity,
            ),
        ):
            run_pipeline()

        portal_keys = [
            k for k in captured_metadata if k.startswith("portal_") and k.endswith("_fetched")
        ]
        # Only the aggregate — no per-portal noise.
        assert portal_keys == ["portal_search_fetched"]
        assert captured_metadata["portal_search_fetched"] == 0
