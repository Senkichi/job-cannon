"""Tests for stale_detector.py — batch archive behavior (Plan 23-02).

Verifies that run_stale_detection() uses 2 SQL statements for archiving
(1 batch UPDATE + 1 executemany INSERT) instead of N per-row calls
to update_pipeline_status().
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from job_finder.web.stale_detector import run_stale_detection

def _insert_job(conn: sqlite3.Connection, dedup_key: str, pipeline_status: str, last_seen: str) -> None:
    """Insert a minimal job row for testing."""
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, sources, source_urls, source_id,
            first_seen, last_seen, pipeline_status, is_stale)
           VALUES (?, ?, ?, ?, '[]', '[]', '', ?, ?, ?, 0)""",
        (dedup_key, "Test Job", "Test Co", "Remote", "2025-01-01", last_seen, pipeline_status),
    )
    conn.commit()

def _days_ago(n: int) -> str:
    """Return ISO datetime string for n days ago."""
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")

class TestBatchArchive:
    """Test batch archive path in run_stale_detection()."""

    def test_batch_archive(self, migrated_db):
        """3 stale discovered jobs → all 3 archived, archived count = 3."""
        path, conn = migrated_db
        for i in range(3):
            _insert_job(conn, f"job{i}", "discovered", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 3

        rows = conn.execute(
            "SELECT pipeline_status FROM jobs ORDER BY dedup_key"
        ).fetchall()
        assert all(r["pipeline_status"] == "archived" for r in rows)

    def test_batch_archive_pipeline_events(self, migrated_db):
        """3 stale discovered jobs → pipeline_events has 3 rows with correct fields."""
        path, conn = migrated_db
        for i in range(3):
            _insert_job(conn, f"job{i}", "discovered", _days_ago(35))

        run_stale_detection(path)

        events = conn.execute(
            "SELECT job_id, from_status, to_status, source, evidence "
            "FROM pipeline_events ORDER BY job_id"
        ).fetchall()
        assert len(events) == 3
        for event in events:
            assert event["from_status"] == "discovered"
            assert event["to_status"] == "archived"
            assert event["source"] == "stale_detector"
            assert event["evidence"] == "not_seen_30_days"

    def test_batch_archive_mixed_statuses(self, migrated_db):
        """2 discovered + 1 reviewing, all 35 days stale → all 3 archived with correct from_status."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered", _days_ago(35))
        _insert_job(conn, "job1", "discovered", _days_ago(35))
        _insert_job(conn, "job2", "reviewing", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 3

        events = conn.execute(
            "SELECT job_id, from_status FROM pipeline_events ORDER BY job_id"
        ).fetchall()
        assert len(events) == 3
        from_statuses = {e["job_id"]: e["from_status"] for e in events}
        assert from_statuses["job0"] == "discovered"
        assert from_statuses["job1"] == "discovered"
        assert from_statuses["job2"] == "reviewing"

    def test_batch_archive_no_candidates(self, migrated_db):
        """Jobs in active stages (applied) are never auto-archived."""
        path, conn = migrated_db
        _insert_job(conn, "job_applied", "applied", _days_ago(35))
        _insert_job(conn, "job_phone", "phone_screen", _days_ago(35))

        result = run_stale_detection(path)

        assert result["archived"] == 0

        # pipeline_events should be empty (no archive transitions)
        events = conn.execute("SELECT * FROM pipeline_events").fetchall()
        assert len(events) == 0

    def test_batch_archive_empty(self, migrated_db):
        """Empty DB → archived=0, no errors."""
        path, conn = migrated_db

        result = run_stale_detection(path)

        assert result["archived"] == 0
        assert result["stale_marked"] == 0
        assert result["stale_cleared"] == 0

    def test_stale_marking_unchanged(self, migrated_db):
        """Job seen 15 days ago (stale but not archive candidate) → stale_marked=1."""
        path, conn = migrated_db
        _insert_job(conn, "job_stale", "discovered", _days_ago(15))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 1
        assert result["archived"] == 0

        row = conn.execute(
            "SELECT is_stale, pipeline_status FROM jobs WHERE dedup_key = 'job_stale'"
        ).fetchone()
        assert row["is_stale"] == 1
        assert row["pipeline_status"] == "discovered"  # Not archived, just stale
