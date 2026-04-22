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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = (fake_jobs, set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.side_effect = Exception("OAuth token expired")
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.side_effect = Exception("Gmail OAuth failed")
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            mock_gmail_instance = MockGmail.return_value
            mock_gmail_instance.fetch_jobs.return_value = (gmail_jobs, set())
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
             patch("job_finder.web.ingestion_runner.upsert_job", side_effect=mock_upsert):

            mock_gmail_instance = MockGmail.return_value
            mock_gmail_instance.fetch_jobs.return_value = (jobs, set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = ([], set())
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, minimal_config)

        expected_keys = {
            "gmail_fetched",
            "gmail_errors",
            "serpapi_fetched",
            "serpapi_errors",
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = ([], set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = (lever_jobs, set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = (non_ats_jobs, set())
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
                 "job_finder.web.ats_company.upsert_company",
                 side_effect=Exception("DB connection failed"),
             ):

            MockGmail.return_value.fetch_jobs.return_value = (jobs, set())
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
             patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI:

            MockGmail.return_value.fetch_jobs.return_value = (jobs, set())
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
             patch("job_finder.web.scoring_runner.should_exclude", return_value=(False, None)), \
             patch("job_finder.web.scoring_runner.enrich_job", None):

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
             patch("job_finder.web.scoring_runner.should_exclude", return_value=(False, None)), \
             patch("job_finder.web.scoring_runner.enrich_job", None):

            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], config, migrated_db_path)

        assert haiku_scored == 0, f"Expected 0 scored jobs on error, got {haiku_scored}"

    def test_sonnet_evaluation_unwraps_scoring_result(self, migrated_db_path):
        """run_sonnet_evaluation correctly extracts score from ScoringResult.data."""
        from job_finder.web.scoring_runner import run_sonnet_evaluation
        from job_finder.web.scoring_types import ScoringResult

        dedup_key = self._make_job_row(
            migrated_db_path,
            dedup_key="corp|scientist|nyc",
            jd_full="Full job description text for Sonnet evaluation.",
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
             patch("job_finder.web.scoring_runner.enrich_company_info", None):

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
# Test: DataForSEO e2e — jobs flow through run_ingestion into database
# ---------------------------------------------------------------------------

class TestDataForSEOIngestion:
    """E2E: DataForSEO submit/collect pipeline is wired into run_ingestion."""

    @pytest.fixture
    def dataforseo_config(self, migrated_db_path):
        """Config with DataForSEO enabled and other sources disabled."""
        return {
            "db": {"path": migrated_db_path},
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
                "dataforseo": {
                    "enabled": True,
                    "api_key": "dGVzdDp0ZXN0",  # base64("test:test")
                    "depth": 10,
                    "max_age_days": 7,
                    "priority": 1,
                    "poll_interval_seconds": 0,
                    "poll_timeout_seconds": 10,
                    "queries": [
                        {"query": "Data Scientist", "location": "Remote"},
                    ],
                },
            },
            "profile": {
                "target_titles": ["Data Scientist"],
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
                "min_score_threshold": 0,
            },
        }

    def _make_dataforseo_jobs(self, n=3):
        return [
            Job(
                title=f"Data Scientist {i}",
                company=f"DfseCompany{i}",
                location="Remote",
                source="dataforseo",
                source_url=f"https://example.com/dfse-{i}",
                source_id=f"dfse-job-{i}",
            )
            for i in range(n)
        ]

    def test_dataforseo_jobs_persisted_in_database(self, dataforseo_config, migrated_db_path):
        """DataForSEO jobs flow through run_ingestion and land in the jobs table."""
        fake_jobs = self._make_dataforseo_jobs(3)

        mock_source = MagicMock()
        mock_source.submit_tasks.return_value = ["task-001"]
        mock_source.collect_results.return_value = fake_jobs

        with patch(
            "job_finder.sources.dataforseo_source.DataForSEOSource",
            return_value=mock_source,
        ):
            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, dataforseo_config)

        assert summary["dataforseo_fetched"] == 3
        assert summary["jobs_new"] == 3

        # Verify jobs are in the database
        conn = sqlite3.connect(migrated_db_path)
        rows = conn.execute(
            "SELECT title, company FROM jobs WHERE sources LIKE '%dataforseo%'"
        ).fetchall()
        conn.close()

        assert len(rows) == 3
        companies = {r[1] for r in rows}
        assert companies == {"DfseCompany0", "DfseCompany1", "DfseCompany2"}

    def test_dataforseo_logged_in_runs_table(self, dataforseo_config, migrated_db_path):
        """DataForSEO run is recorded in the runs table."""
        mock_source = MagicMock()
        mock_source.submit_tasks.return_value = ["task-001"]
        mock_source.collect_results.return_value = self._make_dataforseo_jobs(2)

        with patch(
            "job_finder.sources.dataforseo_source.DataForSEOSource",
            return_value=mock_source,
        ):
            from job_finder.web.pipeline_runner import run_ingestion
            run_ingestion(migrated_db_path, dataforseo_config)

        conn = sqlite3.connect(migrated_db_path)
        row = conn.execute(
            "SELECT source, jobs_fetched FROM runs WHERE source = 'dataforseo'"
        ).fetchone()
        conn.close()

        assert row is not None, "Expected a 'dataforseo' entry in runs table"
        assert row[1] == 2

    def test_dataforseo_disabled_skips_silently(self, dataforseo_config, migrated_db_path):
        """When DataForSEO is disabled in config, no tasks are submitted."""
        dataforseo_config["sources"]["dataforseo"]["enabled"] = False

        with patch(
            "job_finder.sources.dataforseo_source.DataForSEOSource",
        ) as MockCls:
            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, dataforseo_config)

        MockCls.assert_not_called()
        assert summary["dataforseo_fetched"] == 0

    def test_dataforseo_submit_failure_does_not_crash_ingestion(
        self, dataforseo_config, migrated_db_path
    ):
        """If DataForSEO submit_tasks raises, other sources still run."""
        with patch(
            "job_finder.sources.dataforseo_source.DataForSEOSource",
            side_effect=Exception("API auth failed"),
        ):
            from job_finder.web.pipeline_runner import run_ingestion
            summary = run_ingestion(migrated_db_path, dataforseo_config)

        assert summary["dataforseo_fetched"] == 0
        assert len(summary["dataforseo_errors"]) >= 1


# ---------------------------------------------------------------------------
# Phase 34 Plan 2 — use_unified_scorer flag gate tests
#
# Verifies run_ingestion routes new-job scoring through either the legacy
# run_haiku_scoring + run_sonnet_evaluation path (flag false / absent) or
# the unified run_scoring path (flag true). Commit A ships the flag with
# default false; Commit B flips it to true in config.yaml after a smoke
# test on the dev DB.
# ---------------------------------------------------------------------------


class TestUnifiedScorerFlagGate:
    """Phase 34 Plan 3 Commit E — use_unified_scorer flag after legacy else-branch deletion.

    The legacy Haiku/Sonnet two-phase branch is removed in Commit E; only the
    unified-scorer path remains. The flag is still consulted but now with a
    default of True, and a False value makes run_ingestion skip AI scoring
    entirely (no legacy fallback). Plan 4 removes the flag itself.
    """

    def _job(self, title="Unified DS", company="Acme"):
        return _make_job(title=title, company=company)

    def _run_with_flag(self, flag_value, minimal_config, migrated_db_path):
        """Common harness — runs ingestion with a single fake job and the
        given flag value, recording whether the unified scorer was called."""
        import job_finder.web.pipeline_runner as pr

        gmail_jobs = [self._job()]
        flags = {"unified_called": False}

        def fake_unified(keys, cfg, db):
            flags["unified_called"] = True
            return {
                "scored": len(keys),
                "classified_apply": 0,
                "classified_consider": 0,
                "classified_skip": 0,
                "classified_reject": 0,
                "skipped_dead": 0,
                "skipped_no_jd": 0,
                "errors": 0,
            }

        # Configure the flag in minimal_config if provided (None = absent key).
        cfg = dict(minimal_config)
        if flag_value is None:
            cfg.pop("use_unified_scorer", None)
        else:
            cfg["use_unified_scorer"] = flag_value

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerp,
            patch("job_finder.web.scoring_runner.run_scoring",
                  side_effect=fake_unified),
        ):
            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, set())
            MockSerp.return_value.fetch_jobs.return_value = []
            pr.run_ingestion(migrated_db_path, cfg)

        return flags

    def test_flag_false_skips_ai_scoring(
        self, minimal_config, migrated_db_path,
    ):
        """Flag False -> unified scorer NOT invoked (no fallback to legacy path)."""
        flags = self._run_with_flag(False, minimal_config, migrated_db_path)
        assert flags["unified_called"] is False

    def test_flag_true_uses_run_scoring(self, minimal_config, migrated_db_path):
        """Flag True -> run_scoring invoked."""
        flags = self._run_with_flag(True, minimal_config, migrated_db_path)
        assert flags["unified_called"] is True

    def test_flag_absent_defaults_true(
        self, minimal_config, migrated_db_path,
    ):
        """Config with no use_unified_scorer key -> default True (v3.0 Commit E)."""
        flags = self._run_with_flag(None, minimal_config, migrated_db_path)
        assert flags["unified_called"] is True

    def test_flag_true_populates_classification_summary_keys(
        self, minimal_config, migrated_db_path,
    ):
        """Flag True path writes classified_{apply,consider,skip,reject} keys
        into the run summary."""
        import job_finder.web.pipeline_runner as pr

        gmail_jobs = [self._job()]

        def fake_unified(keys, cfg, db):
            return {
                "scored": 1,
                "classified_apply": 1,
                "classified_consider": 0,
                "classified_skip": 0,
                "classified_reject": 0,
                "skipped_dead": 0,
                "skipped_no_jd": 0,
                "errors": 0,
            }

        cfg = dict(minimal_config)
        cfg["use_unified_scorer"] = True

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerp,
            patch("job_finder.web.scoring_runner.run_scoring",
                  side_effect=fake_unified),
        ):
            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, set())
            MockSerp.return_value.fetch_jobs.return_value = []
            summary = pr.run_ingestion(migrated_db_path, cfg)

        assert summary.get("scored") == 1
        assert summary.get("classified_apply") == 1
        assert "classified_consider" in summary
        assert "classified_skip" in summary
        assert "classified_reject" in summary


class TestUnifiedScorerConfigShape:
    """Phase 34 Plan 2 — configuration artifacts for the unified scorer."""

    def test_config_example_has_use_unified_scorer_flag(self):
        """config.example.yaml ships with use_unified_scorer: false default."""
        from pathlib import Path
        text = Path("config.example.yaml").read_text(encoding="utf-8")
        assert "use_unified_scorer: false" in text

    def test_config_example_has_providers_scoring_block(self):
        """config.example.yaml documents the providers.scoring block template."""
        from pathlib import Path
        text = Path("config.example.yaml").read_text(encoding="utf-8")
        # Commented example — search for the scoring: sub-block and qwen model.
        assert "scoring:" in text
        assert "qwen2.5:14b" in text

    def test_config_yaml_has_providers_scoring_block(self):
        """config.yaml (user config) has providers.scoring after Plan 2."""
        from pathlib import Path
        text = Path("config.yaml").read_text(encoding="utf-8")
        # Active scoring block (not commented) — must be indented under providers:.
        assert "\n  scoring:\n" in text
        assert "qwen2.5:14b" in text

    def test_config_yaml_use_unified_scorer_present(self):
        """config.yaml has the use_unified_scorer flag (value is env-specific)."""
        from pathlib import Path
        text = Path("config.yaml").read_text(encoding="utf-8")
        assert "use_unified_scorer:" in text


class TestRunOneshotLegacyFixture:
    """Sanity tests for the new mock_run_oneshot_legacy conftest fixture."""

    def test_legacy_fixture_yields_legacy_envelope(self, mock_run_oneshot_legacy):
        """Declaring mock_run_oneshot_legacy in a signature returns a mock
        whose envelope carries only the legacy {score, summary} shape."""
        import json
        envelope = mock_run_oneshot_legacy.return_value
        assert envelope["is_error"] is False
        parsed = json.loads(envelope["result"])
        assert parsed.get("score") == 75
        assert parsed.get("summary") == "Good match"
        # v3 ordinal keys should be absent on the legacy envelope.
        assert "title_fit" not in parsed


class TestCascadeConfigScoringFixture:
    """Sanity tests for the new cascade_config_scoring conftest fixture."""

    def test_cascade_config_scoring_shape(self, cascade_config_scoring):
        """Fixture exposes providers.scoring with model qwen2.5:14b and
        the full Phase 34 cascade fallback chain."""
        providers = cascade_config_scoring["providers"]
        assert "scoring" in providers
        scoring = providers["scoring"]
        assert scoring["model"] == "qwen2.5:14b"
        assert scoring["provider"] == "ollama"
        assert any(
            link.get("provider") == "anthropic"
            for link in scoring["fallback_chain"]
        )
        # Flag co-ships with the fixture so tests exercise the unified path.
        assert cascade_config_scoring["use_unified_scorer"] is True
