"""Tests for job expiry detection signal cascade."""

import json
import sqlite3
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest
import requests


class TestExtractPostingId:
    """_extract_posting_id extracts individual posting IDs from ATS URLs."""

    def test_lever_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.lever.co/acme-corp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "lever") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_greenhouse_numeric_id(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://boards.greenhouse.io/acme/jobs/4567890"
        assert _extract_posting_id(url, "greenhouse") == "4567890"

    def test_ashby_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.ashbyhq.com/AcmeCorp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "ashby") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_returns_none_for_non_matching_url(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://www.linkedin.com/jobs/view/12345/"
        assert _extract_posting_id(url, "lever") is None

    def test_returns_none_for_unknown_platform(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.lever.co/acme/abc123"
        assert _extract_posting_id(url, "unknown") is None


class TestCheckAtsApi:
    """Signal 1: ATS API liveness check."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, EXPIRED
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=404)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == EXPIRED
        mock_get.assert_called_once()
        assert "api.lever.co" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_200_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, LIVE
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=200)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_greenhouse_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, EXPIRED
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=404)
        result = _check_ats_api("acme", "12345", "greenhouse")
        assert result == EXPIRED
        assert "boards-api.greenhouse.io" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_network_error_returns_inconclusive(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, INCONCLUSIVE
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == INCONCLUSIVE

    def test_unknown_platform_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_ats_api, INCONCLUSIVE
        result = _check_ats_api("acme", "abc-123", "unknown")
        assert result == INCONCLUSIVE


class TestCheckCareersPage:
    """Signal 2: Company careers page title search."""

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_found_returns_live(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import _check_careers_page, LIVE
        mock_find.return_value = "https://acme.com/careers"
        # scrape_careers_page returns list of dicts with 'title' and 'url' keys
        mock_scrape.return_value = [{"title": "Senior Data Scientist", "url": "https://acme.com/careers/123"}]
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_not_found_returns_inconclusive(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        mock_find.return_value = "https://acme.com/careers"
        mock_scrape.return_value = [{"title": "Backend Engineer", "url": "https://acme.com/careers/456"}]
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_no_careers_url_returns_inconclusive(self, mock_find):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        mock_find.return_value = None
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE

    def test_no_homepage_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        result = _check_careers_page(None, "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE


class TestCheckSerpapi:
    """Signal 3: SerpAPI re-search fallback."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_no_match_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_serpapi, EXPIRED
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"jobs_results": [
                {"title": "Backend Engineer", "company_name": "OtherCo"},
            ]}),
        )
        config = {"sources": {"serpapi": {"enabled": True, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_match_found_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import _check_serpapi, LIVE
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"jobs_results": [
                {"title": "Senior Data Scientist", "company_name": "Acme Corp"},
            ]}),
        )
        config = {"sources": {"serpapi": {"enabled": True, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == LIVE

    def test_serpapi_disabled_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_serpapi, INCONCLUSIVE
        config = {"sources": {"serpapi": {"enabled": False, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == INCONCLUSIVE

    def test_serpapi_no_key_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_serpapi, INCONCLUSIVE
        config = {"sources": {"serpapi": {"enabled": True, "api_key": ""}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == INCONCLUSIVE


class TestSignalCascade:
    """_check_job_expiry runs signals in order and short-circuits."""

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_ats_expired_short_circuits(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, EXPIRED
        mock_ats.return_value = EXPIRED
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == EXPIRED
        assert "lever" in evidence.lower() or "ats" in evidence.lower()
        mock_careers.assert_not_called()
        mock_serpapi.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_ats_inconclusive_falls_through_to_careers(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, INCONCLUSIVE, LIVE
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = LIVE
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == LIVE
        mock_careers.assert_called_once()
        mock_serpapi.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_all_inconclusive_returns_inconclusive(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, INCONCLUSIVE
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = INCONCLUSIVE
        mock_serpapi.return_value = INCONCLUSIVE
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == INCONCLUSIVE


class TestRunExpiryCheck:
    """run_expiry_check batch runner queries DB and processes jobs."""

    def _setup_db(self, path):
        """Create a migrated DB with test jobs and companies."""
        from job_finder.web.db_migrate import run_migrations
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row

        # Insert a company with ATS info
        conn.execute(
            "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, ats_slug, "
            "ats_probe_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme", "Acme Corp", "https://acme.com", "lever", "acme-corp",
             "hit", "2026-03-01", "2026-03-01"),
        )
        company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert a discovered job linked to company
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id, source_urls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme|ds|remote", "Data Scientist", "Acme Corp", "Remote",
             "2026-03-01", "2026-03-10", "discovered", company_id,
             '["https://jobs.lever.co/acme-corp/abc-123-def"]'),
        )

        # Insert an applied job (should NOT be checked)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme|sde|remote", "Software Engineer", "Acme Corp", "Remote",
             "2026-03-01", "2026-03-10", "applied", company_id),
        )
        conn.commit()
        conn.close()
        return path

    @patch("job_finder.web.expiry_checker.time.sleep")
    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_archives_expired_job(self, mock_check, mock_sleep, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result = run_expiry_check(tmp_db_path, config)

        assert result["archived"] >= 1

        # Verify the job was actually archived
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)).fetchone()
        assert row["pipeline_status"] == "archived"
        conn.close()

    @patch("job_finder.web.expiry_checker.time.sleep")
    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_does_not_touch_applied_jobs(self, mock_check, mock_sleep, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        run_expiry_check(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|sde|remote",)).fetchone()
        assert row["pipeline_status"] == "applied"
        conn.close()

    @patch("job_finder.web.expiry_checker.time.sleep")
    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_updates_expiry_checked_at_on_live(self, mock_check, mock_sleep, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("live", "lever_api 200")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        run_expiry_check(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT expiry_checked_at FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)).fetchone()
        assert row["expiry_checked_at"] is not None
        conn.close()

    @patch("job_finder.web.expiry_checker.time.sleep")
    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_skips_recently_checked_jobs(self, mock_check, mock_sleep, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)

        # Set expiry_checked_at to now (recently checked)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
            (datetime.now(timezone.utc).isoformat(), "acme|ds|remote"),
        )
        conn.commit()
        conn.close()

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}, "expiry": {"recheck_days": 3}}
        run_expiry_check(tmp_db_path, config)

        mock_check.assert_not_called()


class TestCareersBackoff:
    """_record_careers_outcome tracks failures and sets skip-until timestamps."""

    def setup_method(self):
        """Reset module-level backoff state before each test."""
        from job_finder.web import expiry_checker
        expiry_checker._careers_failure_counts.clear()
        expiry_checker._careers_skip_until.clear()

    def test_three_consecutive_failures_trigger_skip(self):
        from job_finder.web.expiry_checker import _record_careers_outcome, _careers_skip_until
        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert 42 not in _careers_skip_until  # Not yet at threshold
        _record_careers_outcome(42, success=False)
        assert 42 in _careers_skip_until  # Now at threshold
        skip_time = _careers_skip_until[42]
        # Should be ~7 days from now
        now = datetime.now(timezone.utc)
        assert skip_time > now + timedelta(days=6)
        assert skip_time < now + timedelta(days=8)

    def test_success_resets_failure_count(self):
        from job_finder.web.expiry_checker import _record_careers_outcome, _careers_failure_counts, _careers_skip_until
        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert _careers_failure_counts.get(42, 0) == 2
        _record_careers_outcome(42, success=True)
        assert 42 not in _careers_failure_counts
        assert 42 not in _careers_skip_until


class TestAutoReopen:
    """Archived jobs re-appearing during ingestion are auto-reopened."""

    def test_archived_job_reopened_on_upsert(self, tmp_db_path):
        """upsert_job for an existing archived job sets pipeline_status to discovered."""
        import sqlite3
        from job_finder.web.db_migrate import run_migrations
        from job_finder.db import upsert_job
        from job_finder.models import Job

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        # Insert a job and set it to archived
        job = Job(
            title="Data Scientist", company="Acme Corp", location="Remote",
            source="linkedin", source_url="https://linkedin.com/jobs/1",
        )
        upsert_job(conn, job)
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()

        # Re-ingest the same job (simulates re-appearance in Gmail/SerpAPI)
        is_new = upsert_job(conn, job)
        assert is_new is False  # existing job, not new

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
        assert row["pipeline_status"] == "discovered"

        # Verify evidence was recorded in pipeline_events
        event = conn.execute(
            "SELECT evidence, source FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC LIMIT 1",
            (job.dedup_key,),
        ).fetchone()
        assert event["evidence"] == "re_appeared"
        assert event["source"] == "ingestion"

        conn.close()

    def test_non_archived_job_not_reopened(self, tmp_db_path):
        """upsert_job for an existing reviewing job does NOT change pipeline_status."""
        import sqlite3
        from job_finder.web.db_migrate import run_migrations
        from job_finder.db import upsert_job
        from job_finder.models import Job

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        job = Job(
            title="Data Scientist", company="Acme Corp", location="Remote",
            source="linkedin", source_url="https://linkedin.com/jobs/1",
        )
        upsert_job(conn, job)
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'reviewing' WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()

        upsert_job(conn, job)

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
        assert row["pipeline_status"] == "reviewing"  # unchanged

        conn.close()
