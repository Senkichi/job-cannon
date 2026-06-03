"""Tests for Migration 72 — default workplace_type='UNSPECIFIED' for NULL rows.

Covers:
- NULL workplace_type → 'UNSPECIFIED'.
- Empty-string workplace_type → 'UNSPECIFIED'.
- Existing real values (REMOTE / HYBRID / ONSITE / UNSPECIFIED) preserved.
- Idempotent re-run.
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.migrations.m072_default_workplace_type_unspecified import (
    MIGRATION,
    _backfill,
)
from job_finder.web.migrations.types import MigrationContext
from tests.helpers.contract_triggers import (
    run_migrations_without_contract as run_migrations,
)


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


def _insert(conn: sqlite3.Connection, *, dedup_key: str, workplace_type: str | None) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               pipeline_status, sources, workplace_type,
               first_seen, last_seen)
            VALUES (?, 'T', 'C', 'X', '[]',
                    'discovered', '["test"]', ?,
                    '2026-01-01', '2026-01-01')""",
        (dedup_key, workplace_type),
    )
    conn.commit()


def _read(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    r = conn.execute(
        "SELECT workplace_type FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return r["workplace_type"]


def _run(path: str, conn: sqlite3.Connection) -> None:
    _backfill(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=71))
    conn.commit()


def test_migration_declares_version_72():
    assert MIGRATION.version == 72


class TestBackfill:
    def test_null_workplace_type_set_to_unspecified(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="null|a", workplace_type=None)
        _run(path, conn)
        assert _read(conn, "null|a") == "UNSPECIFIED"

    def test_empty_workplace_type_set_to_unspecified(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="empty|b", workplace_type="")
        _run(path, conn)
        assert _read(conn, "empty|b") == "UNSPECIFIED"


class TestPreserves:
    @pytest.mark.parametrize("value", ["REMOTE", "HYBRID", "ONSITE", "UNSPECIFIED"])
    def test_real_value_preserved(self, migrated_db, value):
        path, conn = migrated_db
        _insert(conn, dedup_key=f"preserve|{value}", workplace_type=value)
        _run(path, conn)
        assert _read(conn, f"preserve|{value}") == value


class TestIdempotence:
    def test_second_run_no_change(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, dedup_key="null|c", workplace_type=None)
        _run(path, conn)
        first = _read(conn, "null|c")
        _run(path, conn)
        assert _read(conn, "null|c") == first == "UNSPECIFIED"


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0
