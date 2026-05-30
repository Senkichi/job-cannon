"""Tests for Migration 59 — heal careers_crawl title-bleed rows.

Covers:
- Careers_crawl-only rows with metadata-blob titles are deleted.
- Rows whose title is clean are kept regardless of source.
- Multi-source rows are kept even when title is a metadata blob (out of
  scope of this heal — title may have come from another source).
- User-touched rows (pipeline_status != NULL/unscored) are kept even if
  the title is a metadata blob.
- Idempotent re-run after the heal is a no-op.
- No-op on a fresh database with no jobs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m059_heal_careers_crawl_title_bleed import (
    MIGRATION,
    _heal_title_bleed,
)
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db():
    """Temp DB with all migrations applied, yielding (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    title: str,
    sources: list[str],
    pipeline_status: str = "discovered",
) -> str:
    """Insert a minimal job row and return its dedup_key (primary key)."""
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, first_seen, last_seen)
            VALUES (?, ?, 'X', 'Remote', '[]',
                    NULL, ?, ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, title, pipeline_status, json.dumps(sources)),
    )
    conn.commit()
    return dedup_key


def _run_m059(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=58,
    )
    _heal_title_bleed(ctx)
    conn.commit()


def _job_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone() is not None
    )


def test_migration_declares_version_59():
    assert MIGRATION.version == 59


class TestPollutedRowDeletion:
    def test_long_blob_title_careers_crawl_only_is_deleted(self, migrated_db):
        path, conn = migrated_db
        polluted = (
            "Senior Data Scientist - GenAI Innovation and Automation, "
            "more accessible "
            + "x" * 100  # push length > 140
            + " SQL2354308|Chennai, Tamil Nadu"
        )
        row_id = _insert_job(conn, "test|polluted", polluted, ["careers_crawl"])
        _run_m059(path, conn)
        assert not _job_exists(conn, row_id)

    def test_posted_phrase_marker_is_deleted(self, migrated_db):
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|posted",
            "Senior Engineer Posted 10 days ago AgencyUNDP",
            ["careers_crawl"],
        )
        _run_m059(path, conn)
        assert not _job_exists(conn, row_id)

    def test_dollar_in_title_is_deleted(self, migrated_db):
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|dollar",
            "Engineer - $120,000 - $160,000 - Multiple locations",
            ["careers_crawl"],
        )
        _run_m059(path, conn)
        assert not _job_exists(conn, row_id)

    def test_req_id_pipe_title_is_deleted(self, migrated_db):
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|reqid",
            "Engineer SQL2354308|Chennai",
            ["careers_crawl"],
        )
        _run_m059(path, conn)
        assert not _job_exists(conn, row_id)


class TestSpareCleanRows:
    def test_clean_title_careers_crawl_is_kept(self, migrated_db):
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|clean",
            "Senior Software Engineer",
            ["careers_crawl"],
        )
        _run_m059(path, conn)
        assert _job_exists(conn, row_id)


class TestSourceScope:
    def test_multisource_row_with_blob_title_is_kept(self, migrated_db):
        """Out of scope: title may have come from another source, and the
        row may carry valid data from that source. The new careers_crawl
        filter only prevents future bleed; this heal handles careers_crawl-
        only rows."""
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|multi",
            "Engineer Posted 10 days ago AgencyUNDP",
            ["careers_crawl", "linkedin"],
        )
        _run_m059(path, conn)
        assert _job_exists(conn, row_id)

    def test_non_careers_crawl_blob_title_is_kept(self, migrated_db):
        path, conn = migrated_db
        row_id = _insert_job(
            conn,
            "test|other",
            "Engineer Posted 10 days ago AgencyUNDP",
            ["linkedin"],
        )
        _run_m059(path, conn)
        assert _job_exists(conn, row_id)


class TestUserActionGuard:
    @pytest.mark.parametrize(
        "status",
        # Sample across the pipeline_status state machine: a user-engagement
        # state, an interview-stage state, a terminal state, and the auto-
        # archive state (stale_detector writes 'archived' without user input,
        # but we still preserve it — conservative over surgical).
        ["reviewing", "applied", "phone_screen", "rejected", "archived"],
    )
    def test_non_discovered_row_is_kept_even_with_blob_title(self, migrated_db, status):
        path, conn = migrated_db
        dedup_key = _insert_job(
            conn,
            f"test|touched|{status}",
            "Engineer Posted 10 days ago AgencyUNDP",
            ["careers_crawl"],
            pipeline_status=status,
        )
        _run_m059(path, conn)
        assert _job_exists(conn, dedup_key), (
            f"row with pipeline_status={status!r} must be preserved"
        )


class TestIdempotence:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        polluted_id = _insert_job(
            conn,
            "test|polluted",
            "Engineer Posted 10 days ago AgencyUNDP",
            ["careers_crawl"],
        )
        clean_id = _insert_job(
            conn,
            "test|clean",
            "Senior Software Engineer",
            ["careers_crawl"],
        )

        _run_m059(path, conn)
        assert not _job_exists(conn, polluted_id)
        assert _job_exists(conn, clean_id)

        # Second invocation finds nothing to delete and leaves the clean row alone.
        _run_m059(path, conn)
        assert not _job_exists(conn, polluted_id)
        assert _job_exists(conn, clean_id)


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m059(path, conn)  # should not raise
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0
