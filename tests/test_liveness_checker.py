"""Tests for URL liveness checker."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.liveness_checker import (
    LivenessStatus,
    check_url_liveness,
    run_liveness_check,
)


class TestCheckUrlLiveness:
    """Unit tests for check_url_liveness()."""

    def test_greenhouse_error_redirect(self):
        status, reason = check_url_liveness(
            "https://boards.greenhouse.io/acme/jobs/123?error=true"
        )
        assert status == LivenessStatus.EXPIRED
        assert reason == "greenhouse_error_redirect"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_200_with_apply_button(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html><body><button>Apply Now</button>" + "x" * 500 + "</body></html>"
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.ACTIVE
        assert reason == "apply_button_found"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_200_with_expired_text(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html><body>This job is no longer available" + "x" * 500 + "</body></html>"
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.EXPIRED
        assert "pattern:" in reason

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_404(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.EXPIRED
        assert reason == "http_404"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_410(self, mock_get):
        resp = MagicMock()
        resp.status_code = 410
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.EXPIRED
        assert reason == "http_410"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_500(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.ERROR
        assert reason == "http_500"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_403_blocked(self, mock_get):
        resp = MagicMock()
        resp.status_code = 403
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.UNCERTAIN
        assert reason == "http_403_blocked"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_timeout(self, mock_get):
        import requests
        mock_get.side_effect = requests.Timeout("timed out")

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.ERROR
        assert "timed out" in reason

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_empty_page(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html></html>"
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.EXPIRED
        assert reason == "empty_page"

    @patch("job_finder.web.liveness_checker.requests.get")
    def test_200_page_ok_no_apply(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html><body>" + "This is a normal job description page. " * 50 + "</body></html>"
        mock_get.return_value = resp

        status, reason = check_url_liveness("https://example.com/job/123")
        assert status == LivenessStatus.ACTIVE
        assert reason == "page_ok"


@pytest.fixture
def liveness_db(tmp_path):
    """Create a test DB with the required schema."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT NOT NULL,
        sources TEXT DEFAULT '[]',
        source_urls TEXT DEFAULT '[]',
        pipeline_status TEXT DEFAULT 'discovered',
        is_stale INTEGER DEFAULT 0,
        liveness_checked_at TEXT DEFAULT NULL,
        liveness_status TEXT DEFAULT NULL,
        liveness_reason TEXT DEFAULT NULL,
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE pipeline_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        old_status TEXT,
        new_status TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        evidence TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()
    return db_path


class TestRunLivenessCheck:
    """Integration tests for run_liveness_check()."""

    def _insert_job(self, db_path, dedup_key, source_urls=None, pipeline_status="discovered",
                    is_stale=0, liveness_checked_at=None):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, source_urls,
                                pipeline_status, is_stale, liveness_checked_at)
               VALUES (?, 'Test', 'TestCo', 'Remote', ?, ?, ?, ?)""",
            (dedup_key, json.dumps(source_urls or []), pipeline_status,
             is_stale, liveness_checked_at),
        )
        conn.commit()
        conn.close()

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_checks_eligible_jobs(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(liveness_db, "key1", ["https://example.com/1"])
        self._insert_job(liveness_db, "key2", ["https://example.com/2"])
        mock_check.return_value = (LivenessStatus.ACTIVE, "apply_button_found")

        result = run_liveness_check(liveness_db)
        assert result["checked"] == 2
        assert result["active"] == 2

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_skips_stale_jobs(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(liveness_db, "stale1", ["https://example.com/1"], is_stale=1)
        mock_check.return_value = (LivenessStatus.ACTIVE, "ok")

        result = run_liveness_check(liveness_db)
        assert result["checked"] == 0

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_skips_archived_jobs(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(liveness_db, "archived1", ["https://example.com/1"],
                         pipeline_status="archived")
        mock_check.return_value = (LivenessStatus.ACTIVE, "ok")

        result = run_liveness_check(liveness_db)
        assert result["checked"] == 0

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_expired_marks_stale(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(liveness_db, "exp1", ["https://example.com/1"])
        mock_check.return_value = (LivenessStatus.EXPIRED, "http_404")

        result = run_liveness_check(liveness_db)
        assert result["expired"] == 1

        conn = sqlite3.connect(liveness_db)
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key = 'exp1'").fetchone()
        assert job["is_stale"] == 1

        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = 'exp1'"
        ).fetchone()
        assert event is not None
        assert event["source"] == "liveness_checker"
        conn.close()

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_batch_limit(self, mock_sleep, mock_check, liveness_db):
        for i in range(5):
            self._insert_job(liveness_db, f"batch{i}", [f"https://example.com/{i}"])
        mock_check.return_value = (LivenessStatus.ACTIVE, "ok")

        result = run_liveness_check(liveness_db, config={"liveness": {"batch_limit": 2}})
        assert result["checked"] == 2

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_check_interval_respected(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(
            liveness_db, "recent1", ["https://example.com/1"],
            liveness_checked_at="2099-01-01T00:00:00",  # Far future
        )
        mock_check.return_value = (LivenessStatus.ACTIVE, "ok")

        result = run_liveness_check(liveness_db)
        assert result["checked"] == 0

    @patch("job_finder.web.liveness_checker.check_url_liveness")
    @patch("job_finder.web.liveness_checker.time.sleep")
    def test_empty_source_urls_skipped(self, mock_sleep, mock_check, liveness_db):
        self._insert_job(liveness_db, "empty1", [])
        mock_check.return_value = (LivenessStatus.ACTIVE, "ok")

        result = run_liveness_check(liveness_db)
        assert result["checked"] == 0
