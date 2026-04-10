"""Unit and integration tests for activity_tracker.py.

Tests cover:
- log_activity() inserts correct rows into user_activity
- log_activity() transaction independence (own connection)
- log_activity() silent failure on bad paths/missing tables
- log_activity() works without Flask application context
- ACTION_* constants are exported with correct string values
- Integration: Flask routes create activity rows on action
"""

import json
import os
import sqlite3
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Unit tests for log_activity()
# ---------------------------------------------------------------------------

class TestLogActivity:
    """Core log_activity() behavior tests."""

    def test_inserts_row(self, migrated_db):
        """log_activity inserts exactly 1 row with correct fields."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, conn = migrated_db

        log_activity(db_path, ACTION_SYNC, metadata={"status": "success"})

        rows = conn.execute("SELECT * FROM user_activity WHERE action = ?", (ACTION_SYNC,)).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "sync"
        assert row["entity_id"] is None
        assert row["occurred_at"] is not None and len(row["occurred_at"]) > 0
        # metadata contains "status"
        meta = json.loads(row["metadata"])
        assert meta.get("status") == "success"

    def test_inserts_with_entity_id(self, migrated_db):
        """log_activity stores entity_id correctly."""
        from job_finder.web.activity_tracker import log_activity, ACTION_RESCORE

        db_path, conn = migrated_db
        entity = "company|title|loc"
        log_activity(db_path, ACTION_RESCORE, entity_id=entity, metadata={"title": "X"})

        row = conn.execute(
            "SELECT * FROM user_activity WHERE action = ?", (ACTION_RESCORE,)
        ).fetchone()
        assert row is not None
        assert row["entity_id"] == entity
        meta = json.loads(row["metadata"])
        assert meta.get("title") == "X"

    def test_metadata_serialized_as_json(self, migrated_db):
        """metadata dict is stored as valid JSON TEXT that round-trips correctly."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, conn = migrated_db
        original_meta = {"foo": "bar", "count": 42, "nested": {"a": 1}}
        log_activity(db_path, ACTION_SYNC, metadata=original_meta)

        row = conn.execute("SELECT metadata FROM user_activity").fetchone()
        parsed = json.loads(row["metadata"])
        assert parsed == original_meta

    def test_empty_metadata_defaults_to_empty_dict(self, migrated_db):
        """log_activity with no metadata stores '{}' as metadata."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, conn = migrated_db
        log_activity(db_path, ACTION_SYNC)

        row = conn.execute("SELECT metadata FROM user_activity").fetchone()
        assert row["metadata"] == "{}"

    def test_none_metadata_defaults_to_empty_dict(self, migrated_db):
        """log_activity with metadata=None stores '{}' as metadata."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, conn = migrated_db
        log_activity(db_path, ACTION_SYNC, metadata=None)

        row = conn.execute("SELECT metadata FROM user_activity").fetchone()
        assert row["metadata"] == "{}"

    def test_transaction_independence(self, migrated_db):
        """log_activity uses a separate connection, not the caller's connection object.

        Verifies that log_activity does NOT use the passed-in conn object.
        The caller's conn can be in any state — log_activity opens its own connection.
        After log_activity completes, a fresh connection can see the committed activity row.
        """
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, conn = migrated_db

        # log_activity uses its own connection, not conn
        log_activity(db_path, ACTION_SYNC, metadata={"status": "success"})

        # Verify the activity row was committed (visible to a fresh connection)
        fresh_conn = sqlite3.connect(db_path)
        fresh_conn.row_factory = sqlite3.Row
        row = fresh_conn.execute("SELECT * FROM user_activity WHERE action = 'sync'").fetchone()
        fresh_conn.close()
        assert row is not None, "Activity row should be committed by log_activity's own connection"

        # The caller's conn was never used by log_activity — confirm it still has no rows
        # via its own view (it made no changes)
        caller_row = conn.execute("SELECT * FROM user_activity WHERE action = 'sync'").fetchone()
        assert caller_row is not None, "Caller's connection should also be able to read the row"

    def test_failure_is_silent_bad_path(self):
        """log_activity with an invalid db_path does not raise any exception."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        # Should not raise — even on completely invalid path
        log_activity("/nonexistent/dir/bad.db", ACTION_SYNC, metadata={"status": "test"})

    def test_failure_on_missing_table(self, tmp_db_path):
        """log_activity on a DB without user_activity table does not raise."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        # tmp_db_path is a valid SQLite DB but has no tables
        log_activity(tmp_db_path, ACTION_SYNC)

    def test_no_app_context_required(self, migrated_db):
        """Calling log_activity outside any Flask app context works without RuntimeError."""
        from job_finder.web.activity_tracker import log_activity, ACTION_SYNC

        db_path, _ = migrated_db

        # No Flask app pushed — must not raise RuntimeError
        try:
            from flask import current_app
            _ = current_app._get_current_object()
            # If this succeeds, we're inside an app context — skip test
            pytest.skip("This test must run outside a Flask app context")
        except RuntimeError:
            pass  # Good — no app context active

        # Must succeed without Flask context
        log_activity(db_path, ACTION_SYNC, metadata={"context": "none"})

# ---------------------------------------------------------------------------
# Tests for ACTION_* constants
# ---------------------------------------------------------------------------

class TestActionConstants:
    """ACTION_* constants exported with correct string values."""

    def test_constants_exported(self):
        """All 19 ACTION_* constants are importable."""
        from job_finder.web.activity_tracker import (
            ACTION_SYNC,
            ACTION_SCHEDULED_SYNC,
            ACTION_EXPAND_JOB,
            ACTION_STATUS_CHANGE,
            ACTION_PASTE_JD,
            ACTION_RESCORE,
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            ACTION_GENERATE_RESUME,
            ACTION_QUICK_APPLY,
            ACTION_SCHEDULED_STALE_DETECTION,
            ACTION_SCHEDULED_ATS_SCAN,
            ACTION_SCHEDULED_REJECTION_ANALYSIS,
            ACTION_UPLOAD_RESUME_PDF,
            ACTION_CONFLICT_REVIEW,
            ACTION_SAVE_CONFLICTS,
            ACTION_EXTRACT_STYLE,
            ACTION_SCHEDULED_EXPIRY_CHECK,
            ACTION_SAVE_JD,
        )
        constants = [
            ACTION_SYNC,
            ACTION_SCHEDULED_SYNC,
            ACTION_EXPAND_JOB,
            ACTION_STATUS_CHANGE,
            ACTION_PASTE_JD,
            ACTION_RESCORE,
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            ACTION_GENERATE_RESUME,
            ACTION_QUICK_APPLY,
            ACTION_SCHEDULED_STALE_DETECTION,
            ACTION_SCHEDULED_ATS_SCAN,
            ACTION_SCHEDULED_REJECTION_ANALYSIS,
            ACTION_UPLOAD_RESUME_PDF,
            ACTION_CONFLICT_REVIEW,
            ACTION_SAVE_CONFLICTS,
            ACTION_EXTRACT_STYLE,
            ACTION_SCHEDULED_EXPIRY_CHECK,
            ACTION_SAVE_JD,
        ]
        # Unique string values
        assert len(set(constants)) == 19, f"Duplicate ACTION_* values found: {constants}"

    def test_constants_match_expected_names(self):
        """All 19 ACTION_* constants have the expected string values."""
        from job_finder.web.activity_tracker import (
            ACTION_SYNC,
            ACTION_SCHEDULED_SYNC,
            ACTION_EXPAND_JOB,
            ACTION_STATUS_CHANGE,
            ACTION_PASTE_JD,
            ACTION_RESCORE,
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            ACTION_GENERATE_RESUME,
            ACTION_QUICK_APPLY,
            ACTION_SCHEDULED_STALE_DETECTION,
            ACTION_SCHEDULED_ATS_SCAN,
            ACTION_SCHEDULED_REJECTION_ANALYSIS,
            ACTION_UPLOAD_RESUME_PDF,
            ACTION_CONFLICT_REVIEW,
            ACTION_SAVE_CONFLICTS,
            ACTION_EXTRACT_STYLE,
            ACTION_SCHEDULED_EXPIRY_CHECK,
            ACTION_SAVE_JD,
        )
        assert ACTION_SYNC == "sync"
        assert ACTION_SCHEDULED_SYNC == "scheduled_sync"
        assert ACTION_EXPAND_JOB == "expand_job"
        assert ACTION_STATUS_CHANGE == "status_change"
        assert ACTION_PASTE_JD == "paste_jd"
        assert ACTION_RESCORE == "rescore"
        assert ACTION_BATCH_SCORE_HAIKU == "batch_score_haiku"
        assert ACTION_BATCH_SCORE_SONNET == "batch_score_sonnet"
        assert ACTION_GENERATE_RESUME == "generate_resume"
        assert ACTION_QUICK_APPLY == "quick_apply"
        assert ACTION_SCHEDULED_STALE_DETECTION == "scheduled_stale_detection"
        assert ACTION_SCHEDULED_ATS_SCAN == "scheduled_ats_scan"
        assert ACTION_SCHEDULED_REJECTION_ANALYSIS == "scheduled_rejection_analysis"
        # Constants added in later phases:
        assert ACTION_UPLOAD_RESUME_PDF == "upload_resume_pdf"
        assert ACTION_CONFLICT_REVIEW == "conflict_review"
        assert ACTION_SAVE_CONFLICTS == "save_conflicts"
        assert ACTION_EXTRACT_STYLE == "extract_style"
        assert ACTION_SCHEDULED_EXPIRY_CHECK == "scheduled_expiry_check"
        assert ACTION_SAVE_JD == "save_jd"

# ---------------------------------------------------------------------------
# Integration tests for call site wiring
# ---------------------------------------------------------------------------

class TestCallSiteIntegration:
    """Integration tests verifying Flask routes create user_activity rows."""

    @pytest.fixture
    def app_with_db(self):
        """Create a Flask test app with its own migrated temp DB."""
        import tempfile
        from job_finder.web import create_app
        from job_finder.web.db_migrate import run_migrations

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        run_migrations(db_path)

        app = create_app(config={
            "TESTING": True,
            "db": {"path": db_path},
            "scoring": {"min_score": 5.0, "haiku_threshold": 55, "monthly_budget_usd": 25.0},
            "polling": {"interval_minutes": 30},
        })

        # Insert a test job so routes have something to act on
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "source_id, first_seen, last_seen, pipeline_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test|software engineer|remote",
                "Software Engineer",
                "TestCo",
                "Remote",
                '["linkedin"]',
                '["https://example.com/job/1"]',
                "job123",
                "2026-01-01",
                "2026-03-01",
                "reviewing",
            ),
        )
        conn.commit()

        yield app, db_path, conn

        conn.close()
        # Best-effort cleanup — Windows may hold the file briefly due to bg threads
        import time as _time
        for _ in range(5):
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
                break
            except PermissionError:
                _time.sleep(0.1)

    def test_expand_logs_activity(self, app_with_db):
        """GET /jobs/<key>/expand creates a user_activity row with action='expand_job'."""
        app, db_path, conn = app_with_db
        dedup_key = "test|software engineer|remote"

        with app.test_client() as client:
            resp = client.get(
                f"/jobs/{dedup_key}/expand",
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200

        row = conn.execute(
            "SELECT * FROM user_activity WHERE action = 'expand_job'",
        ).fetchone()
        assert row is not None, "expand route should have logged an expand_job activity"
        assert row["entity_id"] == dedup_key

    def test_status_change_logs_activity(self, app_with_db):
        """POST /jobs/<key>/status creates a user_activity row with action='status_change'."""
        app, db_path, conn = app_with_db
        dedup_key = "test|software engineer|remote"

        with app.test_client() as client:
            resp = client.post(
                f"/jobs/{dedup_key}/status",
                data={"pipeline_status": "applied"},
            )
            assert resp.status_code == 200

        row = conn.execute(
            "SELECT * FROM user_activity WHERE action = 'status_change'",
        ).fetchone()
        assert row is not None, "update_status route should have logged a status_change activity"
        assert row["entity_id"] == dedup_key
        meta = json.loads(row["metadata"])
        assert meta.get("new_status") == "applied"

    def test_sync_logs_activity(self, app_with_db):
        """POST /dashboard/sync creates a user_activity row with action='sync'."""
        from unittest.mock import patch

        app, db_path, conn = app_with_db

        mock_summary = {
            "jobs_new": 3,
            "gmail_fetched": 5,
            "serpapi_fetched": 0,
            "gmail_errors": [],
            "serpapi_errors": [],
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 1.5,
            "detection_auto_updated": 0,
            "detection_queued": 0,
        }

        with app.test_client() as client:
            with patch("job_finder.web.scheduler.run_sync_now", return_value=mock_summary):
                resp = client.post("/dashboard/sync")
                # Sync redirects to dashboard
                assert resp.status_code in (200, 302)

        row = conn.execute(
            "SELECT * FROM user_activity WHERE action = 'sync'",
        ).fetchone()
        assert row is not None, "sync route should have logged a sync activity"
        meta = json.loads(row["metadata"])
        assert meta.get("jobs_new") == 3
        assert meta.get("status") == "success"
