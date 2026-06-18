"""Tests for Migration 97 — heal result-count / category-landing tile rows (#211).

Covers:
- Result-count tile titles (incl. whitespace-variant dups, #212) are deleted.
- Clean / real-posting rows are kept (incl. numeric-prefixed legit titles).
- User-touched rows (pipeline_status != 'discovered') are kept.
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
from job_finder.web.migrations.m097_heal_listing_tile_rows import MIGRATION, _heal_listing_tiles
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db():
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
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, first_seen, last_seen)
            VALUES (?, ?, 'Capital One', 'Remote', '[]',
                    NULL, ?, ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, title, pipeline_status, json.dumps(sources)),
    )
    conn.commit()
    return dedup_key


def _run_m097(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=96,
    )
    _heal_listing_tiles(ctx)
    conn.commit()


def _job_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone() is not None
    )


def test_migration_declares_version_97():
    assert MIGRATION.version == 97


class TestTileRowDeletion:
    @pytest.mark.parametrize(
        ("dedup_key", "title"),
        [
            # The four #211 Capital One offenders (two whitespace-variant dups,
            # #212) plus a comma-grouped count tile.
            ("capital one|84 data scientist jobs", "84 Data Scientist Jobs"),
            ("capital one|71 business analyst jobs", "71 Business Analyst Jobs"),
            ("capital one|66business analyst jobs", "66 Business Analyst Jobs"),
            ("capital one|1200 openings", "1,200+ openings"),
        ],
    )
    def test_tile_title_is_deleted(self, migrated_db, dedup_key, title):
        path, conn = migrated_db
        _insert_job(conn, dedup_key, title, ["careers_page"])
        _run_m097(path, conn)
        assert not _job_exists(conn, dedup_key)


class TestSpareRealRows:
    @pytest.mark.parametrize(
        "title",
        ["Data Scientist", "100 Women in Finance — Analyst", "3D Artist"],
    )
    def test_real_title_is_kept(self, migrated_db, title):
        path, conn = migrated_db
        dedup_key = f"capital one|{title.lower()}"
        _insert_job(conn, dedup_key, title, ["careers_page"])
        _run_m097(path, conn)
        assert _job_exists(conn, dedup_key)


class TestUserActionGuard:
    @pytest.mark.parametrize("status", ["reviewing", "applied", "rejected", "archived"])
    def test_non_discovered_tile_row_is_kept(self, migrated_db, status):
        path, conn = migrated_db
        dedup_key = f"capital one|tile|{status}"
        _insert_job(
            conn,
            dedup_key,
            "84 Data Scientist Jobs",
            ["careers_page"],
            pipeline_status=status,
        )
        _run_m097(path, conn)
        assert _job_exists(conn, dedup_key), f"pipeline_status={status!r} must be preserved"


class TestIdempotence:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        tile_id = _insert_job(
            conn, "capital one|84 data scientist jobs", "84 Data Scientist Jobs", ["careers_page"]
        )
        clean_id = _insert_job(
            conn, "capital one|data scientist", "Data Scientist", ["careers_page"]
        )

        _run_m097(path, conn)
        assert not _job_exists(conn, tile_id)
        assert _job_exists(conn, clean_id)

        _run_m097(path, conn)
        assert not _job_exists(conn, tile_id)
        assert _job_exists(conn, clean_id)


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m097(path, conn)  # should not raise
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0
