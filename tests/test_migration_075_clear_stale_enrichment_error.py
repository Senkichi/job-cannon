"""Tests for Migration 75 — clear stale enrichment_last_error labels."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m075_clear_stale_enrichment_error_for_active_companies import (
    MIGRATION,
    _clear,
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
    error: str | None = "no_signals_found",
    probe_status: str = "miss",
    jobs_found: int = 0,
    last_scanned_at: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_probe_status, enrichment_last_error,
               jobs_found_total, last_scanned_at,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
        (name, name.title(), probe_status, error, jobs_found, last_scanned_at),
    )
    conn.commit()


def _err(conn: sqlite3.Connection, name: str) -> str | None:
    return conn.execute(
        "SELECT enrichment_last_error FROM companies WHERE name = ?", (name,)
    ).fetchone()["enrichment_last_error"]


def _run(path: str, conn: sqlite3.Connection) -> None:
    _clear(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=74))
    conn.commit()


def test_migration_declares_version_75():
    assert MIGRATION.version == 75


class TestClears:
    def test_hit_probe_status_clears_label(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="a", probe_status="hit")
        _run(path, conn)
        assert _err(conn, "a") is None

    def test_positive_jobs_found_total_clears_label(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="b", probe_status="miss", jobs_found=5)
        _run(path, conn)
        assert _err(conn, "b") is None

    def test_recent_scan_clears_label(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="c", probe_status="miss", last_scanned_at="2026-05-20T00:00:00")
        _run(path, conn)
        assert _err(conn, "c") is None


class TestPreserves:
    def test_genuinely_dead_company_label_kept(self, migrated_db):
        # miss probe, no jobs, no recent scan — the label is still
        # informative.
        path, conn = migrated_db
        _insert(conn, name="d", probe_status="miss", jobs_found=0, last_scanned_at=None)
        _run(path, conn)
        assert _err(conn, "d") == "no_signals_found"

    def test_other_errors_untouched(self, migrated_db):
        # A different error string should not be cleared even if the
        # company is otherwise active.
        path, conn = migrated_db
        _insert(conn, name="e", error="timeout", probe_status="hit")
        _run(path, conn)
        assert _err(conn, "e") == "timeout"

    def test_already_null_left_alone(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="f", error=None, probe_status="hit")
        _run(path, conn)
        assert _err(conn, "f") is None


class TestIdempotence:
    def test_second_run_no_changes(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, name="g", probe_status="hit")
        _run(path, conn)
        _run(path, conn)
        assert _err(conn, "g") is None


class TestEmptyDatabase:
    def test_no_companies_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert before == after == 0
