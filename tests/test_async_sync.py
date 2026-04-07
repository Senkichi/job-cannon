"""Tests for async sync routes — POST /dashboard/sync/start and GET /dashboard/sync/status/{id}.

Follows the pattern from test_activity_tracker.py:TestCallSiteIntegration.
Uses a tempfile DB with full migrations, and TESTING=True to skip the background thread.
"""

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations


class TestAsyncSync:
    """Tests for async sync start/status routes and duplicate guard."""

    @pytest.fixture
    def app_with_db(self):
        """Create a Flask test app with its own migrated temp DB."""
        from job_finder.web import create_app

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        run_migrations(db_path)

        app = create_app(config={
            "TESTING": True,
            "db": {"path": db_path},
            "scoring": {"min_score": 5.0, "haiku_threshold": 55, "daily_budget_usd": 25.0},
            "polling": {"interval_minutes": 30},
        })
        # Set Flask's native TESTING flag so routes skip background threads.
        app.config["TESTING"] = True

        yield app, db_path

        # Windows: SQLite WAL mode keeps the file locked briefly after test.
        # Suppress PermissionError on cleanup — temp file will be cleaned by OS.
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except PermissionError:
            pass

    def test_sync_start_inserts_session_row(self, app_with_db):
        """POST /dashboard/sync/start inserts batch_score_sessions row with session_type='sync'."""
        app, db_path = app_with_db
        with app.test_client() as client:
            resp = client.post("/dashboard/sync/start")
        assert resp.status_code == 200

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE session_type='sync'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["session_type"] == "sync"
        assert row["status"] == "running"

    def test_sync_start_returns_progress_fragment(self, app_with_db):
        """POST /dashboard/sync/start returns 200 with HTML containing hx-trigger and every 2s."""
        app, db_path = app_with_db
        with app.test_client() as client:
            resp = client.post("/dashboard/sync/start")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "hx-trigger" in body
        assert "every 2s" in body

    def test_sync_start_duplicate_guard(self, app_with_db):
        """POST /dashboard/sync/start when a sync is already running returns 'already running'."""
        app, db_path = app_with_db

        # Insert a non-terminal sync session to simulate an in-progress sync
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('sync', 'running', 0, 0, '2026-03-25T12:00:00')"
        )
        conn.commit()
        conn.close()

        with app.test_client() as client:
            resp = client.post("/dashboard/sync/start")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "already running" in body.lower()

    def test_sync_status_running_returns_progress_fragment(self, app_with_db):
        """GET /dashboard/sync/status/{id} for a running session returns progress with hx-trigger."""
        from datetime import datetime, timezone
        app, db_path = app_with_db

        # Use a recent timestamp to avoid triggering the 30-minute timeout
        recent_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('sync', 'running', 0, 0, ?)",
            (recent_time,)
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/dashboard/sync/status/{session_id}")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "hx-trigger" in body
        assert "every 2s" in body

    def test_sync_status_done_returns_done_fragment(self, app_with_db):
        """GET /dashboard/sync/status/{id} for a done session returns done fragment (no polling)."""
        app, db_path = app_with_db

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions "
            "(session_type, status, total, scored, skipped, started_at, finished_at) "
            "VALUES ('sync', 'done', 10, 3, 0, '2026-03-25T12:00:00', '2026-03-25T12:01:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/dashboard/sync/status/{session_id}")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Done fragment should have success styling
        assert "text-emerald-400" in body
        # Done fragment should NOT have polling trigger
        assert 'hx-trigger="every' not in body

    def test_sync_status_error_returns_error_fragment(self, app_with_db):
        """GET /dashboard/sync/status/{id} for an error session returns error fragment."""
        app, db_path = app_with_db

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions "
            "(session_type, status, total, scored, skipped, started_at, finished_at, error_msg) "
            "VALUES ('sync', 'error', 0, 0, 0, '2026-03-25T12:00:00', '2026-03-25T12:01:00', 'Connection failed')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/dashboard/sync/status/{session_id}")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "text-red-400" in body

    def test_sync_status_timeout_marks_error(self, app_with_db):
        """GET /dashboard/sync/status/{id} for a session started >30 min ago triggers timeout."""
        app, db_path = app_with_db

        # Insert a session started 35 minutes ago (beyond the 30-minute timeout)
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=35)).replace(tzinfo=None).isoformat()

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions "
            "(session_type, status, total, scored, started_at) "
            "VALUES ('sync', 'running', 0, 0, ?)",
            (old_time,)
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/dashboard/sync/status/{session_id}")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Should return error fragment
        assert "text-red-400" in body

        # Verify DB row was updated to error
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM batch_score_sessions WHERE id=?", (session_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "error"

    def test_sync_status_nonexistent_session(self, app_with_db):
        """GET /dashboard/sync/status/{id} for nonexistent session returns error fragment."""
        app, db_path = app_with_db
        with app.test_client() as client:
            resp = client.get("/dashboard/sync/status/99999")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "text-red-400" in body

    def test_sync_dismiss_returns_sync_button(self, app_with_db):
        """GET /dashboard/sync/dismiss returns HTML containing the Sync Now button form."""
        app, db_path = app_with_db
        with app.test_client() as client:
            resp = client.get("/dashboard/sync/dismiss")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "sync-status" in body
        assert "sync/start" in body
