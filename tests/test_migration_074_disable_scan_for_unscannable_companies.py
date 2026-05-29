"""Tests for Migration 74 — disable scan_enabled for unscannable companies."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m074_disable_scan_for_unscannable_companies import (
    MIGRATION,
    _disable,
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
    name: str,
    probe_status: str,
    platform: str | None,
    scan_enabled: int = 1,
) -> None:
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_probe_status, scan_enabled,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
        (name, name.title(), platform, probe_status, scan_enabled),
    )
    conn.commit()


def _scan_enabled(conn: sqlite3.Connection, name: str) -> int:
    return conn.execute(
        "SELECT scan_enabled FROM companies WHERE name = ?", (name,)
    ).fetchone()["scan_enabled"]


def _run(path: str, conn: sqlite3.Connection) -> None:
    _disable(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=73))
    conn.commit()


def test_migration_declares_version_74():
    assert MIGRATION.version == 74


class TestDisables:
    def test_miss_with_null_platform_disabled(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="a", probe_status="miss", platform=None)
        _run(path, conn)
        assert _scan_enabled(conn, "a") == 0

    def test_miss_with_empty_string_platform_disabled(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="b", probe_status="miss", platform="")
        _run(path, conn)
        assert _scan_enabled(conn, "b") == 0


class TestPreserves:
    def test_hit_with_platform_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="c", probe_status="hit", platform="greenhouse")
        _run(path, conn)
        assert _scan_enabled(conn, "c") == 1

    def test_pending_with_null_platform_preserved(self, migrated_db):
        # Pending may still resolve to a real platform — don't disable.
        path, conn = migrated_db
        _insert(conn, name="d", probe_status="pending", platform=None)
        _run(path, conn)
        assert _scan_enabled(conn, "d") == 1

    def test_miss_with_platform_preserved(self, migrated_db):
        # 'miss' but a platform was previously detected — edge case but
        # leave the row alone, the operator may want to retry.
        path, conn = migrated_db
        _insert(conn, name="e", probe_status="miss", platform="lever")
        _run(path, conn)
        assert _scan_enabled(conn, "e") == 1

    def test_already_disabled_left_alone(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="f", probe_status="miss", platform=None, scan_enabled=0)
        _run(path, conn)
        assert _scan_enabled(conn, "f") == 0


class TestIdempotence:
    def test_second_run_no_changes(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="g", probe_status="miss", platform=None)
        _run(path, conn)
        first = _scan_enabled(conn, "g")
        _run(path, conn)
        assert _scan_enabled(conn, "g") == first == 0


class TestEmptyDatabase:
    def test_no_companies_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run(path, conn)
