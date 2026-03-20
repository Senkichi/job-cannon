"""Tests for db.py module-level functions, including load_job_context (DEBT-05)."""

import sqlite3
import tempfile
import os
from datetime import datetime

import pytest

from job_finder.web.db_migrate import run_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_conn():
    """Create a temp DB with full migrations applied, yield conn. Cleanup after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_job(conn, dedup_key, title="Test Job", company="Test Co",
                location="Remote", pipeline_status="discovered",
                sonnet_score=None, haiku_score=None):
    """Insert a minimal job row for testing."""
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             pipeline_status, first_seen, last_seen, score, score_breakdown,
             user_interest, sonnet_score, haiku_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, title, company, location, '["test"]',
         f'["https://example.com/{dedup_key}"]',
         pipeline_status, now, now, 7.0, '{}', 'unreviewed',
         sonnet_score, haiku_score),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: load_job_context (DEBT-05)
# ---------------------------------------------------------------------------


class TestLoadJobContext:
    """Tests for load_job_context shared helper (DEBT-05)."""

    def test_returns_none_for_nonexistent_job(self, migrated_conn):
        """load_job_context returns None when dedup_key not found."""
        from job_finder.db import load_job_context
        result = load_job_context(migrated_conn, "nonexistent|job key")
        assert result is None

    def test_returns_dict_with_required_keys(self, migrated_conn):
        """load_job_context returns dict with 'job', 'resume_history', 'prep_row' keys."""
        from job_finder.db import load_job_context
        _insert_job(migrated_conn, "acme|senior engineer")

        result = load_job_context(migrated_conn, "acme|senior engineer")

        assert result is not None
        assert "job" in result
        assert "resume_history" in result
        assert "prep_row" in result

    def test_job_key_is_dict(self, migrated_conn):
        """load_job_context result['job'] is a dict with expected fields."""
        from job_finder.db import load_job_context
        _insert_job(migrated_conn, "acme|senior engineer", title="Senior Engineer", company="Acme")

        result = load_job_context(migrated_conn, "acme|senior engineer")

        assert isinstance(result["job"], dict)
        assert result["job"]["dedup_key"] == "acme|senior engineer"
        assert result["job"]["title"] == "Senior Engineer"
        assert result["job"]["company"] == "Acme"

    def test_resume_history_is_list(self, migrated_conn):
        """load_job_context result['resume_history'] is a list (empty when no records)."""
        from job_finder.db import load_job_context
        _insert_job(migrated_conn, "acme|senior engineer")

        result = load_job_context(migrated_conn, "acme|senior engineer")

        assert isinstance(result["resume_history"], list)
        assert result["resume_history"] == []

    def test_prep_row_is_none_when_no_preps(self, migrated_conn):
        """load_job_context result['prep_row'] is None when no interview_preps exist."""
        from job_finder.db import load_job_context
        _insert_job(migrated_conn, "acme|senior engineer")

        result = load_job_context(migrated_conn, "acme|senior engineer")

        assert result["prep_row"] is None

    def test_resume_history_contains_records(self, migrated_conn):
        """load_job_context result['resume_history'] includes existing resume_generations rows."""
        from job_finder.db import load_job_context
        dedup_key = "stripe|data scientist"
        _insert_job(migrated_conn, dedup_key)

        # Insert a resume generation record
        now = datetime.now().isoformat()
        migrated_conn.execute(
            "INSERT INTO resume_generations "
            "(job_id, generated_at, model, status, doc_url, generation_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dedup_key, now, "claude-sonnet-4-6", "done", "https://docs.google.com/test", "single"),
        )
        migrated_conn.commit()

        result = load_job_context(migrated_conn, dedup_key)

        assert len(result["resume_history"]) == 1
        assert result["resume_history"][0]["job_id"] == dedup_key
        assert result["resume_history"][0]["status"] == "done"

    def test_prep_row_returned_when_exists(self, migrated_conn):
        """load_job_context result['prep_row'] contains most recent interview_preps row."""
        from job_finder.db import load_job_context
        dedup_key = "google|product manager"
        _insert_job(migrated_conn, dedup_key)

        # Insert an interview prep record
        now = datetime.now().isoformat()
        migrated_conn.execute(
            "INSERT INTO interview_preps "
            "(job_id, status, company_brief, generated_at) "
            "VALUES (?, ?, ?, ?)",
            (dedup_key, "done", "Google is a tech giant.", now),
        )
        migrated_conn.commit()

        result = load_job_context(migrated_conn, dedup_key)

        assert result["prep_row"] is not None
        assert result["prep_row"]["status"] == "done"

    def test_resume_history_ordered_newest_first(self, migrated_conn):
        """load_job_context resume_history is ordered by generated_at DESC."""
        from job_finder.db import load_job_context
        dedup_key = "meta|ml engineer"
        _insert_job(migrated_conn, dedup_key)

        # Insert two resume generation records with different timestamps
        migrated_conn.execute(
            "INSERT INTO resume_generations "
            "(job_id, generated_at, model, status, generation_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (dedup_key, "2026-01-01T10:00:00Z", "claude-sonnet-4-6", "done", "single"),
        )
        migrated_conn.execute(
            "INSERT INTO resume_generations "
            "(job_id, generated_at, model, status, generation_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (dedup_key, "2026-01-05T10:00:00Z", "claude-sonnet-4-6", "done", "single"),
        )
        migrated_conn.commit()

        result = load_job_context(migrated_conn, dedup_key)

        assert len(result["resume_history"]) == 2
        # Newest first
        assert result["resume_history"][0]["generated_at"] == "2026-01-05T10:00:00Z"
        assert result["resume_history"][1]["generated_at"] == "2026-01-01T10:00:00Z"

    def test_resume_history_includes_validation_report(self, migrated_conn):
        """load_job_context resume_history rows include validation_report column."""
        from job_finder.db import load_job_context
        dedup_key = "anthropic|ml researcher"
        _insert_job(migrated_conn, dedup_key)

        validation_json = '{"passed": true, "violations": [], "fix_summary": ""}'
        now = datetime.now().isoformat()
        migrated_conn.execute(
            "INSERT INTO resume_generations "
            "(job_id, generated_at, model, status, doc_url, generation_type, validation_report) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dedup_key, now, "claude-sonnet-4-6", "done",
             "https://docs.google.com/test", "single", validation_json),
        )
        migrated_conn.commit()

        result = load_job_context(migrated_conn, dedup_key)

        assert len(result["resume_history"]) == 1
        row = result["resume_history"][0]
        assert "validation_report" in row.keys(), (
            "resume_history row missing validation_report column"
        )
        assert row["validation_report"] == validation_json

    def test_resume_history_validation_report_null_when_not_set(self, migrated_conn):
        """load_job_context resume_history rows have validation_report=None when not stored."""
        from job_finder.db import load_job_context
        dedup_key = "openai|engineer"
        _insert_job(migrated_conn, dedup_key)

        now = datetime.now().isoformat()
        migrated_conn.execute(
            "INSERT INTO resume_generations "
            "(job_id, generated_at, model, status, generation_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (dedup_key, now, "claude-sonnet-4-6", "done", "single"),
        )
        migrated_conn.commit()

        result = load_job_context(migrated_conn, dedup_key)

        assert len(result["resume_history"]) == 1
        row = result["resume_history"][0]
        assert "validation_report" in row.keys(), (
            "resume_history row missing validation_report column"
        )
        assert row["validation_report"] is None


# ---------------------------------------------------------------------------
# Tests: update_pipeline_status evidence parameter (Phase 30 infrastructure)
# ---------------------------------------------------------------------------


class TestUpdatePipelineStatusEvidence:
    """Tests for evidence parameter on update_pipeline_status() (Phase 30 INFRA-01)."""

    def test_evidence_written_to_pipeline_events(self, migrated_conn):
        """Evidence string is written to pipeline_events.evidence on status change."""
        from job_finder.db import update_pipeline_status
        _insert_job(migrated_conn, "test|evidence|job", pipeline_status="discovered")
        update_pipeline_status(
            migrated_conn,
            "test|evidence|job",
            "archived",
            source="expiry_check",
            evidence="lever_api 404",
        )
        event = migrated_conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'test|evidence|job'"
        ).fetchone()
        assert event is not None, "No pipeline_event row found after status change"
        assert event["evidence"] == "lever_api 404"

    def test_default_evidence_is_empty_string(self, migrated_conn):
        """Calling update_pipeline_status without evidence kwarg writes empty string."""
        from job_finder.db import update_pipeline_status
        _insert_job(migrated_conn, "test|default-evidence|job", pipeline_status="discovered")
        update_pipeline_status(migrated_conn, "test|default-evidence|job", "reviewing")
        event = migrated_conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'test|default-evidence|job'"
        ).fetchone()
        assert event is not None, "No pipeline_event row found after status change"
        assert event["evidence"] == ""

    def test_same_status_no_event_even_with_evidence(self, migrated_conn):
        """Calling update_pipeline_status with same status is a no-op even with evidence."""
        from job_finder.db import update_pipeline_status
        _insert_job(migrated_conn, "test|noop-evidence|job", pipeline_status="archived")
        update_pipeline_status(
            migrated_conn,
            "test|noop-evidence|job",
            "archived",
            evidence="should not appear",
        )
        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id = 'test|noop-evidence|job'"
        ).fetchone()[0]
        assert count == 0, f"Expected no pipeline_event rows (no-op), got: {count}"
