"""Tests for Migration 69 — heal rows where salary_min > salary_max.

Covers:
- Same-unit inversion (ratio <= 10:1 after swap) → swap.
- Extreme inversion (ratio > 10:1 after swap, likely unit mismatch) → null both.
- Well-ordered rows untouched.
- Only one of {min, max} populated → untouched.
- Idempotent re-run (no further changes on second invocation).
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.migrations.m069_heal_salary_inversions import MIGRATION, _heal
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


def _insert(
    conn: sqlite3.Connection,
    dedup_key: str,
    salary_min: int | None,
    salary_max: int | None,
) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               pipeline_status, sources,
               first_seen, last_seen, salary_min, salary_max)
            VALUES (?, 'T', 'C', 'X', '[]',
                    'discovered', '["test"]',
                    '2026-01-01', '2026-01-01', ?, ?)""",
        (dedup_key, salary_min, salary_max),
    )
    conn.commit()


def _read(conn: sqlite3.Connection, dedup_key: str) -> tuple[int | None, int | None]:
    r = conn.execute(
        "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return r["salary_min"], r["salary_max"]


def _run(path: str, conn: sqlite3.Connection) -> None:
    _heal(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=68))
    conn.commit()


def test_migration_declares_version_69():
    assert MIGRATION.version == 69


class TestHealSalaryInversions:
    def test_simple_inversion_swapped(self, migrated_db):
        # xAI-shape: $75/hr-$62/hr → swap.
        path, conn = migrated_db
        _insert(conn, "xai|x", 75, 62)
        _run(path, conn)
        assert _read(conn, "xai|x") == (62, 75)

    def test_workday_inversion_swapped(self, migrated_db):
        # JLL-shape: 100000/90309 → swap.
        path, conn = migrated_db
        _insert(conn, "jll|y", 100_000, 90_309)
        _run(path, conn)
        assert _read(conn, "jll|y") == (90_309, 100_000)

    def test_extreme_inversion_nulled(self, migrated_db):
        # PG&E-shape: 140000/2000 (likely $/yr vs $/hr) → null both.
        path, conn = migrated_db
        _insert(conn, "pge|z", 140_000, 2_000)
        _run(path, conn)
        assert _read(conn, "pge|z") == (None, None)


class TestUntouched:
    def test_well_ordered_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|a", 120_000, 150_000)
        _run(path, conn)
        assert _read(conn, "ok|a") == (120_000, 150_000)

    def test_equal_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|b", 100_000, 100_000)
        _run(path, conn)
        assert _read(conn, "ok|b") == (100_000, 100_000)

    def test_only_min_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|c", 120_000, None)
        _run(path, conn)
        assert _read(conn, "ok|c") == (120_000, None)

    def test_both_none_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|d", None, None)
        _run(path, conn)
        assert _read(conn, "ok|d") == (None, None)


class TestIdempotence:
    def test_second_run_no_change(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "xai|x", 75, 62)
        _insert(conn, "pge|z", 140_000, 2_000)
        _run(path, conn)
        first = (_read(conn, "xai|x"), _read(conn, "pge|z"))
        _run(path, conn)
        second = (_read(conn, "xai|x"), _read(conn, "pge|z"))
        assert first == second
        assert first == ((62, 75), (None, None))


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0
