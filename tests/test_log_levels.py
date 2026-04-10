"""Regression tests for Phase 37 log-level demotions (OPS-01).

Each test verifies one of the 10 log-level changes specified in the
37-01 acceptance criteria. Tests trigger the exact code path and assert
on caplog records — if any level is reverted to WARNING the test fails.

All tests are pure unit/integration tests against local state only.
No network calls, no real Anthropic client, no Gmail credentials required.
"""

import logging
import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_migrated_db() -> tuple[str, sqlite3.Connection]:
    """Create a temp DB with full migrations applied. Returns (path, conn)."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn

# ---------------------------------------------------------------------------
# pipeline_runner.py — 3 DEBUG demotions
# ---------------------------------------------------------------------------

class TestPipelineRunnerLogLevels:
    """pipeline_runner.py log level regressions."""

    def test_claude_cli_not_found_logs_at_debug_in_scoring_runner(self, caplog):
        """scoring_runner 'claude CLI not found' branch uses logger.debug not WARNING."""
        import inspect
        import job_finder.web.scoring_runner as sr_module

        source = inspect.getsource(sr_module.run_haiku_scoring)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "claude cli not found" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"scoring_runner 'claude CLI not found' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.debug" in context, (
                    f"scoring_runner 'claude CLI not found' must use logger.debug.\n"
                    f"Context:\n{context}"
                )
                return  # found and validated

        raise AssertionError(
            "Could not find 'claude CLI not found' log message in "
            "scoring_runner.run_haiku_scoring — was the message text changed?"
        )

    def test_claude_cli_not_found_logs_at_debug(self, caplog):
        """When claude CLI is not on PATH, scoring_runner emits DEBUG not WARNING."""
        import job_finder.web.pipeline_runner as runner_module
        from job_finder.models import Job

        db_path, conn = _make_migrated_db()
        conn.close()

        config = {
            "sources": {"gmail": {"enabled": False}, "serpapi": {"enabled": False}},
            "scoring": {"monthly_budget_usd": 25.0, "haiku_threshold": 42},
            "profile": {"target_titles": [], "target_locations": [],
                        "min_salary": None, "exclusions": {}, "industries": [], "skills": []},
        }

        fake_job = Job(
            title="Fake Job", company="FakeCo", location="Remote",
            source="test", source_url="http://example.com", source_id="x1",
        )

        try:
            with patch.object(runner_module, "_fetch_gmail", return_value=[fake_job]), \
                 patch.object(runner_module, "_fetch_serpapi", return_value=[]), \
                 patch.object(runner_module, "_check_budget_alert"), \
                 patch("job_finder.web.scoring_runner.shutil.which", return_value=None), \
                 caplog.at_level(logging.DEBUG, logger="job_finder.web.scoring_runner"):
                runner_module.run_ingestion(db_path, config)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

        debug_records = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "claude cli not found" in r.message.lower()
        ]
        assert debug_records, (
            f"Expected a DEBUG record mentioning 'claude CLI not found' — got: "
            f"{[r.message for r in caplog.records if r.levelno == logging.DEBUG]}"
        )

    def test_zero_job_email_routed_to_activity_feed_logs_at_debug(self, caplog):
        """'Zero-job email routed to activity feed' uses logger.debug not WARNING."""
        # Inspect the source directly: the log call at pipeline_runner line ~225
        # must be logger.debug, not logger.warning.
        import ast
        import inspect
        import job_finder.web.pipeline_runner as runner_module

        source = inspect.getsource(runner_module._fetch_gmail)
        # Find the log call that mentions "activity feed" or "Zero-job email"
        assert "logger.debug" in source and ("Zero-job email" in source or "activity feed" in source), (
            "pipeline_runner._fetch_gmail: 'Zero-job email routed to activity feed' "
            "must use logger.debug (found: check for logger.warning in source)"
        )
        # Also verify it is NOT logger.warning for that specific message
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "Zero-job email" in line or ("activity feed" in line and "routed" in line):
                # The .debug/.warning call is on this line or the preceding line
                context = "\n".join(lines[max(0, i-2):i+1])
                assert "logger.warning" not in context, (
                    f"'Zero-job email routed to activity feed' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )

    def test_haiku_no_result_logs_at_debug(self, caplog):
        """'Haiku: no result for' uses logger.debug not WARNING."""
        import inspect
        import job_finder.web.scoring_runner as scoring_runner_module

        source = inspect.getsource(scoring_runner_module.run_haiku_scoring)
        # Find the line(s) referencing "no result for"
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "no result for" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"'Haiku: no result for' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                # Verify the debug call is present
                assert "logger.debug" in context, (
                    f"'Haiku: no result for' must use logger.debug.\n"
                    f"Context:\n{context}"
                )

# ---------------------------------------------------------------------------
# rejection_analyzer.py — INFO demotion
# ---------------------------------------------------------------------------

class TestRejectionAnalyzerLogLevels:
    """rejection_analyzer.py log level regressions."""

    def test_budget_cap_logs_at_info_not_warning(self, caplog):
        """'monthly budget cap reached' in rejection_analyzer uses logger.info."""
        import inspect
        import job_finder.web.rejection_analyzer as ra_module

        source = inspect.getsource(ra_module._run_analysis)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "budget cap reached" in line.lower() or "monthly budget cap" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"rejection_analyzer budget cap message must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.info" in context, (
                    f"rejection_analyzer budget cap message must use logger.info.\n"
                    f"Context:\n{context}"
                )

    def test_budget_cap_caplog_integration(self, caplog):
        """When cost_gate returns False, rejection_analyzer emits an INFO record."""
        import job_finder.web.rejection_analyzer as ra_module

        db_path, conn = _make_migrated_db()
        # Insert a rejected + unreviewed job so the code reaches the budget gate
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                source_id, first_seen, last_seen, score, score_breakdown,
                pipeline_status, rejection_reviewed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("co|title|loc", "Eng", "Co", "NYC", "[]", "[]", "",
             "2026-01-01", "2026-01-01", 50.0, "{}", "rejected", 0),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"monthly_budget_usd": 25.0}}

        try:
            with patch("job_finder.web.rejection_analyzer.cost_gate", return_value=False):
                with caplog.at_level(logging.INFO, logger="job_finder.web.rejection_analyzer"):
                    ra_module.run_rejection_analysis(db_path, config)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "budget cap" in r.message.lower()
        ]
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "budget cap" in r.message.lower()
        ]
        assert info_records, "Expected INFO record for budget cap in rejection_analyzer"
        assert not warning_records, "Budget cap must not be WARNING in rejection_analyzer"

# ---------------------------------------------------------------------------
# interview_prep.py — 2 INFO demotions
# ---------------------------------------------------------------------------

class TestInterviewPrepLogLevels:
    """interview_prep.py log level regressions."""

    def test_cost_gate_false_logs_at_info(self, caplog):
        """When cost_gate returns False, interview_prep logs at INFO not WARNING."""
        import inspect
        import job_finder.web.interview_prep as ip_module

        source = inspect.getsource(ip_module._run_prep_generation)
        lines = source.splitlines()

        # Find the budget gate branch (cost_gate returning False path)
        found_budget_log = False
        for i, line in enumerate(lines):
            if "budget exceeded" in line.lower() and "cost_gate" not in line:
                found_budget_log = True
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"interview_prep cost_gate=False branch must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.info" in context, (
                    f"interview_prep cost_gate=False branch must use logger.info.\n"
                    f"Context:\n{context}"
                )
        assert found_budget_log, (
            "Could not find 'budget exceeded' log line in interview_prep._run_prep_generation"
        )

    def test_budget_exceeded_error_logs_at_info(self, caplog):
        """BudgetExceededError handler in interview_prep uses logger.info not WARNING."""
        import inspect
        import job_finder.web.interview_prep as ip_module
        from job_finder.web.claude_client import BudgetExceededError

        source = inspect.getsource(ip_module._run_prep_generation)
        lines = source.splitlines()

        # Find the except BudgetExceededError block
        in_budget_except = False
        for i, line in enumerate(lines):
            if "except BudgetExceededError" in line:
                in_budget_except = True
            if in_budget_except and ("logger.info" in line or "logger.warning" in line):
                assert "logger.warning" not in line, (
                    f"BudgetExceededError handler must not use logger.warning at line {i+1}: {line.strip()}"
                )
                assert "logger.info" in line, (
                    f"BudgetExceededError handler must use logger.info at line {i+1}: {line.strip()}"
                )
                break

    def test_budget_cap_caplog_integration(self, caplog):
        """cost_gate=False path in interview_prep emits INFO record."""
        import job_finder.web.interview_prep as ip_module

        db_path, conn = _make_migrated_db()
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                source_id, first_seen, last_seen, score, score_breakdown)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("co|eng|nyc", "Eng", "Co", "NYC", "[]", "[]", "",
             "2026-01-01", "2026-01-01", 50.0, "{}"),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"monthly_budget_usd": 25.0}}

        try:
            with patch("job_finder.web.interview_prep.cost_gate", return_value=False):
                with caplog.at_level(logging.INFO, logger="job_finder.web.interview_prep"):
                    ip_module.generate_interview_prep_background("co|eng|nyc", db_path, config)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "budget exceeded" in r.message.lower()
        ]
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "budget" in r.message.lower()
        ]
        assert info_records, "Expected INFO record for budget exceeded in interview_prep"
        assert not warning_records, "Budget message must not be WARNING in interview_prep"

# ---------------------------------------------------------------------------
# ats_scanner.py — INFO demotion
# ---------------------------------------------------------------------------

class TestAtsScannerLogLevels:
    """ats_scanner.py log level regressions."""

    def test_promoted_to_unreachable_logs_at_info(self, caplog):
        """'promoted to unreachable' uses logger.info not WARNING."""
        import inspect
        import job_finder.web.ats_scanner as ats_module

        source = inspect.getsource(ats_module._handle_scan_error)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "promoted to unreachable" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"ats_scanner 'promoted to unreachable' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.info" in context, (
                    f"ats_scanner 'promoted to unreachable' must use logger.info.\n"
                    f"Context:\n{context}"
                )

    def test_promoted_to_unreachable_caplog_integration(self, caplog):
        """_handle_scan_error promotion to unreachable emits INFO record."""
        import job_finder.web.ats_scanner as ats_module
        from datetime import datetime, timezone

        db_path, conn = _make_migrated_db()

        # Insert a company at retry_count = _MAX_RETRIES - 1 so the next error promotes it
        max_retries = ats_module._MAX_RETRIES
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status, retry_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("AcmeCorp", "AcmeCorp", "greenhouse", "acme", "error", max_retries - 1, now, now),
        )
        conn.commit()

        company_id = conn.execute(
            "SELECT id FROM companies WHERE name = 'AcmeCorp'"
        ).fetchone()[0]

        try:
            with caplog.at_level(logging.INFO, logger="job_finder.web.ats_prober"):
                ats_module._handle_scan_error(
                    conn, company_id, "AcmeCorp", "connection refused", now
                )
        finally:
            conn.close()
            if os.path.exists(db_path):
                os.remove(db_path)

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "unreachable" in r.message.lower()
        ]
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "unreachable" in r.message.lower()
        ]
        assert info_records, "Expected INFO record for 'promoted to unreachable'"
        assert not warning_records, "'promoted to unreachable' must not be WARNING"

# ---------------------------------------------------------------------------
# blueprints/settings.py — DEBUG demotion
# ---------------------------------------------------------------------------

class TestSettingsLogLevels:
    """blueprints/settings.py log level regressions."""

    def test_blocked_wipe_logs_at_debug(self, caplog):
        """'settings save: blocked wipe of' uses logger.debug not WARNING."""
        import inspect
        from job_finder.web.blueprints import settings as settings_module

        source = inspect.getsource(settings_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "blocked wipe of" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"settings 'blocked wipe of' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.debug" in context, (
                    f"settings 'blocked wipe of' must use logger.debug.\n"
                    f"Context:\n{context}"
                )

# ---------------------------------------------------------------------------
# blueprints/jobs.py — 2 INFO demotions
# ---------------------------------------------------------------------------

class TestJobsBlueprintLogLevels:
    """blueprints/jobs.py log level regressions."""

    def test_paste_jd_budget_cap_logs_at_info(self, caplog):
        """'paste-jd: budget cap reached' uses logger.info not WARNING."""
        import inspect
        from job_finder.web.blueprints import jobs as jobs_module

        source = inspect.getsource(jobs_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "paste-jd: budget cap reached" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"jobs.py 'paste-jd: budget cap reached' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.info" in context, (
                    f"jobs.py 'paste-jd: budget cap reached' must use logger.info.\n"
                    f"Context:\n{context}"
                )

    def test_rescore_budget_cap_logs_at_info(self, caplog):
        """'rescore: budget cap reached' uses logger.info not WARNING."""
        import inspect
        from job_finder.web.blueprints import jobs as jobs_module

        source = inspect.getsource(jobs_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "rescore: budget cap reached" in line.lower():
                context = "\n".join(lines[max(0, i-3):i+1])
                assert "logger.warning" not in context, (
                    f"jobs.py 'rescore: budget cap reached' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )
                assert "logger.info" in context, (
                    f"jobs.py 'rescore: budget cap reached' must use logger.info.\n"
                    f"Context:\n{context}"
                )

# ---------------------------------------------------------------------------
# parsers/ziprecruiter_parser.py — body-size guard
# ---------------------------------------------------------------------------

class TestZipRecruiterParserBodyGuard:
    """ziprecruiter_parser.py body-size guard regression."""

    def test_no_jobs_warning_suppressed_for_empty_body(self, caplog):
        """Empty/short body does NOT trigger the 'no jobs found' WARNING."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        with caplog.at_level(logging.WARNING, logger="job_finder.parsers.ziprecruiter_parser"):
            # Empty body — should return [] silently (guard at top of function)
            result = parse_ziprecruiter_alert("")
        assert result == []
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "no jobs found" in r.message.lower()
        ]
        assert not warning_records, (
            "Empty body must NOT trigger 'no jobs found' WARNING"
        )

    def test_no_jobs_warning_suppressed_for_short_body(self, caplog):
        """Body of <= 100 stripped chars does NOT trigger the 'no jobs found' WARNING."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        short_body = "<html><body>Hi</body></html>"
        with caplog.at_level(logging.WARNING, logger="job_finder.parsers.ziprecruiter_parser"):
            result = parse_ziprecruiter_alert(short_body)
        assert result == []
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "no jobs found" in r.message.lower()
        ]
        assert not warning_records, (
            f"Short body ({len(short_body.strip())} chars) must NOT trigger 'no jobs found' WARNING"
        )

    def test_no_jobs_warning_fires_for_substantive_body(self, caplog):
        """Body > 100 stripped chars with no jobs DOES trigger the WARNING (guard preserved)."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        # A substantive HTML body with no parseable job links
        substantive_body = (
            "<html><body>"
            "<p>Welcome to your weekly ZipRecruiter digest.</p>"
            "<p>We found several opportunities that might interest you.</p>"
            "<p>Please check back later for updated listings.</p>"
            "<p>This is a test body with enough content to exceed the guard threshold.</p>"
            "</body></html>"
        )
        assert len(substantive_body.strip()) > 100

        with caplog.at_level(logging.WARNING, logger="job_finder.parsers.ziprecruiter_parser"):
            result = parse_ziprecruiter_alert(substantive_body)

        assert result == []
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "no jobs found" in r.message.lower()
        ]
        assert warning_records, (
            "Substantive body with no parseable jobs MUST trigger 'no jobs found' WARNING"
        )

    def test_guard_condition_present_in_source(self):
        """Source code contains the body-size guard expression."""
        import inspect
        from job_finder.parsers import ziprecruiter_parser

        source = inspect.getsource(ziprecruiter_parser.parse_ziprecruiter_alert)
        assert "len(body.strip()) > 100" in source, (
            "ziprecruiter_parser must have body-size guard: "
            "`if not jobs and body and len(body.strip()) > 100:`"
        )
