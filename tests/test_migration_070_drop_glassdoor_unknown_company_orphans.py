"""Tests for Migration 70 — delete Glassdoor jobs with company='Unknown'.

Covers:
- Glassdoor-only orphan deleted.
- Multi-source row including glassdoor + company_id NULL deleted.
- Multi-source row including glassdoor + company_id SET preserved
  (some later path attached an FK, leave the row alone).
- Non-glassdoor row with company='Unknown' preserved (out of scope).
- Glassdoor row with a real company name preserved.
- Idempotent re-run (no further deletes).
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m070_drop_glassdoor_unknown_company_orphans import (
    MIGRATION,
    _drop,
)
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


def _insert(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
    company: str,
    sources_json: str,
    company_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               pipeline_status, sources, company_id,
               first_seen, last_seen)
            VALUES (?, 'T', ?, 'X', '[]',
                    'discovered', ?, ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, company, sources_json, company_id),
    )
    conn.commit()


def _exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    r = conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return r is not None


def _run(path: str, conn: sqlite3.Connection) -> None:
    _drop(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=69))
    conn.commit()


def test_migration_declares_version_70():
    assert MIGRATION.version == 70


class TestDeletes:
    def test_glassdoor_only_unknown_deleted(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="orphan|a", company="Unknown", sources_json='["glassdoor"]')
        _run(path, conn)
        assert not _exists(conn, "orphan|a")

    def test_multi_source_including_glassdoor_no_fk_deleted(self, migrated_db):
        path, conn = migrated_db
        _insert(
            conn,
            dedup_key="orphan|b",
            company="Unknown",
            sources_json='["serpapi", "glassdoor"]',
        )
        _run(path, conn)
        assert not _exists(conn, "orphan|b")


class TestPreserves:
    def test_glassdoor_unknown_with_company_id_preserved(self, migrated_db):
        # A later resolver attached an FK — keep the row, the orphan
        # condition no longer holds.
        path, conn = migrated_db
        _insert(
            conn,
            dedup_key="keep|c",
            company="Unknown",
            sources_json='["glassdoor"]',
            company_id=42,
        )
        _run(path, conn)
        assert _exists(conn, "keep|c")

    def test_non_glassdoor_unknown_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="keep|d", company="Unknown", sources_json='["serpapi"]')
        _run(path, conn)
        assert _exists(conn, "keep|d")

    def test_glassdoor_real_company_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="keep|e", company="Acme Corp", sources_json='["glassdoor"]')
        _run(path, conn)
        assert _exists(conn, "keep|e")


class TestIdempotence:
    def test_second_run_no_further_deletes(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="orphan|f", company="Unknown", sources_json='["glassdoor"]')
        _insert(conn, dedup_key="keep|g", company="Acme", sources_json='["glassdoor"]')
        _run(path, conn)
        assert not _exists(conn, "orphan|f")
        assert _exists(conn, "keep|g")
        # Second run: nothing left to delete.
        _run(path, conn)
        assert _exists(conn, "keep|g")


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run(path, conn)
