"""Tests for company research service layer."""

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def research_db():
    """Create a migrated DB with a test company, return (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO companies (name, name_raw, homepage_url, industry, company_size, "
        "ats_probe_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("testco", "TestCo Inc", "https://testco.com", "SaaS", "500", "pending", now, now),
    )
    conn.commit()
    yield path, conn
    conn.close()
    os.remove(path)


class TestGetCachedCompanyResearch:
    """get_cached_company_research: cache hit/miss behavior."""

    def test_returns_none_when_no_research(self, research_db):
        from job_finder.web.company_research import get_cached_company_research

        path, conn = research_db
        assert get_cached_company_research(conn, 1) is None

    def test_returns_done_row_within_ttl(self, research_db):
        from job_finder.web.company_research import get_cached_company_research

        path, conn = research_db
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO company_research (company_id, status, research_json, requested_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "done", "Test research", now, now),
        )
        conn.commit()
        result = get_cached_company_research(conn, 1)
        assert result is not None
        assert result["status"] == "done"

    def test_returns_none_for_stale_cache(self, research_db):
        from job_finder.web.company_research import get_cached_company_research

        path, conn = research_db
        old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
        conn.execute(
            "INSERT INTO company_research (company_id, status, research_json, requested_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "done", "Old research", old, old),
        )
        conn.commit()
        assert get_cached_company_research(conn, 1, ttl_hours=72) is None

    def test_returns_generating_row(self, research_db):
        from job_finder.web.company_research import get_cached_company_research

        path, conn = research_db
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO company_research (company_id, status, requested_at) VALUES (?, ?, ?)",
            (1, "generating", now),
        )
        conn.commit()
        result = get_cached_company_research(conn, 1)
        assert result is not None
        assert result["status"] == "generating"


class TestStartCompanyResearch:
    """start_company_research: creates row and launches thread."""

    @patch("job_finder.web.company_research.threading.Thread")
    def test_creates_generating_row_and_returns_id(self, mock_thread, research_db):
        from job_finder.web.company_research import start_company_research

        path, conn = research_db
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        research_id = start_company_research(conn, 1, path, {})

        assert research_id is not None
        row = conn.execute(
            "SELECT * FROM company_research WHERE id = ?", (research_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "generating"
        mock_thread_instance.start.assert_called_once()


class TestRunCompanyResearchBackground:
    """run_company_research_background: service-layer behavior."""

    @patch("job_finder.web.company_research.call_model")
    def test_successful_research_sets_done(self, mock_call, research_db):
        from job_finder.web.company_research import run_company_research_background

        path, conn = research_db

        # Insert a generating row
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO company_research (company_id, status, requested_at) VALUES (?, ?, ?)",
            (1, "generating", now),
        )
        conn.commit()

        mock_result = MagicMock()
        mock_result.data = "TestCo is a SaaS company specializing in analytics."
        mock_result.cost_usd = 0.002
        mock_call.return_value = mock_result

        run_company_research_background(1, 1, path, {"scoring": {}})

        # Re-read from a fresh connection (background uses standalone_connection)
        check_conn = sqlite3.connect(path)
        check_conn.row_factory = sqlite3.Row
        row = check_conn.execute("SELECT * FROM company_research WHERE id = 1").fetchone()
        check_conn.close()
        assert row["status"] == "done"
        assert row["research_json"] is not None

    @patch("job_finder.web.company_research.call_model")
    def test_failed_research_sets_error(self, mock_call, research_db):
        from job_finder.web.company_research import run_company_research_background

        path, conn = research_db

        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO company_research (company_id, status, requested_at) VALUES (?, ?, ?)",
            (1, "generating", now),
        )
        conn.commit()

        mock_call.side_effect = Exception("API timeout")

        run_company_research_background(1, 1, path, {"scoring": {}})

        check_conn = sqlite3.connect(path)
        check_conn.row_factory = sqlite3.Row
        row = check_conn.execute("SELECT * FROM company_research WHERE id = 1").fetchone()
        check_conn.close()
        assert row["status"] == "error"
        assert "API timeout" in row["error_msg"]
