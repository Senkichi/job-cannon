"""Tests for stale_detector.py — batch archive behavior (Plan 23-02).

Verifies that run_stale_detection() uses 2 SQL statements for archiving
(1 batch UPDATE + 1 executemany INSERT) instead of N per-row calls
to update_pipeline_status().
"""

import sqlite3
from datetime import datetime, timedelta

from job_finder.web.stale_detector import run_stale_detection


def _insert_job(
    conn: sqlite3.Connection, dedup_key: str, pipeline_status: str, last_seen: str
) -> None:
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

        rows = conn.execute("SELECT pipeline_status FROM jobs ORDER BY dedup_key").fetchall()
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


class TestPassiveStageScoping:
    """is_stale is only meaningful pre-application: marked on passive stages,
    cleared everywhere else."""

    def test_active_stage_jobs_never_marked_stale(self, migrated_db):
        """Applied/phone_screen jobs seen 15+ days ago are NOT marked stale."""
        path, conn = migrated_db
        _insert_job(conn, "job_applied", "applied", _days_ago(20))
        _insert_job(conn, "job_phone", "phone_screen", _days_ago(20))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0
        rows = conn.execute("SELECT is_stale FROM jobs").fetchall()
        assert all(r["is_stale"] == 0 for r in rows)

    def test_stale_flag_cleared_when_job_leaves_passive_stage(self, migrated_db):
        """A stale discovered job that the user applies to sheds its stale flag."""
        path, conn = migrated_db
        _insert_job(conn, "job_now_applied", "applied", _days_ago(20))
        conn.execute("UPDATE jobs SET is_stale = 1 WHERE dedup_key = 'job_now_applied'")
        conn.commit()

        result = run_stale_detection(path)

        assert result["stale_cleared"] == 1
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'job_now_applied'"
        ).fetchone()
        assert row["is_stale"] == 0

    def test_reseen_job_cleared(self, migrated_db):
        """A stale job re-seen recently is cleared (original re-sighting rule)."""
        path, conn = migrated_db
        _insert_job(conn, "job_reseen", "discovered", _days_ago(2))
        conn.execute("UPDATE jobs SET is_stale = 1 WHERE dedup_key = 'job_reseen'")
        conn.commit()

        result = run_stale_detection(path)

        assert result["stale_cleared"] == 1
        row = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key = 'job_reseen'").fetchone()
        assert row["is_stale"] == 0


class TestConfigurableThresholds:
    """staleness.stale_threshold_days / archive_threshold_days override defaults."""

    def test_custom_stale_threshold(self, migrated_db):
        """stale_threshold_days=5 marks a job seen 7 days ago (default 14 would not)."""
        path, conn = migrated_db
        _insert_job(conn, "job_week", "discovered", _days_ago(7))

        config = {"staleness": {"stale_threshold_days": 5}}
        result = run_stale_detection(path, config)

        assert result["stale_marked"] == 1

    def test_custom_archive_threshold(self, migrated_db):
        """archive_threshold_days=10 archives a job seen 12 days ago, with
        threshold-accurate evidence."""
        path, conn = migrated_db
        _insert_job(conn, "job_old", "discovered", _days_ago(12))

        config = {"staleness": {"archive_threshold_days": 10}}
        result = run_stale_detection(path, config)

        assert result["archived"] == 1
        event = conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'job_old'"
        ).fetchone()
        assert event["evidence"] == "not_seen_10_days"

    def test_defaults_without_config(self, migrated_db):
        """No config → 14/30 defaults: a 7-day-old sighting is neither stale nor archived."""
        path, conn = migrated_db
        _insert_job(conn, "job_fresh", "discovered", _days_ago(7))

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0
        assert result["archived"] == 0
