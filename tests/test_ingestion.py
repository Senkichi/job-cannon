"""Tests for the ingestion pipeline runner.

Tests:
- email_parse_log entry created after successful Gmail run
- email_parse_log entry created with error when Gmail fails
- Gmail failure does not stop SerpAPI ingestion (per-source error isolation)
- Single job persistence failure does not halt other jobs (per-job error isolation)
- run_ingestion returns summary dict with correct counts
- ZipRecruiter parser returns list (even when HTML is unrecognized)
"""

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.db_migrate import run_migrations


def _as_parsed(j: Job):
    """Phase 48.07 caller boundary: build ParsedJob via from_job.

    Existing tests construct ``Job(...)`` and pass it straight to upsert_job;
    after the shim removal every caller must hand upsert_job a ParsedJob.
    This wrapper preserves the body of every test while moving the
    boundary into a single helper.
    """
    return ParsedJob.from_job(j)


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
            # Generic target so tests using shorthand titles like "Staff DS" /
            # "Senior DS" / "Principal DS" / "Unified DS" word-boundary-match
            # after _normalize_title expansion ("DS" -> "Data Scientist").
            # Tests that exercise the Stage 7.6 title-gate directly override
            # target_titles in their own fixture.
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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
        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
            MockGmail.return_value.fetch_jobs.side_effect = Exception("OAuth token expired")
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion

            summary = run_ingestion(migrated_db_path, minimal_config)

        # Verify error is in summary
        assert len(summary["gmail_errors"]) >= 1
        assert "OAuth token expired" in summary["gmail_errors"][0]

        # Verify email_parse_log has an error entry
        conn = sqlite3.connect(migrated_db_path)
        rows = conn.execute("SELECT * FROM email_parse_log WHERE error IS NOT NULL").fetchall()
        conn.close()

        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Test: Per-source error isolation
# ---------------------------------------------------------------------------


class TestSourceErrorIsolation:
    def test_gmail_failure_does_not_stop_serpapi(self, minimal_config, migrated_db_path):
        """If Gmail throws an exception, SerpAPI still runs."""
        serpapi_jobs = [_make_job(title="Staff DS", company="TechCorp")]

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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

        def mock_upsert(conn, parsed, **_kwargs):
            # Phase 48.07: upsert_job now takes ParsedJob plus keyword-only
            # score/score_breakdown/company_id — accept **kwargs so the mock
            # mirrors the new signature.
            from job_finder.db._jobs import UpsertResult

            nonlocal call_count
            call_count += 1
            if call_count == 2:  # second job fails
                raise sqlite3.OperationalError("disk full")
            return UpsertResult(kind="inserted", dedup_key=parsed.dedup_key)

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
            patch("job_finder.web.ingestion_runner.upsert_job", side_effect=mock_upsert),
        ):
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
        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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
        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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
            result = upsert_job(conn, _as_parsed(job))
            conn.close()

            assert result.kind == "inserted"

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
            before = datetime.now(UTC).replace(tzinfo=None)
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
            upsert_job(conn, _as_parsed(job))
            after = datetime.now(UTC).replace(tzinfo=None)
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
            upsert_job(db, _as_parsed(job))
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
            upsert_job(db, _as_parsed(job1))
            upsert_job(db, _as_parsed(job2))
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
            upsert_job(db, _as_parsed(job1))
            upsert_job(db, _as_parsed(job2))
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
            upsert_job(db, _as_parsed(job1))
            upsert_job(db, _as_parsed(job2))
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
            upsert_job(db, _as_parsed(job1))
            upsert_job(db, _as_parsed(job2))
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
            upsert_job(db, _as_parsed(job1))
            upsert_job(db, _as_parsed(job2))
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

    def test_lever_job_creates_company_with_ats_platform(self, minimal_config, migrated_db_path):
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
            MockGmail.return_value.fetch_jobs.return_value = (lever_jobs, set())
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion

            run_ingestion(migrated_db_path, minimal_config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company = conn.execute("SELECT * FROM companies WHERE name = 'stripe'").fetchone()
        conn.close()

        assert company is not None, "Company record should be created after ingestion"
        assert company["ats_platform"] == "lever"
        assert company["ats_slug"] == "stripe"
        assert company["ats_probe_status"] == "pending"

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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
            MockGmail.return_value.fetch_jobs.return_value = (non_ats_jobs, set())
            MockSerpAPI.return_value.fetch_jobs.return_value = []

            from job_finder.web.pipeline_runner import run_ingestion

            run_ingestion(migrated_db_path, minimal_config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company = conn.execute("SELECT * FROM companies WHERE name = 'betterhelp'").fetchone()
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
            patch(
                "job_finder.web.ats_company.upsert_company",
                side_effect=Exception("DB connection failed"),
            ),
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

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerpAPI,
        ):
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

# NOTE: TestScoringResultUnwrap was removed in Plan 4 Commit E along with the
# legacy run_haiku_scoring + run_sonnet_evaluation entry points. The v3 unified
# run_scoring's own ScoringResult-unwrap behavior is covered in
# tests/test_scoring_runner.py::TestRunScoring.

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

        assert len(result) == 500, f"Expected exactly 500 messages (cap), got {len(result)}"
        # Should have called .list().execute() at most 5 times (5 pages × 100 = 500)
        # Possibly 6 times if the cap check happens after the 5th page extends to 500
        assert page_call_count <= 6, f"Expected at most 6 API pages fetched, got {page_call_count}"


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
            patch("job_finder.web.scoring_runner.run_scoring", side_effect=fake_unified),
        ):
            MockGmail.return_value.fetch_jobs.return_value = (gmail_jobs, set())
            MockSerp.return_value.fetch_jobs.return_value = []
            pr.run_ingestion(migrated_db_path, cfg)

        return flags

    def test_flag_false_no_longer_disables_scoring(
        self,
        minimal_config,
        migrated_db_path,
    ):
        """Plan 4 Commit E removed the use_unified_scorer toggle.
        Setting it to False (legacy escape hatch) is now a no-op -- the
        unified runner is the only path."""
        flags = self._run_with_flag(False, minimal_config, migrated_db_path)
        assert flags["unified_called"] is True

    def test_flag_true_uses_run_scoring(self, minimal_config, migrated_db_path):
        """Flag True -> run_scoring invoked."""
        flags = self._run_with_flag(True, minimal_config, migrated_db_path)
        assert flags["unified_called"] is True

    def test_flag_absent_defaults_true(
        self,
        minimal_config,
        migrated_db_path,
    ):
        """Config with no use_unified_scorer key -> unified runner invoked
        (Plan 4 Commit E made this unconditional)."""
        flags = self._run_with_flag(None, minimal_config, migrated_db_path)
        assert flags["unified_called"] is True

    def test_flag_true_populates_classification_summary_keys(
        self,
        minimal_config,
        migrated_db_path,
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
            patch("job_finder.web.scoring_runner.run_scoring", side_effect=fake_unified),
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
    """Phase 34 Plan 4 — configuration artifacts after the deletion sweep."""

    def test_config_example_has_no_legacy_use_unified_scorer_flag(self):
        """Plan 4 Commit E removed use_unified_scorer from config.example.yaml."""
        from pathlib import Path

        text = Path("config.example.yaml").read_text(encoding="utf-8")
        assert "use_unified_scorer" not in text

    def test_config_example_has_providers_scoring_block(self):
        """config.example.yaml documents the providers.scoring block template."""
        from pathlib import Path

        text = Path("config.example.yaml").read_text(encoding="utf-8")
        # Commented example — search for the scoring: sub-block and qwen model.
        assert "scoring:" in text
        assert "qwen2.5:14b" in text


class TestCascadeConfigScoringFixture:
    """Sanity tests for the cascade_config_scoring conftest fixture."""

    def test_cascade_config_scoring_shape(self, cascade_config_scoring):
        """Fixture exposes providers.scoring with model qwen2.5:14b and
        the full Phase 34 cascade fallback chain."""
        providers = cascade_config_scoring["providers"]
        assert "scoring" in providers
        scoring = providers["scoring"]
        assert scoring["model"] == "qwen2.5:14b"
        assert scoring["provider"] == "ollama"
        assert any(link.get("provider") == "anthropic" for link in scoring["fallback_chain"])


# ---------------------------------------------------------------------------
# F3: ingestion-wiring tests for _fetch_portal_search.
#
# Covers the deferred wiring from Stages 2 + 3: portal_config (Stage-2 free
# portals) and google_cse_source (Stage-3 free SERP backend) now flow from
# config through _fetch_portal_search into fetch_all_portals. Tests verify
# the construction logic and the include_cse gate that the scheduler uses
# to enforce PLAN.md load-bearing decision #8 (CSE once per day).
# ---------------------------------------------------------------------------


class TestFetchPortalSearchWiring:
    """Unit tests for _fetch_portal_search wiring (F3)."""

    @pytest.fixture
    def base_config(self):
        """Minimal portal_search-enabled config with no SERP backends configured."""
        return {
            "sources": {
                "portal_search": {
                    "enabled": True,
                    "keywords": ["Staff Engineer"],
                    "max_serp_queries": 30,
                },
                "dataforseo": {"enabled": False},
                "google_cse": {"enabled": False},
            }
        }

    def test_portal_config_passed_to_fetch_all_portals(self, base_config):
        """portal_search sub-dict is forwarded to fetch_all_portals as portal_config."""
        base_config["sources"]["portal_search"]["jobicy"] = {"enabled": True}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {})

        assert mock_fetch.called
        kwargs = mock_fetch.call_args.kwargs
        # Stage 7.1: portal_config is a shallow copy with keyring-resolved creds
        # injected for USAJobs/Adzuna/Jooble, not the original subtree reference.
        # Value-equality is the contract that matters here.
        assert kwargs["portal_config"] == base_config["sources"]["portal_search"]
        assert kwargs["portal_config"]["jobicy"]["enabled"] is True

    def test_cse_built_when_enabled_and_credentials_present(self, base_config):
        """include_cse=True + enabled + creds → GoogleCSESource constructed and passed through."""
        base_config["sources"]["google_cse"] = {
            "enabled": True,
            "api_key": "test-cse-key",
            "cse_id": "test-cse-id",
        }

        from job_finder.sources.google_cse_source import GoogleCSESource
        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {}, include_cse=True)

        kwargs = mock_fetch.call_args.kwargs
        assert isinstance(kwargs["google_cse_source"], GoogleCSESource)

    def test_cse_skipped_when_include_cse_false(self, base_config):
        """include_cse=False suppresses CSE construction even if fully configured."""
        base_config["sources"]["google_cse"] = {
            "enabled": True,
            "api_key": "test-cse-key",
            "cse_id": "test-cse-id",
        }

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {}, include_cse=False)

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["google_cse_source"] is None

    def test_cse_skipped_when_disabled_in_config(self, base_config):
        """sources.google_cse.enabled=False → no CSE source regardless of creds."""
        base_config["sources"]["google_cse"] = {
            "enabled": False,
            "api_key": "test-cse-key",
            "cse_id": "test-cse-id",
        }

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {}, include_cse=True)

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["google_cse_source"] is None

    def test_cse_skipped_when_credentials_missing(self, base_config):
        """enabled=True but api_key/cse_id empty → no CSE source built."""
        base_config["sources"]["google_cse"] = {
            "enabled": True,
            "api_key": "",
            "cse_id": "",
        }

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {}, include_cse=True)

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["google_cse_source"] is None

    def test_portal_search_disabled_short_circuits(self, base_config):
        """portal_search.enabled=False → fetch_all_portals not called."""
        base_config["sources"]["portal_search"]["enabled"] = False

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            result = _fetch_portal_search(base_config, {})

        assert result == []
        assert not mock_fetch.called

    def test_default_include_cse_is_true(self, base_config):
        """Default arg matches the manual-sync path (CSE included if configured)."""
        base_config["sources"]["google_cse"] = {
            "enabled": True,
            "api_key": "test-cse-key",
            "cse_id": "test-cse-id",
        }

        from job_finder.sources.google_cse_source import GoogleCSESource
        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            # No include_cse kwarg → default
            _fetch_portal_search(base_config, {})

        kwargs = mock_fetch.call_args.kwargs
        assert isinstance(kwargs["google_cse_source"], GoogleCSESource)

    # Stage 7.4 — keywords→target_titles fallback (Finding #3 from E2E shakedown).
    # Production was silently early-returning [] when sources.portal_search.keywords
    # was empty, while scripts/benchmark_sources.py::_portal_keywords falls back to
    # profile.target_titles. That divergence meant the Q6 benchmark measured a
    # path users couldn't reach by toggling the Stage 7 master switch alone.

    def test_empty_keywords_falls_back_to_target_titles(self, base_config):
        """When portal_search.keywords is empty, profile.target_titles is used."""
        base_config["sources"]["portal_search"]["keywords"] = []
        base_config["profile"] = {"target_titles": ["Staff Data Scientist", "Principal Engineer"]}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {})

        assert mock_fetch.called
        # First positional arg to fetch_all_portals is the keywords list.
        args, _ = mock_fetch.call_args
        assert args[0] == ["Staff Data Scientist", "Principal Engineer"]

    def test_explicit_keywords_win_over_target_titles(self, base_config):
        """When both are populated, explicit keywords are used (target_titles ignored)."""
        base_config["sources"]["portal_search"]["keywords"] = ["explicit kw"]
        base_config["profile"] = {"target_titles": ["target title"]}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(base_config, {})

        args, _ = mock_fetch.call_args
        assert args[0] == ["explicit kw"]

    def test_both_empty_short_circuits(self, base_config):
        """Empty keywords + empty target_titles → skip, do not call fetch_all_portals."""
        base_config["sources"]["portal_search"]["keywords"] = []
        base_config["profile"] = {"target_titles": []}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            result = _fetch_portal_search(base_config, {})

        assert result == []
        assert not mock_fetch.called

    def test_missing_profile_section_short_circuits_when_keywords_empty(self, base_config):
        """No profile section at all + empty keywords → safe skip, no KeyError."""
        base_config["sources"]["portal_search"]["keywords"] = []
        # No "profile" key in config at all.

        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            result = _fetch_portal_search(base_config, {})

        assert result == []
        assert not mock_fetch.called

    # Stage 7.9 observability: surface whether the 7.4 fallback fired so the
    # dashboard's recent-activity panel can distinguish explicit-keyword runs
    # from implicit-target_titles runs.

    def test_summary_records_fallback_signal_when_keywords_empty(self, base_config):
        """When the 7.4 fallback fires, summary['portal_search_used_fallback_keywords'] = True."""
        base_config["sources"]["portal_search"]["keywords"] = []
        base_config["profile"] = {"target_titles": ["DS"]}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        summary: dict = {}
        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ):
            _fetch_portal_search(base_config, summary)

        assert summary.get("portal_search_used_fallback_keywords") is True

    def test_summary_records_no_fallback_when_keywords_explicit(self, base_config):
        """When explicit keywords are configured, the fallback signal is False."""
        base_config["sources"]["portal_search"]["keywords"] = ["explicit kw"]
        base_config["profile"] = {"target_titles": ["DS"]}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        summary: dict = {}
        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ):
            _fetch_portal_search(base_config, summary)

        assert summary.get("portal_search_used_fallback_keywords") is False

    def test_summary_no_signal_recorded_when_short_circuit(self, base_config):
        """When both keyword sources are empty, _fetch_portal_search returns early
        before recording the fallback signal — summary stays clean."""
        base_config["sources"]["portal_search"]["keywords"] = []
        base_config["profile"] = {"target_titles": []}

        from job_finder.web.ingestion_runner import _fetch_portal_search

        summary: dict = {}
        _fetch_portal_search(base_config, summary)

        assert "portal_search_used_fallback_keywords" not in summary


# ---------------------------------------------------------------------------
# Stage 7.9 follow-up — per-portal breakdown in log_activity summary
# ---------------------------------------------------------------------------


class TestPerPortalBreakdown:
    """Closes the deferred-from-7.9 per-portal observability item.

    _fetch_portal_search now groups the merged Job list by Job.source
    (each fetcher tags its jobs with a unique `portal_<name>` value)
    and injects portal_<name>_fetched keys into the summary dict.
    _run_sync_bg's log_activity metadata composer scoops them up
    dynamically so adding a new portal doesn't require touching it.
    """

    @pytest.fixture
    def base_config(self):
        return {
            "sources": {
                "portal_search": {
                    "enabled": True,
                    "keywords": ["Staff Engineer"],
                    "max_serp_queries": 30,
                },
                "dataforseo": {"enabled": False},
                "google_cse": {"enabled": False},
            }
        }

    def _job(self, source: str, title: str = "x", company: str = "c"):
        from job_finder.models import Job

        return Job(
            title=title,
            company=company,
            location="Remote",
            source=source,
            source_url=f"https://example.com/{source}/{title}/{company}",
        )

    def test_per_portal_breakdown_in_summary(self, base_config):
        from job_finder.web.ingestion_runner import _fetch_portal_search

        mixed = [
            self._job("portal_remoteok", "t1"),
            self._job("portal_remoteok", "t2"),
            self._job("portal_himalayas", "t3"),
            self._job("portal_yc_workatastartup", "t4"),
            self._job("portal_yc_workatastartup", "t5"),
            self._job("portal_yc_workatastartup", "t6"),
        ]
        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=mixed,
        ):
            summary: dict = {}
            _fetch_portal_search(base_config, summary)

        assert summary["portal_search_fetched"] == 6
        assert summary["portal_remoteok_fetched"] == 2
        assert summary["portal_himalayas_fetched"] == 1
        assert summary["portal_yc_workatastartup_fetched"] == 3
        # Portals that returned 0 jobs are absent (reader uses .get(k, 0)).
        assert "portal_jobicy_fetched" not in summary
        assert "portal_remotive_fetched" not in summary

    def test_empty_jobs_produces_no_portal_keys(self, base_config):
        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ):
            summary: dict = {}
            _fetch_portal_search(base_config, summary)

        assert summary["portal_search_fetched"] == 0
        # No portal_<name>_fetched keys at all when there's nothing to count.
        portal_keys = [k for k in summary if k.startswith("portal_") and k.endswith("_fetched")]
        # Only portal_search_fetched (the aggregate) — no per-portal keys.
        assert portal_keys == ["portal_search_fetched"]

    def test_portal_search_logged_in_runs_table(self, base_config, migrated_db_path):
        """pipeline_runner emits a 'portal_search' row in runs when portal jobs fetched.

        Mirrors test_dataforseo_logged_in_runs_table — closes drift item #2 from
        the 2026-05-27 round-4 handoff. Before the fix, portal_search results
        never landed in the runs table, leaving `SELECT * FROM runs WHERE
        source='portal_search'` permanently empty regardless of fetch yield.
        """
        import sqlite3

        from job_finder.models import Job

        base_config["db"] = {"path": migrated_db_path}
        base_config["sources"].update(
            {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
            }
        )
        base_config["profile"] = {"target_titles": ["Staff Engineer"]}
        base_config["scoring"] = {"min_score_threshold": 0, "weights": {}}

        portal_jobs = [
            Job(
                title="Staff Engineer",
                company=f"PortalCo{i}",
                location="Remote",
                source="portal_jobicy",
                source_url=f"https://example.com/portal-job-{i}",
            )
            for i in range(2)
        ]
        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=portal_jobs,
        ):
            from job_finder.web.pipeline_runner import run_ingestion

            run_ingestion(migrated_db_path, base_config, score=False)

        conn = sqlite3.connect(migrated_db_path)
        row = conn.execute(
            "SELECT source, jobs_fetched FROM runs WHERE source = 'portal_search'"
        ).fetchone()
        conn.close()

        assert row is not None, "Expected a 'portal_search' entry in runs table"
        assert row[1] == 2

    def test_log_activity_scoops_dynamic_portal_keys(self):
        """_run_sync_bg's metadata composer picks up any portal_<name>_fetched
        key the fetcher produced, without needing the key enumerated explicitly."""
        # Direct unit of the composer logic — mirrors the loop in
        # blueprints/sync.py::_run_sync_bg. If sync.py drifts from this contract
        # the test catches it.
        summary = {
            "portal_search_fetched": 3,
            "portal_remoteok_fetched": 1,
            "portal_himalayas_fetched": 2,
            "portal_yc_workatastartup_fetched": 0,
            # Non-portal key — must NOT be scooped up.
            "gmail_fetched": 99,
        }
        metadata = {
            "portal_search_fetched": summary.get("portal_search_fetched", 0),
        }
        for key, value in summary.items():
            if key.startswith("portal_") and key.endswith("_fetched") and key not in metadata:
                metadata[key] = value

        assert metadata["portal_remoteok_fetched"] == 1
        assert metadata["portal_himalayas_fetched"] == 2
        assert metadata["portal_yc_workatastartup_fetched"] == 0
        assert "gmail_fetched" not in metadata
        # Pre-seeded keys not clobbered by the dynamic loop.
        assert metadata["portal_search_fetched"] == 3


# ---------------------------------------------------------------------------
# Stage 7.7 — upsert_job does NOT leak the scoring_provider DEFAULT 'anthropic'
# ---------------------------------------------------------------------------


class TestUpsertScoringProviderNotLeaked:
    """Regression guard for the migration 20 column-default leak.

    Migration 20 added `scoring_provider TEXT DEFAULT 'anthropic'`. When
    upsert_job INSERTs a new row without specifying scoring_provider,
    SQLite applies the DEFAULT — tagging every new row as scored by
    anthropic before any scorer has run.

    The fix in upsert_job (job_finder/db/_jobs.py) is to explicitly pass
    scoring_provider=NULL on INSERT. These tests are the canary.
    """

    def test_upsert_inserts_scoring_provider_null(self, migrated_db_path):
        """A freshly upserted row must NOT have the leaked 'anthropic' DEFAULT.

        Originally this test asserted ``scoring_provider IS NULL``. After the
        v3.0 cascade was wired, ``upsert_job`` started tagging the INSERT with
        ``'heuristic'`` to record that JobScorer (heuristic, runs inline at
        the ingestion boundary) produced the ``score`` column — see
        job_finder/db/_jobs.py:285-296. ``persist_job_assessment`` later
        overwrites scoring_provider via COALESCE when the LLM actually scores
        the row, so 'heuristic' is a legitimate "scored by heuristic, never
        reached the LLM" state, not a leak. The original threat (the migration
        20 DEFAULT='anthropic' silently tagging unscored rows) is still
        mitigated because the explicit INSERT value overrides DEFAULT, and
        that's what this test pins.
        """
        job = Job(
            title="Senior Data Scientist",
            company="LeakCheckCo",
            location="Remote",
            source="linkedin",
            source_url="https://linkedin.com/jobs/view/777/",
            source_id="777",
        )

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = upsert_job(conn, _as_parsed(job))
            assert result.kind == "inserted"
            row = conn.execute(
                "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = ?",
                (job.dedup_key,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["scoring_provider"] != "anthropic", (
            f"upsert_job leaked the migration 20 DEFAULT 'anthropic' onto a "
            f"never-LLM-scored row. Got scoring_provider={row['scoring_provider']!r}. "
            f"Check job_finder/db/_jobs.py INSERT column list — the explicit "
            f"value should override the column DEFAULT."
        )
        # scoring_provider should be 'heuristic' (JobScorer tagged it) or NULL.
        assert row["scoring_provider"] in (None, "heuristic")
        # scoring_model must be NULL until persist_job_assessment runs.
        assert row["scoring_model"] is None

    def test_upsert_does_not_overwrite_existing_scoring_provider(self, migrated_db_path):
        """Re-upsert of an already-scored job must not clobber a real attribution.

        Defense-in-depth: the INSERT-NULL fix only fires on the new-row branch.
        The UPDATE branch goes through merge_description / merge_sources and
        leaves scoring_provider alone. This test pins that contract so a
        future refactor that adds scoring_provider to the UPDATE column list
        doesn't accidentally null out real attributions.
        """
        job = Job(
            title="Senior Data Scientist",
            company="ReUpsertCo",
            location="Remote",
            source="linkedin",
            source_url="https://linkedin.com/jobs/view/888/",
            source_id="888",
        )

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        try:
            # INSERT (scoring_provider=NULL via 7.7 fix)
            upsert_job(conn, _as_parsed(job))
            # Simulate a real scoring attribution via the legitimate writer path.
            # The real LLM writer co-writes sub_scores_json + classification with
            # the model tag (m078 I-04/I-05 require them when scoring_model is set).
            conn.execute(
                "UPDATE jobs SET scoring_provider = ?, scoring_model = ?, "
                "sub_scores_json = ?, classification = ? WHERE dedup_key = ?",
                ("ollama", "qwen2.5:14b", '{"title_fit": 3}', "consider", job.dedup_key),
            )
            conn.commit()
            # Re-upsert (UPDATE branch fires because the row exists)
            upsert_job(conn, _as_parsed(job))
            row = conn.execute(
                "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = ?",
                (job.dedup_key,),
            ).fetchone()
        finally:
            conn.close()

        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"


# ---------------------------------------------------------------------------
# Stage 7.1 — _inject_portal_search_creds resolves USAJobs/Adzuna/Jooble
# creds through the secret stack (env > keyring > config-yaml plaintext)
# ---------------------------------------------------------------------------


class TestInjectPortalSearchCreds:
    """Regression guard for Stage 7.1's keyring-routing fix.

    Settings UI writes USAJobs/Adzuna/Jooble creds to OS keyring under
    canonical names mirroring the nested config path. _move_secret_to_keyring
    clears the plaintext leaf so the merged config no longer carries the
    secret. _fetch_portal_search must inject keyring-resolved values back
    into portal_cfg before handing it to fetch_all_portals, otherwise the
    fetchers see empty creds and skip.
    """

    def _portal_cfg(self, **overrides) -> dict:
        cfg = {
            "usajobs": {"enabled": True, "user_agent_email": "", "authorization_key": ""},
            "adzuna": {"enabled": True, "app_id": "", "app_key": "", "country": "us"},
            "jooble": {"enabled": True, "api_key": ""},
        }
        for k, v in overrides.items():
            cfg[k] = v
        return cfg

    def _full_cfg(self, portal_cfg: dict) -> dict:
        return {"sources": {"portal_search": portal_cfg}}

    def test_keyring_values_injected_when_plaintext_empty(self):
        """Cred fields empty in config (post-keyring-migration) → keyring values fill them."""
        import keyring

        from job_finder.web.ingestion_runner import _inject_portal_search_creds

        portal_cfg = self._portal_cfg()
        full_cfg = self._full_cfg(portal_cfg)

        keyring.set_password(
            "job-cannon", "sources.portal_search.usajobs.user_agent_email", "kr@x.com"
        )
        keyring.set_password(
            "job-cannon", "sources.portal_search.usajobs.authorization_key", "KR_KEY"
        )
        keyring.set_password("job-cannon", "sources.portal_search.adzuna.app_id", "KR_ID")
        keyring.set_password("job-cannon", "sources.portal_search.adzuna.app_key", "KR_APP_KEY")
        keyring.set_password("job-cannon", "sources.portal_search.jooble.api_key", "KR_JOOBLE")

        try:
            augmented = _inject_portal_search_creds(portal_cfg, full_cfg)
            assert augmented["usajobs"]["user_agent_email"] == "kr@x.com"
            assert augmented["usajobs"]["authorization_key"] == "KR_KEY"
            assert augmented["adzuna"]["app_id"] == "KR_ID"
            assert augmented["adzuna"]["app_key"] == "KR_APP_KEY"
            assert augmented["jooble"]["api_key"] == "KR_JOOBLE"
            # Original portal_cfg untouched (shallow-copy contract).
            assert portal_cfg["usajobs"]["user_agent_email"] == ""
            assert portal_cfg["jooble"]["api_key"] == ""
        finally:
            for name in (
                "sources.portal_search.usajobs.user_agent_email",
                "sources.portal_search.usajobs.authorization_key",
                "sources.portal_search.adzuna.app_id",
                "sources.portal_search.adzuna.app_key",
                "sources.portal_search.jooble.api_key",
            ):
                try:
                    keyring.delete_password("job-cannon", name)
                except Exception:
                    pass

    def test_plaintext_values_preserved_when_no_keyring_entry(self):
        """Legacy plaintext creds in config keep working when keyring is empty.

        Users who haven't re-saved via the Settings UI (or who run with
        config-yaml-only setups) must continue to ingest from the portals.
        """
        from job_finder.web.ingestion_runner import _inject_portal_search_creds

        portal_cfg = self._portal_cfg(
            usajobs={
                "enabled": True,
                "user_agent_email": "pt@x.com",
                "authorization_key": "PT_KEY",
            },
            adzuna={
                "enabled": True,
                "app_id": "PT_ID",
                "app_key": "PT_APP_KEY",
                "country": "us",
            },
            jooble={"enabled": True, "api_key": "PT_JOOBLE"},
        )
        augmented = _inject_portal_search_creds(portal_cfg, self._full_cfg(portal_cfg))
        # get_secret with config= falls back to _walk_config which returns the
        # plaintext value at the same dotted path.
        assert augmented["usajobs"]["user_agent_email"] == "pt@x.com"
        assert augmented["usajobs"]["authorization_key"] == "PT_KEY"
        assert augmented["adzuna"]["app_id"] == "PT_ID"
        assert augmented["adzuna"]["app_key"] == "PT_APP_KEY"
        assert augmented["jooble"]["api_key"] == "PT_JOOBLE"

    def test_disabled_portals_not_resolved(self):
        """Disabled portals skip the get_secret call entirely (no wasted reads)."""
        from job_finder.web.ingestion_runner import _inject_portal_search_creds

        portal_cfg = {
            "usajobs": {"enabled": False, "user_agent_email": "", "authorization_key": ""},
            "adzuna": {"enabled": False, "app_id": "", "app_key": "", "country": "us"},
            "jooble": {"enabled": False, "api_key": ""},
        }
        augmented = _inject_portal_search_creds(portal_cfg, self._full_cfg(portal_cfg))
        # Returned dict is a shallow copy; disabled portals not augmented
        # (in-place subtree reference is preserved).
        assert augmented["usajobs"] is portal_cfg["usajobs"]
        assert augmented["adzuna"] is portal_cfg["adzuna"]
        assert augmented["jooble"] is portal_cfg["jooble"]

    def test_env_var_wins_over_keyring_and_plaintext(self, monkeypatch):
        """get_secret precedence: env > keyring > config-yaml plaintext."""
        import keyring

        from job_finder.web.ingestion_runner import _inject_portal_search_creds

        portal_cfg = self._portal_cfg(
            jooble={"enabled": True, "api_key": "PLAINTEXT"},
        )
        keyring.set_password("job-cannon", "sources.portal_search.jooble.api_key", "KEYRING")
        monkeypatch.setenv("JOOBLE_API_KEY", "ENV_VAR_WINS")

        try:
            augmented = _inject_portal_search_creds(portal_cfg, self._full_cfg(portal_cfg))
            assert augmented["jooble"]["api_key"] == "ENV_VAR_WINS"
        finally:
            try:
                keyring.delete_password("job-cannon", "sources.portal_search.jooble.api_key")
            except Exception:
                pass

    def test_missing_portal_subtree_returns_empty_creds(self):
        """portal_cfg without a portal section (e.g. only enabled=False omitted) → empty resolved."""
        from job_finder.web.ingestion_runner import _inject_portal_search_creds

        portal_cfg: dict = {}  # no usajobs/adzuna/jooble at all
        augmented = _inject_portal_search_creds(portal_cfg, self._full_cfg(portal_cfg))
        # All three portals absent → not added (no enabled flag to trigger inject)
        assert "usajobs" not in augmented
        assert "adzuna" not in augmented
        assert "jooble" not in augmented


# ---------------------------------------------------------------------------
# Stage 7.6 — title-gate wiring through _fetch_portal_search
# ---------------------------------------------------------------------------


class TestFetchPortalSearchTitleGate:
    """Stage 7.6 — `profile.target_titles` + `exclusions.title_keywords` thread
    from config through `_fetch_portal_search` into `fetch_all_portals` so the
    portal-boundary title-gate fires. Closes the architectural gap where free
    portals' upstream ``q=`` full-text matching let off-target rows reach
    persistence + scoring (the apply-bias surfaced 2026-05-23 option-D run).
    """

    @pytest.fixture
    def gated_config(self):
        return {
            "sources": {
                "portal_search": {
                    "enabled": True,
                    "keywords": ["Data Scientist"],
                    "max_serp_queries": 30,
                },
                "dataforseo": {"enabled": False},
                "google_cse": {"enabled": False},
            },
            "profile": {
                "target_titles": ["Senior Data Scientist", "Analytics Manager"],
                "exclusions": {"title_keywords": ["Intern"]},
            },
        }

    def test_target_titles_threaded_to_fetch_all_portals(self, gated_config):
        """`profile.target_titles` reaches fetch_all_portals as kwarg."""
        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(gated_config, {})

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["target_titles"] == [
            "Senior Data Scientist",
            "Analytics Manager",
        ]
        assert kwargs["exclusions"] == ["Intern"]

    def test_no_profile_section_passes_empty_lists(self):
        """Missing profile entirely → empty target_titles + empty exclusions
        (gate becomes no-op inside fetch_all_portals)."""
        from job_finder.web.ingestion_runner import _fetch_portal_search

        cfg = {
            "sources": {
                "portal_search": {
                    "enabled": True,
                    "keywords": ["x"],
                    "max_serp_queries": 30,
                },
                "dataforseo": {"enabled": False},
                "google_cse": {"enabled": False},
            }
            # no `profile`
        }
        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(cfg, {})

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["target_titles"] == []
        assert kwargs["exclusions"] == []

    def test_empty_exclusions_section_passes_empty_list(self, gated_config):
        """`profile.exclusions = {}` → exclusions kwarg = []."""
        gated_config["profile"]["exclusions"] = {}
        from job_finder.web.ingestion_runner import _fetch_portal_search

        with patch(
            "job_finder.sources.portal_search_source.fetch_all_portals",
            return_value=[],
        ) as mock_fetch:
            _fetch_portal_search(gated_config, {})

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["exclusions"] == []

    def test_gate_is_end_to_end_against_real_fetch_all_portals(self, gated_config):
        """End-to-end through real fetch_all_portals: off-target dropped, on-target retained."""
        from job_finder.models import Job
        from job_finder.web.ingestion_runner import _fetch_portal_search

        def _job(title: str, src: str = "portal_himalayas") -> Job:
            return Job(
                title=title,
                company="Acme",
                location="Remote",
                source=src,
                source_url=f"https://example.com/{title}",
            )

        # _fetch_himalayas returns 3 jobs: 1 on-target, 1 off-target, 1 excluded.
        with (
            patch(
                "job_finder.sources.portal_search_source._fetch_himalayas",
                return_value=[
                    _job("Senior Data Scientist"),  # passes gate
                    _job("IT Service Lead"),  # off-target, dropped
                    _job("Senior Data Scientist Intern"),  # excluded
                ],
            ),
            patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[]),
            patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[]),
        ):
            summary: dict = {}
            jobs = _fetch_portal_search(gated_config, summary)

        # Only the on-target row survives — gate fired through the runner.
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Data Scientist"
        assert summary["portal_search_fetched"] == 1


# ---------------------------------------------------------------------------
# Stage 7.6 follow-up — _apply_title_gate helper + non-portal source gates.
#
# Mirrors the same invariant fetch_all_portals enforces (Stage 7.6). The
# helper is invoked inside every non-portal `_fetch_*` so summary["*_fetched"]
# reflects post-gate inventory — same dashboard semantics as Stage 7.6.
# ---------------------------------------------------------------------------


class TestApplyTitleGateHelper:
    """Unit tests for `_apply_title_gate` — the shared filter applied inside
    every non-portal ingestion fetcher."""

    def _jobs(self, *titles: str) -> list[Job]:
        return [
            Job(
                title=t,
                company="Acme",
                location="Remote",
                source="test",
                source_url=f"https://example.com/{i}",
            )
            for i, t in enumerate(titles)
        ]

    def test_empty_jobs_returns_empty(self):
        """Defensive — never iterate or filter when nothing was fetched."""
        from job_finder.web.ingestion_runner import _apply_title_gate

        config = {"profile": {"target_titles": ["Data Scientist"]}}
        assert _apply_title_gate([], config, "x") == []

    def test_no_target_titles_is_noop(self):
        """Empty target_titles preserves legacy bypass semantics — matches
        Stage 7.6 fetch_all_portals contract (target_titles=None passes all)."""
        from job_finder.web.ingestion_runner import _apply_title_gate

        config = {"profile": {"target_titles": []}}
        jobs = self._jobs("Senior Data Scientist", "Marketing Manager")
        result = _apply_title_gate(jobs, config, "x")
        assert result == jobs

    def test_missing_profile_section_is_noop(self):
        """No `profile` key at all → no-op (graceful when callers omit profile)."""
        from job_finder.web.ingestion_runner import _apply_title_gate

        jobs = self._jobs("Senior Data Scientist", "Marketing Manager")
        result = _apply_title_gate(jobs, {}, "x")
        assert result == jobs

    def test_off_target_jobs_filtered(self):
        """Jobs whose title fails `_title_matches` are dropped."""
        from job_finder.web.ingestion_runner import _apply_title_gate

        config = {"profile": {"target_titles": ["Data Scientist"]}}
        jobs = self._jobs(
            "Senior Data Scientist",  # passes
            "Marketing Manager",  # filtered
            "Sr DS",  # passes via abbreviation expansion
            "Database Admin",  # filtered — word-boundary distinguishes 'Data' from 'Database'
        )
        result = _apply_title_gate(jobs, config, "x")
        assert [j.title for j in result] == ["Senior Data Scientist", "Sr DS"]

    def test_exclusions_respected(self):
        """`profile.exclusions.title_keywords` filters even when target matches."""
        from job_finder.web.ingestion_runner import _apply_title_gate

        config = {
            "profile": {
                "target_titles": ["Data Scientist"],
                "exclusions": {"title_keywords": ["Intern"]},
            }
        }
        jobs = self._jobs("Senior Data Scientist", "Data Scientist Intern")
        result = _apply_title_gate(jobs, config, "x")
        assert [j.title for j in result] == ["Senior Data Scientist"]


class TestNonPortalIngestionTitleGate:
    """Stage 7.6 follow-up — gate fires inside each non-portal `_fetch_*`
    so off-target rows from gmail/imap/serpapi/thordata/dataforseo never
    reach persistence + scoring. Historical pass rate for these sources
    was 26-72%; the gate closes the same downstream leak Stage 7.6 closed
    for portal_search."""

    @pytest.fixture
    def gated_config(self):
        return {
            "sources": {
                "gmail": {"enabled": True, "lookback_days": 7},
                "imap": {
                    "enabled": True,
                    "email": "a@b.c",
                    "app_password": "p",
                    "host": "imap.gmail.com",
                    "port": 993,
                    "folder": "INBOX",
                },
                "serpapi": {
                    "enabled": True,
                    "api_key": "x",
                    "queries": [{"query": "ds"}],
                },
                "thordata": {
                    "enabled": True,
                    "api_key": "x",
                    "queries": [{"query": "ds"}],
                },
            },
            "profile": {
                "target_titles": ["Data Scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

    def _mixed_jobs(self, source: str) -> list[Job]:
        return [
            Job(
                title="Senior Data Scientist",
                company="Co1",
                location="Remote",
                source=source,
                source_url=f"https://x/{source}/1",
            ),
            Job(
                title="IT Service Lead",
                company="Co2",
                location="Remote",
                source=source,
                source_url=f"https://x/{source}/2",
            ),
        ]

    def test_serpapi_gate_drops_off_target(self, gated_config):
        """`_fetch_serpapi`: post-gate jobs + post-gate summary count."""
        from job_finder.web.ingestion_runner import _fetch_serpapi

        with patch("job_finder.sources.serpapi_source.SerpAPISource") as MockSerp:
            MockSerp.return_value.fetch_jobs.return_value = self._mixed_jobs("serpapi")
            summary: dict = {"serpapi_errors": []}
            jobs = _fetch_serpapi(gated_config, summary)

        assert [j.title for j in jobs] == ["Senior Data Scientist"]
        assert summary["serpapi_fetched"] == 1

    def test_thordata_gate_drops_off_target(self, gated_config):
        """`_fetch_thordata`: same shape as serpapi."""
        from job_finder.web.ingestion_runner import _fetch_thordata

        with patch("job_finder.sources.thordata_source.ThordataSource") as MockThor:
            MockThor.return_value.fetch_jobs.return_value = self._mixed_jobs("thordata")
            summary: dict = {"thordata_errors": []}
            jobs = _fetch_thordata(gated_config, summary)

        assert [j.title for j in jobs] == ["Senior Data Scientist"]
        assert summary["thordata_fetched"] == 1

    def test_gmail_gate_drops_off_target(self, gated_config, migrated_db_path):
        """`_fetch_gmail`: gate fires AFTER message-level dedup writes so
        email_parse_log still tracks every message ID, gate only filters Jobs."""
        import sqlite3

        from job_finder.web.ingestion_runner import _fetch_gmail

        conn = sqlite3.connect(migrated_db_path)
        with patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail:
            MockGmail.return_value.fetch_jobs.return_value = (
                self._mixed_jobs("gmail"),
                {"msg1", "msg2"},
            )
            MockGmail.return_value.parse_failures = []
            summary: dict = {"gmail_errors": []}
            jobs = _fetch_gmail(gated_config, conn, summary)

        assert [j.title for j in jobs] == ["Senior Data Scientist"]
        assert summary["gmail_fetched"] == 1
        # email_parse_log still tracks both raw messages — dedup unaffected.
        rows = conn.execute(
            "SELECT COUNT(*) FROM email_parse_log WHERE message_id IN ('msg1','msg2')"
        ).fetchone()
        assert rows[0] == 2
        conn.close()

    def test_imap_gate_drops_off_target(self, gated_config):
        """`_fetch_imap`: same shape as gmail but no email_parse_log writes."""
        from job_finder.web.ingestion_runner import _fetch_imap

        with patch("job_finder.web.ingestion_runner.ImapSource") as MockImap:
            MockImap.return_value.fetch_jobs.return_value = (
                self._mixed_jobs("imap"),
                {},
            )
            summary: dict = {"imap_errors": []}
            jobs = _fetch_imap(gated_config, summary)

        assert [j.title for j in jobs] == ["Senior Data Scientist"]
        assert summary["imap_fetched"] == 1

    def test_imap_missing_email_surfaces_error(self, gated_config):
        """`_fetch_imap`: empty `sources.imap.email` must NOT fail silently.

        Issue #19 risk callout — the registry-driven driver preserves the
        original inline check that surfaced missing IMAP credentials via
        ``summary['imap_errors']`` instead of returning `[]` without trace.
        """
        from job_finder.web.ingestion_runner import _fetch_imap

        cfg = {**gated_config}
        cfg["sources"] = {**gated_config["sources"]}
        cfg["sources"]["imap"] = {
            **gated_config["sources"]["imap"],
            "email": "",
        }
        summary: dict = {"imap_errors": []}
        jobs = _fetch_imap(cfg, summary)

        assert jobs == []
        assert summary["imap_errors"] == ["sources.imap.email is required"]

    def test_dataforseo_gate_drops_off_target_when_config_passed(self, gated_config):
        """`_collect_dataforseo_results`: gate fires when config kwarg is provided."""
        from job_finder.web.ingestion_runner import _collect_dataforseo_results

        mock_source = MagicMock()
        mock_source.collect_results.return_value = self._mixed_jobs("dataforseo")
        summary: dict = {"dataforseo_errors": []}
        jobs = _collect_dataforseo_results(mock_source, ["task1"], summary, config=gated_config)

        assert [j.title for j in jobs] == ["Senior Data Scientist"]
        assert summary["dataforseo_fetched"] == 1

    def test_dataforseo_no_gate_when_config_omitted(self):
        """`_collect_dataforseo_results`: legacy callers (no config kwarg)
        get bypass semantics — matches the rest of the module's defaults."""
        from job_finder.web.ingestion_runner import _collect_dataforseo_results

        mock_source = MagicMock()
        mock_source.collect_results.return_value = self._mixed_jobs("dataforseo")
        summary: dict = {"dataforseo_errors": []}
        # No `config=` kwarg → no gate.
        jobs = _collect_dataforseo_results(mock_source, ["task1"], summary)

        assert len(jobs) == 2
        assert summary["dataforseo_fetched"] == 2
