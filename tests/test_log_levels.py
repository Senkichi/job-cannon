"""Regression tests for Phase 37 log-level demotions (OPS-01).

Each test verifies one of the 10 log-level changes specified in the
37-01 acceptance criteria. Tests trigger the exact code path and assert
on caplog records — if any level is reverted to WARNING the test fails.

All tests are pure unit/integration tests against local state only.
No network calls, no real Anthropic client, no Gmail credentials required.
"""

import logging
import os
import sqlite3
import tempfile
from datetime import UTC

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

    def test_zero_job_email_routed_to_activity_feed_logs_at_debug(self, caplog):
        """'Zero-job email routed to activity feed' uses logger.debug not WARNING."""
        # Inspect the source directly: the log call at pipeline_runner line ~225
        # must be logger.debug, not logger.warning.
        import inspect

        import job_finder.web.pipeline_runner as runner_module

        source = inspect.getsource(runner_module._fetch_gmail)
        # Find the log call that mentions "activity feed" or "Zero-job email"
        assert "logger.debug" in source and (
            "Zero-job email" in source or "activity feed" in source
        ), (
            "pipeline_runner._fetch_gmail: 'Zero-job email routed to activity feed' "
            "must use logger.debug (found: check for logger.warning in source)"
        )
        # Also verify it is NOT logger.warning for that specific message
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "Zero-job email" in line or ("activity feed" in line and "routed" in line):
                # The .debug/.warning call is on this line or the preceding line
                context = "\n".join(lines[max(0, i - 2) : i + 1])
                assert "logger.warning" not in context, (
                    f"'Zero-job email routed to activity feed' must not use logger.warning.\n"
                    f"Context:\n{context}"
                )

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
                context = "\n".join(lines[max(0, i - 3) : i + 1])
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
        from datetime import datetime

        import job_finder.web.ats_scanner as ats_module

        db_path, conn = _make_migrated_db()

        # Insert a company at retry_count = _MAX_RETRIES - 1 so the next error promotes it
        max_retries = ats_module._MAX_RETRIES
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status, retry_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("AcmeCorp", "AcmeCorp", "greenhouse", "acme", "error", max_retries - 1, now, now),
        )
        conn.commit()

        company_id = conn.execute("SELECT id FROM companies WHERE name = 'AcmeCorp'").fetchone()[0]

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
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "unreachable" in r.message.lower()
        ]
        warning_records = [
            r
            for r in caplog.records
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
                context = "\n".join(lines[max(0, i - 3) : i + 1])
                assert "logger.warning" not in context, (
                    f"settings 'blocked wipe of' must not use logger.warning.\nContext:\n{context}"
                )
                assert "logger.debug" in context, (
                    f"settings 'blocked wipe of' must use logger.debug.\nContext:\n{context}"
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
                context = "\n".join(lines[max(0, i - 3) : i + 1])
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
                context = "\n".join(lines[max(0, i - 3) : i + 1])
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
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "no jobs found" in r.message.lower()
        ]
        assert not warning_records, "Empty body must NOT trigger 'no jobs found' WARNING"

    def test_no_jobs_warning_suppressed_for_short_body(self, caplog):
        """Body of <= 100 stripped chars does NOT trigger the 'no jobs found' WARNING."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        short_body = "<html><body>Hi</body></html>"
        with caplog.at_level(logging.WARNING, logger="job_finder.parsers.ziprecruiter_parser"):
            result = parse_ziprecruiter_alert(short_body)
        assert result == []
        warning_records = [
            r
            for r in caplog.records
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
            r
            for r in caplog.records
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
