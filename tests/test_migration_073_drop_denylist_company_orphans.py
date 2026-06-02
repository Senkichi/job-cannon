"""Tests for Migration 73 — delete denylist-company orphans.

Covers:
- Denylisted name + NULL company_id deleted (parametrized across the
  full COMPANY_DENYLIST).
- Denylisted name WITH company_id preserved (defensive — a genuine
  match should never be removed).
- Non-denylisted name preserved.
- Idempotent re-run.
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.config import COMPANY_DENYLIST
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m073_drop_denylist_company_orphans import MIGRATION, _drop
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
    company_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               pipeline_status, sources, company_id,
               first_seen, last_seen)
            VALUES (?, 'T', ?, 'X', '[]',
                    'discovered', '["test"]', ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, company, company_id),
    )
    conn.commit()


def _exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone() is not None
    )


def _run(path: str, conn: sqlite3.Connection) -> None:
    _drop(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=72))
    conn.commit()


def test_migration_declares_version_73():
    assert MIGRATION.version == 73


class TestDeletes:
    @pytest.mark.parametrize("name", sorted(COMPANY_DENYLIST))
    def test_each_denylisted_name_deleted_when_company_id_null(self, migrated_db, name):
        path, conn = migrated_db
        # Mixed case to ensure LOWER() in the migration matches both forms.
        _insert(conn, dedup_key=f"orphan|{name}", company=name.title())
        _run(path, conn)
        assert not _exists(conn, f"orphan|{name}")


class TestPreserves:
    def test_denylisted_name_with_fk_preserved(self, migrated_db):
        # Defensive: if a row somehow got linked to a real company
        # record despite the name overlap, leave it alone.
        path, conn = migrated_db
        _insert(conn, dedup_key="keep|a", company="Jobgether", company_id=42)
        _run(path, conn)
        assert _exists(conn, "keep|a")

    def test_non_denylisted_name_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="keep|b", company="Stripe")
        _run(path, conn)
        assert _exists(conn, "keep|b")


class TestIdempotence:
    def test_second_run_no_further_deletes(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="orphan|c", company="Mercor")
        _insert(conn, dedup_key="keep|d", company="Stripe")
        _run(path, conn)
        assert not _exists(conn, "orphan|c")
        assert _exists(conn, "keep|d")
        _run(path, conn)
        assert _exists(conn, "keep|d")


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0
