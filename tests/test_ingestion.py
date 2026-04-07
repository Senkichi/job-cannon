"""Tests for the ingestion pipeline runner.

Tests:
- email_parse_log entry created after successful Gmail run
- email_parse_log entry created with error when Gmail fails
- Gmail failure does not stop SerpAPI ingestion (per-source error isolation)
- Single job persistence failure does not halt other jobs (per-job error isolation)
- run_ingestion returns summary dict with correct counts
- ZipRecruiter parser returns list (even when HTML is unrecognized)
"""

import sqlite3
import tempfile
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def migrated_db_path():
    """Create a fully migrated temp DB, yield path, clean up after.

    # intentional — local fixture yields path only, unlike conftest migrated_db
    # which yields (path, conn). This file's tests only need the path.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def minimal_config(migrated_db_path):
    """Minimal config dict with both sources enabled (mocked in tests)."""
    return {
        "db": {"path": migrated_db_path},
        "sources": {
            "gmail": {"enabled": True, "lookback_days": 7},
            "serpapi": {
                "enabled": True,
                "api_key": "test-key",
                "queries": [{"query": "Data Scientist", "location": "Remote"}],
            },
        },
        "profile": {
            "target_titles": ["Senior Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 100000,
            "exclusions": {"title_keywords": [], "companies": []},
            "industries": [],
            "skills": [],
        },
        "scoring": {
            "weights": {
                "title_match": 0.30,
                "seniority_alignment": 0.20,
                "location_fit": 0.15,
                "salary_range": 0.15,
                "industry_relevance": 0.10,
                "company_signals": 0.05,
                "recency": 0.05,
            },
            "min_score_threshold": 0,  # accept all jobs in tests
        },
    }


def _make_job(title="Senior Data Scientist", company="Acme", location="Remote") -> Job:
    """Create a minimal Job for testing."""
    return Job(
        title=title,
        company=company,
        location=location,
        source="test",
        source_url=f"https://example.com/{title.lower().replace(' ', '-')}",
    )


# ---------------------------------------------------------------------------
# Test: email_parse_log entry created on success
# ---------------------------------------------------------------------------

class TestEmailParseLog:
    def test_gmail_success_creates_log_entry(self, minimal_config, migrated_db_path):
        """After a successful Gmail run, an email_parse_log entry is written."""
        fake_jobs = [_make_job()]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (fake_jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Verify email_parse_log has an entry
        conn = sqlite3.connect(migrated_db_path)
        rows = conn.execute("SELECT * FROM email_parse_log").fetchall()
        conn.close()

        assert len(rows) >= 1
        # The entry should be for gmail source
        senders = [row[2] for row in rows]  # sender column
        assert "gmail" in senders

    def test_gmail_failure_creates_error_log_entry(self, minimal_config, migrated_db_path):
        """When GmailSource raises, an error entry is written to email_parse_log."""
        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.side_effect = Exception("OAuth token expired")
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Verify error is in summary
        assert len(summary["gmail_errors"]) >= 1
        assert "OAuth token expired" in summary["gmail_errors"][0]

        # Verify email_parse_log has an error entry
        conn = sqlite3.connect(migrated_db_path)
        rows = conn.execute(
            "SELECT * FROM email_parse_log WHERE error IS NOT NULL"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Test: Per-source error isolation
# ---------------------------------------------------------------------------

class TestSourceErrorIsolation:
    def test_gmail_failure_does_not_stop_serpapi(self, minimal_config, migrated_db_path):
        """If Gmail throws an exception, SerpAPI still runs."""
        serpapi_jobs = [_make_job(title="Staff DS", company="TechCorp")]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.side_effect = Exception("Gmail OAuth failed")
            mock_serp_instance = MockSerpAPI.return_value
            mock_serp_instance.fetch_jobs.return_value = serpapi_jobs

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Gmail should have errored
        assert len(summary["gmail_errors"]) >= 1
        # SerpAPI should have succeeded
        assert summary["serpapi_fetched"] == 1
        # The SerpAPI job should be in the database
        conn = sqlite3.connect(migrated_db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_serpapi_failure_does_not_stop_gmail(self, minimal_config, migrated_db_path):
        """If SerpAPI throws, Gmail still persists its jobs."""
        gmail_jobs = [_make_job(title="Senior DS", company="StartupCo")]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            mock_gmail_instance = MockGmail.return_value
            mock_gmail_instance.fetch_jobs.return_value = (gmail_jobs, [])
            MockSerpAPI.side_effect = Exception("SerpAPI quota exceeded")

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # SerpAPI should have errored
        assert len(summary["serpapi_errors"]) >= 1
        # Gmail job should be persisted
        assert summary["gmail_fetched"] == 1
        conn = sqlite3.connect(migrated_db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count >= 1


# ---------------------------------------------------------------------------
# Test: Thordata error isolation
# ---------------------------------------------------------------------------

class TestThordataErrorIsolation:
    def test_thordata_failure_does_not_stop_other_sources(self, minimal_config, migrated_db_path):
        """If Thordata throws, Gmail and SerpAPI still run."""
        minimal_config["sources"]["thordata"] = {
            "enabled": True,
            "api_key": "test-key",
            "queries": [{"query": "DS", "location": "Remote"}],
            "max_age_days": 3,
        }
        gmail_jobs = [_make_job(title="Senior DS", company="GmailCo")]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.thordata_source.ThordataSource") as MockThordata, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, [])
            # SerpAPI is not patched: minimal_config leaves it disabled, so
            # _fetch_serpapi returns [] before ever instantiating SerpAPISource.
            MockThordata.side_effect = Exception("Thordata API down")

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        assert len(summary["thordata_errors"]) >= 1
        assert summary["gmail_fetched"] == 1
        assert summary["serpapi_fetched"] == 0


# ---------------------------------------------------------------------------
# Test: Batch error continuation
# ---------------------------------------------------------------------------

class TestBatchErrorContinuation:
    """Verify all non-failing sources complete when one source raises."""

    def test_batch_error_continuation_after_single_source_failure(
        self, minimal_config, migrated_db_path
    ):
        """If Gmail raises, SerpAPI and Thordata still run and persist jobs."""
        minimal_config["sources"]["thordata"] = {
            "enabled": True,
            "api_key": "test-key",
            "queries": [{"query": "DS", "location": "Remote"}],
            "max_age_days": 3,
        }
        serpapi_jobs = [_make_job(title="SerpAPI Job", company="SerpCo")]
        thordata_jobs = [_make_job(title="Thordata Job", company="ThorCo")]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.sources.thordata_source.ThordataSource") as MockThordata, \
             patch("job_finder.web.pipeline_runner.anthropic", None):
            MockGmail.side_effect = Exception("Gmail auth revoked")
            MockSerpAPI.return_value.fetch_jobs.return_value = serpapi_jobs
            MockThordata.return_value.fetch_jobs.return_value = thordata_jobs

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        assert len(summary["gmail_errors"]) >= 1
        assert summary["serpapi_fetched"] == 1
        assert summary["thordata_fetched"] == 1
        conn = sqlite3.connect(migrated_db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count >= 2


# ---------------------------------------------------------------------------
# Test: Cross-source dedup
# ---------------------------------------------------------------------------

class TestCrossSourceDedup:
    def test_same_job_from_serpapi_and_thordata_persisted_once(
        self, minimal_config, migrated_db_path
    ):
        """When SerpAPI and Thordata return the same job (same title/company/location),
        upsert_job's dedup key ensures only 1 DB row is created, not 2."""
        minimal_config["sources"]["thordata"] = {
            "enabled": True,
            "api_key": "test-key",
            "queries": [{"query": "DS", "location": "Remote"}],
            "max_age_days": 3,
        }
        # Identical title/company/location → same dedup_key
        shared_job = _make_job(title="Staff Data Scientist", company="DupCo", location="Remote")

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.sources.thordata_source.ThordataSource") as MockThordata, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = [shared_job]
            MockThordata.return_value.fetch_jobs.return_value = [shared_job]

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Two sources returned the same job; DB should have exactly 1 new row
        assert summary["jobs_new"] == 1
        assert summary["serpapi_fetched"] == 1
        assert summary["thordata_fetched"] == 1


# ---------------------------------------------------------------------------
# Test: Per-job error isolation
# ---------------------------------------------------------------------------

class TestJobErrorIsolation:
    def test_single_job_failure_does_not_halt_others(self, minimal_config, migrated_db_path):
        """If one job fails to persist, other jobs are still saved."""
        jobs = [
            _make_job(title="Senior Data Scientist", company="GoodCo"),
            _make_job(title="Staff Data Scientist", company="AlsoCo"),
            _make_job(title="Principal DS", company="ThirdCo"),
        ]

        call_count = 0

        def mock_upsert(conn, job):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # second job fails
                raise sqlite3.OperationalError("disk full")
            return True

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.ingestion_runner.upsert_job", side_effect=mock_upsert), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            mock_gmail_instance = MockGmail.return_value
            mock_gmail_instance.fetch_jobs.return_value = (jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Should have 1 error for the failing job
        assert len(summary["job_errors"]) == 1
        # But the other 2 jobs should be accounted for
        assert summary["jobs_new"] == 2


# ---------------------------------------------------------------------------
# Test: Summary dict structure
# ---------------------------------------------------------------------------

class TestSummaryDict:
    def test_run_ingestion_returns_summary_dict(self, minimal_config, migrated_db_path):
        """run_ingestion always returns a dict with the expected keys."""
        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        expected_keys = {
            "gmail_fetched",
            "gmail_errors",
            "serpapi_fetched",
            "serpapi_errors",
            "thordata_fetched",
            "thordata_errors",
            "jobs_new",
            "jobs_updated",
            "jobs_scored",
            "job_errors",
            "duration_seconds",
        }
        assert expected_keys.issubset(set(summary.keys()))

    def test_summary_counts_are_accurate(self, minimal_config, migrated_db_path):
        """Summary counts reflect the actual number of jobs fetched and saved."""
        gmail_jobs = [_make_job(title="Senior DS", company="Co1")]
        serp_jobs = [_make_job(title="Staff DS", company="Co2")]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, [])
            # SerpAPISource is instantiated with an api_key, so mock the class
            MockSerpAPI.return_value.fetch_jobs.return_value = serp_jobs

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        assert summary["gmail_fetched"] == 1
        assert summary["serpapi_fetched"] == 1
        assert summary["jobs_new"] == 2
        assert summary["duration_seconds"] >= 0

    def test_empty_run_returns_zero_counts(self, minimal_config, migrated_db_path):
        """When no jobs are fetched, all counts are zero and no errors."""
        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        assert summary["gmail_fetched"] == 0
        assert summary["serpapi_fetched"] == 0
        assert summary["jobs_new"] == 0
        assert summary["gmail_errors"] == []
        assert summary["serpapi_errors"] == []


# ---------------------------------------------------------------------------
# Test: ZipRecruiter parser
# ---------------------------------------------------------------------------

class TestZipRecruiterParser:
    def test_parser_returns_list_on_empty_body(self):
        """parse_ziprecruiter_alert returns an empty list for empty input."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert
        result = parse_ziprecruiter_alert("", email_date=None)
        assert isinstance(result, list)
        assert result == []

    def test_parser_returns_list_on_unrecognized_html(self):
        """parse_ziprecruiter_alert returns empty list for unrecognized HTML."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert
        html = "<html><body><p>Some random content with no job structure.</p></body></html>"
        result = parse_ziprecruiter_alert(html, email_date=None)
        assert isinstance(result, list)

    def test_parser_returns_list_on_malformed_html(self):
        """parse_ziprecruiter_alert does not raise on malformed HTML."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert
        html = "<html><unclosed><div>bad html"
        result = parse_ziprecruiter_alert(html)
        assert isinstance(result, list)

    def test_parser_extracts_jobs_from_link_structure(self):
        """parse_ziprecruiter_alert extracts jobs when ZipRecruiter links are present."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        html = """
        <html><body>
        <table>
          <tr>
            <td>
              <a href="https://www.ziprecruiter.com/jobs/acme-corp-senior-data-scientist-abcd1234">
                Senior Data Scientist
              </a>
              <span>Acme Corp</span>
              <span>San Francisco, CA</span>
            </td>
          </tr>
        </table>
        </body></html>
        """
        result = parse_ziprecruiter_alert(html, email_date=datetime(2026, 3, 10))
        assert isinstance(result, list)
        # Should find at least one job
        if result:
            job = result[0]
            assert job.source == "ziprecruiter"
            assert "data scientist" in job.title.lower()

    def test_parser_returns_job_objects(self):
        """If jobs are found, they are Job instances."""
        from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

        html = """
        <html><body>
          <a href="https://www.ziprecruiter.com/jobs/staff-engineer-xyz789">Staff Engineer</a>
        </body></html>
        """
        result = parse_ziprecruiter_alert(html)
        assert isinstance(result, list)
        for job in result:
            assert isinstance(job, Job)
            assert job.source == "ziprecruiter"


# ---------------------------------------------------------------------------
# Test: first_seen uses email date (Phase 6 requirement)
# ---------------------------------------------------------------------------

class TestFirstSeenEmailDate:
    """Tests that upsert_job uses posted_date as first_seen for Gmail jobs."""

    def test_upsert_job_uses_posted_date_as_first_seen(self, migrated_db_path):
        """When a Job has posted_date set, upsert_job stores it as first_seen."""
        from job_finder.db import upsert_job

        # conftest migrated_db fixture yields (path, conn)
        # test_ingestion has its own migrated_db_path fixture that yields just path
        fd, path = __import__("tempfile").mkstemp(suffix=".db")
        __import__("os").close(fd)
        run_migrations(path)

        try:
            email_date = datetime(2026, 3, 5, 9, 30, 0)
            job = Job(
                title="Senior Data Scientist",
                company="TestCo",
                location="Remote",
                source="linkedin",
                source_url="https://linkedin.com/jobs/view/123/",
                source_id="123",
                posted_date=email_date,
            )

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            is_new = upsert_job(conn, job)
            conn.close()

            assert is_new is True

            conn2 = sqlite3.connect(path)
            row = conn2.execute(
                "SELECT first_seen FROM jobs WHERE dedup_key = ?",
                (job.dedup_key,),
            ).fetchone()
            conn2.close()

            assert row is not None
            # first_seen should be the email date, not ingestion time
            first_seen = row[0]
            assert first_seen.startswith("2026-03-05"), (
                f"Expected first_seen to start with 2026-03-05 (email date), got: {first_seen}"
            )
        finally:
            if __import__("os").path.exists(path):
                __import__("os").remove(path)

    def test_upsert_job_uses_now_when_posted_date_none(self, migrated_db_path):
        """When a Job has posted_date=None, upsert_job stores current time as first_seen."""
        from job_finder.db import upsert_job

        fd, path = __import__("tempfile").mkstemp(suffix=".db")
        __import__("os").close(fd)
        run_migrations(path)

        try:
            before = datetime.now(timezone.utc).replace(tzinfo=None)
            job = Job(
                title="Staff Engineer",
                company="SerpCo",
                location="Remote",
                source="serpapi",
                source_url="https://example.com/job/456",
                source_id="456",
                posted_date=None,  # SerpAPI jobs have no email date
            )

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            upsert_job(conn, job)
            after = datetime.now(timezone.utc).replace(tzinfo=None)
            conn.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT first_seen FROM jobs WHERE dedup_key = ?",
                (job.dedup_key,),
            ).fetchone()
            conn.close()

            assert row is not None
            first_seen_dt = datetime.fromisoformat(row[0])
            # first_seen should be between before and after (ingestion time)
            assert before <= first_seen_dt <= after, (
                f"Expected first_seen between {before} and {after}, got {first_seen_dt}"
            )
        finally:
            if __import__("os").path.exists(path):
                __import__("os").remove(path)


# ---------------------------------------------------------------------------
# Test: Smart upsert_job merge — locations_raw, location concatenation,
#       description dedup (Phase 6 Plan 02 requirement)
# ---------------------------------------------------------------------------

import json as _json


class TestSmartUpsertJobMerge:
    """Tests for the smart upsert_job merge behavior added in Plan 06-02."""

    def _make_db(self):
        """Create a fresh migrated temp DB, return (path, conn)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return path, conn

    def test_insert_initializes_locations_raw(self):
        """INSERT branch stores initial location in locations_raw JSON array."""
        path, db = self._make_db()
        try:
            job = Job(
                title="Senior Engineer",
                company="Acme",
                location="San Francisco, CA",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
            )
            upsert_job(db, job)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT locations_raw FROM jobs WHERE dedup_key = ?",
                (job.dedup_key,),
            ).fetchone()
            conn.close()

            assert row is not None
            locations_raw = _json.loads(row[0])
            assert isinstance(locations_raw, list)
            assert "San Francisco, CA" in locations_raw
        finally:
            try:
                db.close()
            except Exception:
                pass
            if os.path.exists(path):
                os.remove(path)

    def test_update_appends_new_location_to_locations_raw(self):
        """UPDATE branch appends new location to locations_raw array."""
        path, db = self._make_db()
        try:
            # Insert the same job from two different locations
            job1 = Job(
                title="Senior Engineer",
                company="Acme",
                location="San Francisco, CA",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
            )
            job2 = Job(
                title="Senior Engineer",
                company="Acme",
                location="New York, NY",
                source="glassdoor",
                source_url="https://glassdoor.com/jobs/2",
            )
            upsert_job(db, job1)
            upsert_job(db, job2)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT locations_raw, location FROM jobs WHERE dedup_key = ?",
                (job1.dedup_key,),
            ).fetchone()
            conn.close()

            assert row is not None
            locations_raw = _json.loads(row[0])
            # Both locations should be in the array
            assert "San Francisco, CA" in locations_raw
            assert "New York, NY" in locations_raw
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_remote_location_prioritized_in_merge(self):
        """Remote location appears first in merged locations_raw."""
        path, db = self._make_db()
        try:
            job1 = Job(
                title="Senior Engineer",
                company="Acme",
                location="San Francisco, CA",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
            )
            job2 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",
                source="glassdoor",
                source_url="https://glassdoor.com/jobs/2",
            )
            upsert_job(db, job1)
            upsert_job(db, job2)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT locations_raw FROM jobs WHERE dedup_key = ?",
                (job1.dedup_key,),
            ).fetchone()
            conn.close()

            locations_raw = _json.loads(row[0])
            # Remote should be first
            assert locations_raw[0] == "Remote"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_duplicate_location_not_added_twice(self):
        """Same location is not duplicated in locations_raw on second upsert."""
        path, db = self._make_db()
        try:
            job1 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
            )
            job2 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",  # Same location again
                source="glassdoor",
                source_url="https://glassdoor.com/jobs/2",
            )
            upsert_job(db, job1)
            upsert_job(db, job2)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT locations_raw FROM jobs WHERE dedup_key = ?",
                (job1.dedup_key,),
            ).fetchone()
            conn.close()

            locations_raw = _json.loads(row[0])
            assert locations_raw.count("Remote") == 1
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_description_substring_not_duplicated(self):
        """If new description is substring of existing, it is not appended."""
        path, db = self._make_db()
        try:
            long_desc = "We are hiring a Senior Engineer. You will build scalable systems."
            short_desc = "We are hiring a Senior Engineer."

            job1 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
                description=long_desc,
            )
            job2 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",
                source="glassdoor",
                source_url="https://glassdoor.com/jobs/2",
                description=short_desc,  # Substring of first
            )
            upsert_job(db, job1)
            upsert_job(db, job2)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT description FROM jobs WHERE dedup_key = ?",
                (job1.dedup_key,),
            ).fetchone()
            conn.close()

            assert row is not None
            # Description should be the long one, not doubled
            assert row[0] == long_desc
            assert "---" not in row[0]
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_different_description_appended_with_separator(self):
        """If new description is substantially different, it is appended with separator."""
        path, db = self._make_db()
        try:
            desc1 = "We are building the future of cloud infrastructure."
            desc2 = "Join our team to work on machine learning products."

            job1 = Job(
                title="Senior Engineer",
                company="Acme",
                location="Remote",
                source="linkedin",
                source_url="https://linkedin.com/jobs/1",
                description=desc1,
            )
            job2 = Job(
                title="Senior Engineer",
                company="Acme",
                location="NYC",
                source="glassdoor",
                source_url="https://glassdoor.com/jobs/2",
                description=desc2,
            )
            upsert_job(db, job1)
            upsert_job(db, job2)
            db.close()

            conn = sqlite3.connect(path)
            row = conn.execute(
                "SELECT description FROM jobs WHERE dedup_key = ?",
                (job1.dedup_key,),
            ).fetchone()
            conn.close()

            assert row is not None
            # Both descriptions should be present
            assert desc1 in row[0]
            assert desc2 in row[0]
            # Separator should be present
            assert "---" in row[0]
        finally:
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# Test: Company auto-population hook (Phase 7 Plan 01)
# ---------------------------------------------------------------------------

class TestCompanyAutoPopulation:
    """Tests for company auto-population in pipeline_runner._score_and_persist."""

    @pytest.fixture
    def lever_job(self):
        """Job with a Lever source URL."""
        return Job(
            title="Senior Data Scientist",
            company="Stripe",
            location="Remote",
            source="linkedin",
            source_url="https://jobs.lever.co/stripe/abc-123",
        )

    @pytest.fixture
    def non_ats_job(self):
        """Job with a non-ATS source URL."""
        return Job(
            title="Staff Data Scientist",
            company="BetterHelp",
            location="San Jose, CA",
            source="linkedin",
            source_url="https://www.linkedin.com/jobs/view/999/",
        )

    def test_lever_job_creates_company_with_ats_platform(
        self, minimal_config, migrated_db_path
    ):
        """After ingesting a Lever job, company record has ats_platform='lever' and correct slug."""
        lever_jobs = [
            Job(
                title="Senior Data Scientist",
                company="Stripe",
                location="Remote",
                source="linkedin",
                source_url="https://jobs.lever.co/stripe/job-abc-123",
            )
        ]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (lever_jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company = conn.execute(
            "SELECT * FROM companies WHERE name = 'stripe'"
        ).fetchone()
        conn.close()

        assert company is not None, "Company record should be created after ingestion"
        assert company["ats_platform"] == "lever"
        assert company["ats_slug"] == "stripe"
        assert company["ats_probe_status"] == "hit"

    def test_non_ats_job_creates_company_with_pending_status(
        self, minimal_config, migrated_db_path
    ):
        """After ingesting a non-ATS job, company record exists with ats_probe_status='pending'."""
        non_ats_jobs = [
            Job(
                title="Staff Data Scientist",
                company="BetterHelp",
                location="San Jose, CA",
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/9999/",
            )
        ]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (non_ats_jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company = conn.execute(
            "SELECT * FROM companies WHERE name = 'betterhelp'"
        ).fetchone()
        conn.close()

        assert company is not None, "Company record should be created for non-ATS jobs too"
        assert company["ats_probe_status"] == "pending"
        assert company["ats_platform"] is None

    def test_company_upsert_failure_does_not_crash_ingestion(
        self, minimal_config, migrated_db_path
    ):
        """Company upsert failure is non-fatal — ingestion continues and returns summary."""
        jobs = [
            Job(
                title="Senior Data Scientist",
                company="TestCo",
                location="Remote",
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/111/",
            )
        ]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch(
                 "job_finder.web.ats_scanner.upsert_company",
                 side_effect=Exception("DB connection failed"),
             ), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        # Ingestion should complete successfully despite company upsert failure
        assert summary["jobs_new"] >= 1
        assert summary["job_errors"] == []

    def test_jobs_table_has_company_id_linked(self, minimal_config, migrated_db_path):
        """After ingestion, jobs.company_id is linked to the company record."""
        jobs = [
            Job(
                title="Senior Data Scientist",
                company="Acme",
                location="Remote",
                source="linkedin",
                source_url="https://jobs.lever.co/acme/job-xyz",
            )
        ]

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = (jobs, [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        job_row = conn.execute("SELECT company_id FROM jobs WHERE company = 'Acme'").fetchone()
        company_row = conn.execute("SELECT id FROM companies WHERE name = 'acme'").fetchone()
        conn.close()

        assert job_row is not None
        assert company_row is not None
        assert job_row["company_id"] == company_row["id"], (
            "Job should be linked to its company via company_id"
        )


# ---------------------------------------------------------------------------
# Test: ScoringResult unwrap (Phase 11 plan 01)
# ---------------------------------------------------------------------------


class TestScoringResultUnwrap:
    """Regression tests: run_haiku_scoring and run_sonnet_evaluation correctly
    unwrap ScoringResult via .data and .status instead of treating it as a dict."""

    def _make_job_row(self, db_path: str, dedup_key: str = "testco|data scientist|remote",
                      jd_full: str | None = None) -> str:
        """Insert a minimal job row using correct schema, return dedup_key."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, jd_full)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key, "Data Scientist", "TestCo", "Remote",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", jd_full),
        )
        conn.commit()
        conn.close()
        return dedup_key

    def test_haiku_scoring_unwraps_scoring_result(self, migrated_db_path):
        """run_haiku_scoring correctly extracts score from ScoringResult.data."""
        from job_finder.web.scoring_runner import run_haiku_scoring
        from job_finder.web.scoring_types import ScoringResult

        dedup_key = self._make_job_row(migrated_db_path)

        scoring_result = ScoringResult(
            data={
                "score": 75,
                "summary": "Good match",
                "title_fit": "strong",
                "location_fit": "remote",
                "salary_meets_floor": True,
            },
            status="success",
        )

        config = {"scoring": {"haiku_threshold": 42}, "profile": {"exclusions": {}}}

        with patch("job_finder.web.scoring_runner.score_job_haiku", return_value=scoring_result), \
             patch("job_finder.web.scoring_runner.anthropic") as mock_anthropic, \
             patch("job_finder.web.scoring_runner.should_exclude", return_value=(False, None)), \
             patch("job_finder.web.scoring_runner.enrich_job", None):
            mock_anthropic.Anthropic.return_value = MagicMock()

            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], config, migrated_db_path)

        assert haiku_scored == 1, f"Expected 1 haiku-scored job, got {haiku_scored}"

        conn = sqlite3.connect(migrated_db_path)
        row = conn.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 75, f"Expected haiku_score=75, got {row[0]}"

    def test_haiku_scoring_handles_error_scoring_result(self, migrated_db_path):
        """run_haiku_scoring gracefully handles ScoringResult with data=None (error status)."""
        from job_finder.web.scoring_runner import run_haiku_scoring
        from job_finder.web.scoring_types import ScoringResult

        dedup_key = self._make_job_row(migrated_db_path, dedup_key="testco|engineer|remote")

        error_result = ScoringResult(data=None, status="error")

        config = {"scoring": {"haiku_threshold": 42}, "profile": {"exclusions": {}}}

        with patch("job_finder.web.scoring_runner.score_job_haiku", return_value=error_result), \
             patch("job_finder.web.scoring_runner.anthropic") as mock_anthropic, \
             patch("job_finder.web.scoring_runner.should_exclude", return_value=(False, None)), \
             patch("job_finder.web.scoring_runner.enrich_job", None):
            mock_anthropic.Anthropic.return_value = MagicMock()

            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], config, migrated_db_path)

        assert haiku_scored == 0, f"Expected 0 scored jobs on error, got {haiku_scored}"

    def test_sonnet_evaluation_unwraps_scoring_result(self, migrated_db_path):
        """run_sonnet_evaluation correctly extracts score from ScoringResult.data."""
        from job_finder.web.scoring_runner import run_sonnet_evaluation
        from job_finder.web.scoring_types import ScoringResult

        dedup_key = self._make_job_row(
            migrated_db_path,
            dedup_key="corp|scientist|nyc",
            jd_full=(
                "We are looking for a Data Scientist to join our growing team. "
                "You will build machine learning models, design experiments, and work "
                "closely with product and engineering teams to drive data-informed decisions. "
                "Requirements: 3+ years of experience in data science, proficiency in Python "
                "and SQL, and a strong foundation in statistics and machine learning."
            ),
        )

        scoring_result = ScoringResult(
            data={
                "score": 82,
                "summary": "Great fit for this role",
                "fit_analysis": {
                    "strengths": ["Python"],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
            },
            status="success",
        )

        config = {"scoring": {}, "profile": {}}

        with patch("job_finder.web.scoring_runner.evaluate_job_sonnet", return_value=scoring_result), \
             patch("job_finder.web.scoring_runner.anthropic") as mock_anthropic, \
             patch("job_finder.web.scoring_runner.enrich_company_info", None):
            mock_anthropic.Anthropic.return_value = MagicMock()

            count = run_sonnet_evaluation([dedup_key], config, migrated_db_path)

        assert count == 1, f"Expected 1 sonnet-evaluated job, got {count}"

        conn = sqlite3.connect(migrated_db_path)
        row = conn.execute(
            "SELECT sonnet_score FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 82, f"Expected sonnet_score=82, got {row[0]}"


# ---------------------------------------------------------------------------
# Test: Gmail message-level dedup (Priority 1 integration path)
# ---------------------------------------------------------------------------


class TestGmailMessageDedup:
    """Integration tests for the second-sync dedup path in _fetch_gmail."""

    def test_second_run_skips_previously_processed_gmail_ids(
        self, minimal_config, migrated_db_path
    ):
        """Pre-seeded message IDs in email_parse_log are NOT re-fetched on a
        subsequent run — this is the core correctness claim of the dedup feature."""
        import sqlite3 as _sqlite3
        from unittest.mock import MagicMock, patch

        known_id = "msg_already_seen_001"

        # Pre-seed email_parse_log so the next run treats this ID as known
        setup_conn = _sqlite3.connect(migrated_db_path)
        setup_conn.execute(
            "INSERT OR IGNORE INTO email_parse_log"
            " (message_id, sender, processed_at, jobs_found)"
            " VALUES (?, 'gmail', datetime('now'), 0)",
            (known_id,),
        )
        setup_conn.commit()
        setup_conn.close()

        mock_source = MagicMock()
        # Simulate GmailSource.fetch_jobs returning no jobs and no new IDs
        # (because the only candidate was already known and filtered out)
        mock_source.fetch_jobs.return_value = ([], [])
        mock_source.parse_failures = []

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value = mock_source
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        # Verify fetch_jobs was called with the known_id in processed_message_ids
        assert mock_source.fetch_jobs.called
        call_kwargs = mock_source.fetch_jobs.call_args
        passed_ids = call_kwargs.kwargs.get(
            "processed_message_ids", call_kwargs.args[1] if len(call_kwargs.args) > 1 else set()
        )
        assert known_id in passed_ids, (
            f"Expected known_id '{known_id}' to be in processed_message_ids passed to "
            f"fetch_jobs, got: {passed_ids}"
        )

    def test_new_ids_bulk_inserted_into_email_parse_log(
        self, minimal_config, migrated_db_path
    ):
        """Message IDs returned by fetch_jobs are persisted to email_parse_log
        so the next sync can skip them."""
        import sqlite3 as _sqlite3
        from unittest.mock import MagicMock, patch

        new_msg_ids = ["msg_new_001", "msg_new_002"]

        mock_source = MagicMock()
        mock_source.fetch_jobs.return_value = ([], new_msg_ids)
        mock_source.parse_failures = []

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value = mock_source
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        conn = _sqlite3.connect(migrated_db_path)
        rows = conn.execute(
            "SELECT message_id FROM email_parse_log WHERE sender = 'gmail'"
        ).fetchall()
        conn.close()

        stored_ids = {row[0] for row in rows}
        for mid in new_msg_ids:
            assert mid in stored_ids, (
                f"Expected message_id '{mid}' to be inserted into email_parse_log"
            )

    def test_non_gmail_sender_rows_excluded_from_known_ids(
        self, minimal_config, migrated_db_path
    ):
        """Rows with a non-'gmail' sender in email_parse_log (e.g. pipeline detection
        entries) must NOT be passed to fetch_jobs as known IDs — the dedup query
        must be scoped to sender='gmail'."""
        import sqlite3 as _sqlite3
        from unittest.mock import MagicMock, patch

        pipeline_detection_id = "pipeline_detection_row_001"

        # Pre-seed a row with a non-gmail sender (simulates a pipeline_detector entry
        # that happened to land in email_parse_log with a different sender)
        setup_conn = _sqlite3.connect(migrated_db_path)
        setup_conn.execute(
            "INSERT OR IGNORE INTO email_parse_log"
            " (message_id, sender, processed_at, jobs_found)"
            " VALUES (?, 'pipeline_detector', datetime('now'), 0)",
            (pipeline_detection_id,),
        )
        setup_conn.commit()
        setup_conn.close()

        mock_source = MagicMock()
        mock_source.fetch_jobs.return_value = ([], [])
        mock_source.parse_failures = []

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value = mock_source
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        assert mock_source.fetch_jobs.called
        call_kwargs = mock_source.fetch_jobs.call_args
        passed_ids = call_kwargs.kwargs.get(
            "processed_message_ids", call_kwargs.args[1] if len(call_kwargs.args) > 1 else set()
        )
        assert pipeline_detection_id not in passed_ids, (
            f"Non-gmail sender row '{pipeline_detection_id}' must not appear in "
            f"processed_message_ids passed to fetch_jobs; got: {passed_ids}"
        )


class TestPruneStaleData:
    """Tests for _prune_stale_data TTL cleanup."""

    def test_prune_stale_data_removes_old_parse_failure_entries(
        self, migrated_db_path
    ):
        """parse_failure rows older than 30 days are deleted; recent rows survive."""
        import sqlite3 as _sqlite3
        from job_finder.web.pipeline_runner import _prune_stale_data

        conn = _sqlite3.connect(migrated_db_path)

        # Insert an old parse_failure row (35 days ago)
        conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)"
            " VALUES (datetime('now', '-35 days'), 'example_parse_failure', 0, 0, 0)"
        )
        # Insert a recent parse_failure row (5 days ago — should survive)
        conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)"
            " VALUES (datetime('now', '-5 days'), 'example_parse_failure', 0, 0, 0)"
        )
        # Insert a regular row older than 90 days (should be deleted)
        conn.execute(
            "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)"
            " VALUES (datetime('now', '-95 days'), 'gmail', 0, 0, 0)"
        )
        conn.commit()

        _prune_stale_data(conn)

        rows = conn.execute(
            "SELECT source, timestamp FROM runs ORDER BY timestamp"
        ).fetchall()
        conn.close()

        sources = [r[0] for r in rows]
        # Old parse_failure row gone
        assert sources.count("example_parse_failure") == 1, (
            "Expected only the recent parse_failure row to survive"
        )
        # Ancient regular row gone
        assert "gmail" not in sources, (
            "Expected 90-day-old gmail row to be pruned"
        )

    def test_prune_stale_data_removes_old_email_parse_log_rows(
        self, migrated_db_path
    ):
        """email_parse_log rows older than 14 days with sender='gmail' are deleted."""
        import sqlite3 as _sqlite3
        from job_finder.web.pipeline_runner import _prune_stale_data

        conn = _sqlite3.connect(migrated_db_path)

        # Old row (20 days ago) — should be pruned (TTL=14 with default lookback=7)
        conn.execute(
            "INSERT OR IGNORE INTO email_parse_log"
            " (message_id, sender, processed_at, jobs_found)"
            " VALUES ('old_msg_001', 'gmail', datetime('now', '-20 days'), 0)"
        )
        # Recent row (3 days ago) — should survive
        conn.execute(
            "INSERT OR IGNORE INTO email_parse_log"
            " (message_id, sender, processed_at, jobs_found)"
            " VALUES ('recent_msg_001', 'gmail', datetime('now', '-3 days'), 0)"
        )
        conn.commit()

        _prune_stale_data(conn)  # lookback_days=7 → TTL=14

        rows = conn.execute(
            "SELECT message_id FROM email_parse_log WHERE sender = 'gmail'"
        ).fetchall()
        conn.close()

        ids = {r[0] for r in rows}
        assert "old_msg_001" not in ids, "Old email_parse_log row should have been pruned"
        assert "recent_msg_001" in ids, "Recent email_parse_log row should have survived"

    def test_prune_stale_data_ttl_scales_with_lookback_days(
        self, migrated_db_path
    ):
        """TTL is max(lookback_days * 2, 14); a 20-day row survives when lookback=30."""
        import sqlite3 as _sqlite3
        from job_finder.web.pipeline_runner import _prune_stale_data

        conn = _sqlite3.connect(migrated_db_path)

        # 20-day-old row: pruned at default TTL=14, but should survive TTL=60 (lookback=30)
        conn.execute(
            "INSERT OR IGNORE INTO email_parse_log"
            " (message_id, sender, processed_at, jobs_found)"
            " VALUES ('msg_20d', 'gmail', datetime('now', '-20 days'), 0)"
        )
        conn.commit()

        _prune_stale_data(conn, lookback_days=30)  # TTL = max(60, 14) = 60

        rows = conn.execute(
            "SELECT message_id FROM email_parse_log WHERE sender = 'gmail'"
        ).fetchall()
        conn.close()

        ids = {r[0] for r in rows}
        assert "msg_20d" in ids, (
            "Row 20 days old should survive with lookback_days=30 (TTL=60)"
        )


# ---------------------------------------------------------------------------
# Test: Gmail pagination cap (SAFE-03, Phase 11 plan 01)
# ---------------------------------------------------------------------------


class TestGmailPaginationCap:
    """Regression tests: GmailSource._search_messages respects max_messages=500 cap."""

    def test_gmail_pagination_cap_stops_at_500(self):
        """_search_messages stops paginating when it reaches 500 messages."""
        from job_finder.sources.gmail_source import GmailSource

        # Build a mock service that always returns 100 messages with a nextPageToken
        page_call_count = 0

        def make_page(page_num: int) -> dict:
            start = page_num * 100
            return {
                "messages": [{"id": str(start + i)} for i in range(100)],
                "nextPageToken": f"token_{page_num + 1}",
            }

        mock_execute = MagicMock()
        mock_execute.execute.side_effect = lambda: make_page(page_call_count)

        mock_list = MagicMock(return_value=mock_execute)

        def counting_execute():
            nonlocal page_call_count
            result = make_page(page_call_count)
            page_call_count += 1
            return result

        mock_execute.execute.side_effect = counting_execute

        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.list.return_value = mock_execute

        # Create GmailSource with mocked service
        source = GmailSource.__new__(GmailSource)
        source.service = mock_service
        source.parse_failures = []

        result = source._search_messages("test query")

        assert len(result) == 500, (
            f"Expected exactly 500 messages (cap), got {len(result)}"
        )
        # Should have called .list().execute() at most 5 times (5 pages × 100 = 500)
        # Possibly 6 times if the cap check happens after the 5th page extends to 500
        assert page_call_count <= 6, (
            f"Expected at most 6 API pages fetched, got {page_call_count}"
        )


# ---------------------------------------------------------------------------
# Test: Batch pre-ingestion dedup
# ---------------------------------------------------------------------------

class TestBatchDedup:
    """Tests for the batch pre-check that routes known jobs to _touch_existing_job
    and routes new (or salary-carrying) jobs to the full _score_and_persist path."""

    def test_known_job_skips_scorer(self, minimal_config, migrated_db_path):
        """A job already in the DB routes to _touch_existing_job, not _score_and_persist."""
        job = _make_job()

        # Pre-insert so the batch pre-check finds it
        conn = sqlite3.connect(migrated_db_path)
        upsert_job(conn, job)
        conn.close()

        mock_score_and_persist = MagicMock()

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner._score_and_persist", mock_score_and_persist), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([job], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        mock_score_and_persist.assert_not_called()
        assert summary["jobs_touch_only"] == 1

    def test_new_job_uses_full_scoring(self, minimal_config, migrated_db_path):
        """A job not in the DB routes to _score_and_persist."""
        job = _make_job()
        mock_score_and_persist = MagicMock()

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner._score_and_persist", mock_score_and_persist), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([job], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        mock_score_and_persist.assert_called_once()
        assert summary["jobs_touch_only"] == 0

    def test_known_job_with_salary_uses_full_scoring(self, minimal_config, migrated_db_path):
        """Known job with new salary data bypasses touch-only path (salary guard)."""
        base_job = _make_job()

        # Pre-insert base job (no salary)
        conn = sqlite3.connect(migrated_db_path)
        upsert_job(conn, base_job)
        conn.commit()
        conn.close()

        # Incoming job: same dedup_key, but carries salary data
        salary_job = _make_job()
        salary_job.salary_min = 200000

        mock_score_and_persist = MagicMock()

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner._score_and_persist", mock_score_and_persist), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([salary_job], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, minimal_config)

        mock_score_and_persist.assert_called_once()

    def test_touch_updates_last_seen_and_merges_source(self, migrated_db_path):
        """_touch_existing_job updates last_seen and merges new source/source_url into JSON columns."""
        import json

        conn = sqlite3.connect(migrated_db_path)
        job = _make_job()
        dedup_key = job.dedup_key
        old_url = "https://old.example.com/job"

        # Insert with explicit old last_seen, single-source list, and one source_url
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key, job.title, job.company, job.location,
             '["gmail"]', json.dumps([old_url]), "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Incoming job arrives from a different source with a new URL
        incoming = _make_job()
        incoming.source = "serpapi"
        incoming.source_url = "https://new.example.com/job"

        summary: dict = {"jobs_touch_only": 0}

        from job_finder.web.pipeline_runner import _touch_existing_job
        _touch_existing_job(incoming, conn, summary)

        row = conn.execute(
            "SELECT last_seen, sources, source_urls FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        conn.close()

        assert row is not None
        last_seen, sources_json, source_urls_json = row
        assert last_seen > "2026-01-01", f"last_seen not updated: {last_seen}"
        sources = json.loads(sources_json)
        assert "gmail" in sources, f"gmail missing from merged sources: {sources}"
        assert "serpapi" in sources, f"serpapi missing from merged sources: {sources}"
        source_urls = json.loads(source_urls_json)
        assert old_url in source_urls, f"old URL missing from merged source_urls: {source_urls}"
        assert "https://new.example.com/job" in source_urls, f"new URL missing from merged source_urls: {source_urls}"
        assert summary["jobs_touch_only"] == 1

    def test_touch_failure_falls_back_to_full_scoring(
        self, minimal_config, migrated_db_path, caplog
    ):
        """When _touch_existing_job raises, the fallback path calls _score_and_persist
        and emits a WARNING log. jobs_touch_only stays at 0."""
        import logging

        job = _make_job()

        # Pre-insert so the batch pre-check routes to the touch path
        conn = sqlite3.connect(migrated_db_path)
        upsert_job(conn, job)
        conn.close()

        mock_score_and_persist = MagicMock()

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner._touch_existing_job",
                   side_effect=Exception("DB locked")), \
             patch("job_finder.web.pipeline_runner._score_and_persist",
                   mock_score_and_persist), \
             patch("job_finder.web.pipeline_runner.anthropic", None), \
             caplog.at_level(logging.WARNING, logger="job_finder.web.pipeline_runner"):

            MockGmail.return_value.fetch_jobs.return_value = ([job], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        mock_score_and_persist.assert_called_once()
        assert summary["jobs_touch_only"] == 0
        assert any("Touch-update failed" in r.message for r in caplog.records), (
            f"Expected WARNING about touch failure, got: {[r.message for r in caplog.records]}"
        )

    def test_archived_job_uses_full_scoring(self, minimal_config, migrated_db_path):
        """An archived job re-appearing in ingestion routes to _score_and_persist,
        not touch-only, so upsert_job() can auto-reopen it to 'discovered'."""
        job = _make_job()

        # Pre-insert and mark as archived
        conn = sqlite3.connect(migrated_db_path)
        upsert_job(conn, job)
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()
        conn.close()

        mock_score_and_persist = MagicMock()

        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail, \
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI, \
             patch("job_finder.web.pipeline_runner._score_and_persist", mock_score_and_persist), \
             patch("job_finder.web.pipeline_runner.anthropic", None):

            MockGmail.return_value.fetch_jobs.return_value = ([job], [])
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        mock_score_and_persist.assert_called_once()
        assert summary["jobs_touch_only"] == 0


# ---------------------------------------------------------------------------
# Test: DataForSEO orchestration (_submit_dataforseo_tasks / _collect_dataforseo_results)
# ---------------------------------------------------------------------------

def _dataforseo_config(migrated_db_path: str, enabled: bool = True, api_key: str = "dGVzdDp0ZXN0") -> dict:
    """Return a config dict with DataForSEO configured."""
    return {
        "db": {"path": migrated_db_path},
        "sources": {
            "gmail": {"enabled": False},
            "serpapi": {"enabled": False},
            "thordata": {"enabled": False},
            "scaleserp": {"enabled": False},
            "dataforseo": {
                "enabled": enabled,
                "api_key": api_key,
                "queries": [{"query": "Data Scientist", "location": "Remote"}],
                "max_age_days": 7,
                "depth": 20,
                "priority": 1,
                "poll_interval_seconds": 0,
                "poll_timeout_seconds": 5,
            },
        },
        "profile": {
            "target_titles": ["Senior Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 0,
            "exclusions": {"title_keywords": [], "companies": []},
            "industries": [],
            "skills": [],
        },
        "scoring": {
            "weights": {
                "title_match": 0.30,
                "seniority_alignment": 0.20,
                "location_fit": 0.15,
                "salary_range": 0.15,
                "industry_relevance": 0.10,
                "company_signals": 0.05,
                "recency": 0.05,
            },
            "min_score_threshold": 0,
        },
    }


class TestDataForSEOOrchestration:
    """Tests for _submit_dataforseo_tasks and _collect_dataforseo_results helpers."""

    def test_disabled_source_returns_empty_task_ids(self, migrated_db_path):
        """DataForSEO disabled -> _submit returns ([], None), no errors."""
        config = _dataforseo_config(migrated_db_path, enabled=False)

        from job_finder.web.pipeline_runner import _submit_dataforseo_tasks

        summary = {"dataforseo_fetched": 0, "dataforseo_errors": []}
        task_ids, source = _submit_dataforseo_tasks(config, summary)

        assert task_ids == []
        assert source is None
        assert summary["dataforseo_errors"] == []

    def test_empty_api_key_returns_empty_and_populates_errors(self, migrated_db_path):
        """api_key='' -> ([], None) and 'not configured' in dataforseo_errors."""
        config = _dataforseo_config(migrated_db_path, enabled=True, api_key="")

        from job_finder.web.pipeline_runner import _submit_dataforseo_tasks

        summary = {"dataforseo_fetched": 0, "dataforseo_errors": []}
        task_ids, source = _submit_dataforseo_tasks(config, summary)

        assert task_ids == []
        assert source is None
        assert len(summary["dataforseo_errors"]) == 1
        assert "not configured" in summary["dataforseo_errors"][0]

    def test_submit_exception_populates_errors(self, migrated_db_path):
        """If DataForSEOSource.submit_tasks raises, dataforseo_errors is populated and ([], None) returned."""
        config = _dataforseo_config(migrated_db_path, enabled=True)

        from job_finder.web.pipeline_runner import _submit_dataforseo_tasks

        summary = {"dataforseo_fetched": 0, "dataforseo_errors": []}
        # Patch DataForSEOSource at the module level so the lazy import inside
        # _submit_dataforseo_tasks picks up the mock. submit_tasks must raise
        # (not just return []) to trigger the except branch that writes to errors.
        with patch("job_finder.sources.dataforseo_source.DataForSEOSource") as MockSrc:
            MockSrc.return_value.submit_tasks.side_effect = RuntimeError("network timeout")
            task_ids, source = _submit_dataforseo_tasks(config, summary)

        assert task_ids == []
        assert source is None
        assert len(summary["dataforseo_errors"]) == 1
        assert "network timeout" in summary["dataforseo_errors"][0]

    def test_submit_returns_empty_list_without_exception_populates_errors(self, migrated_db_path):
        """submit_tasks() returning [] without raising (API-level rejection) populates dataforseo_errors."""
        config = _dataforseo_config(migrated_db_path, enabled=True)

        from job_finder.web.pipeline_runner import _submit_dataforseo_tasks

        summary = {"dataforseo_fetched": 0, "dataforseo_errors": []}
        # Patch DataForSEOSource so submit_tasks returns [] without raising —
        # simulates all tasks being rejected at the DataForSEO API level.
        with patch("job_finder.sources.dataforseo_source.DataForSEOSource") as MockSrc:
            MockSrc.return_value.submit_tasks.return_value = []
            task_ids, source = _submit_dataforseo_tasks(config, summary)

        assert task_ids == []
        assert source is None
        assert len(summary["dataforseo_errors"]) == 1
        assert "all tasks rejected" in summary["dataforseo_errors"][0]

    def test_collect_exception_populates_errors_and_returns_empty(self, migrated_db_path):
        """If collect_results raises, dataforseo_errors is populated and [] is returned."""
        from job_finder.web.pipeline_runner import _collect_dataforseo_results

        mock_source = MagicMock()
        mock_source.collect_results.side_effect = RuntimeError("poll timed out")

        summary = {"dataforseo_fetched": 0, "dataforseo_errors": []}
        jobs = _collect_dataforseo_results(mock_source, ["id-001"], summary)

        assert jobs == []
        assert len(summary["dataforseo_errors"]) == 1
        assert "poll timed out" in summary["dataforseo_errors"][0]
        assert summary["dataforseo_fetched"] == 0

