"""Tests for Migration 86 — heal rows where salary_min/salary_max are outside [$30K, $5M].

Covers:
- Both-fields-inflated ordered pair (~100x annual) → both nulled.
- Single inflated salary_min with salary_max NULL → both nulled.
- Single inflated salary_max with salary_min NULL → both nulled.
- One field inflated, the other plausible → BOTH nulled (no half-open range leak).
- salary_min below $30K floor (NULL salary_max) → both nulled.
- Well-ordered plausible rows untouched.
- Boundary values $30K and $5M untouched.
- Idempotent re-run (no further changes on second invocation).
- Empty DB no-op.
- Migration declares version 86.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.migrations.m086_heal_inflated_salaries import MIGRATION, _heal
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
    _heal(MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=85))
    conn.commit()


def test_migration_declares_version_86():
    assert MIGRATION.version == 86


class TestHealInflatedSalaries:
    def test_both_inflated_ordered_pair_nulled(self, migrated_db):
        # Anthropic-shape: $275k role emitted as 27_500_000 / 37_000_000.
        path, conn = migrated_db
        _insert(conn, "anthropic|ds", 27_500_000, 37_000_000)
        _run(path, conn)
        assert _read(conn, "anthropic|ds") == (None, None)

    def test_single_inflated_min_max_null_nulled(self, migrated_db):
        # Reddit-shape: 27_670_000 / NULL.
        path, conn = migrated_db
        _insert(conn, "reddit|staff_ds", 27_670_000, None)
        _run(path, conn)
        assert _read(conn, "reddit|staff_ds") == (None, None)

    def test_single_inflated_max_min_null_nulled(self, migrated_db):
        # Mirror case: NULL / inflated max.
        path, conn = migrated_db
        _insert(conn, "x|inflated_max", None, 7_500_000)
        _run(path, conn)
        assert _read(conn, "x|inflated_max") == (None, None)

    def test_one_inflated_one_plausible_both_nulled(self, migrated_db):
        # Asymmetric case must drop BOTH — no half-open range leak.
        path, conn = migrated_db
        _insert(conn, "x|half_open", 100_000, 7_500_000)
        _run(path, conn)
        assert _read(conn, "x|half_open") == (None, None)

    def test_below_min_floor_nulled(self, migrated_db):
        # Below-$30K floor (e.g. hourly value mis-extracted) — both nulled.
        path, conn = migrated_db
        _insert(conn, "x|below_floor", 15_000, None)
        _run(path, conn)
        assert _read(conn, "x|below_floor") == (None, None)


class TestUntouched:
    def test_plausible_range_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|a", 120_000, 150_000)
        _run(path, conn)
        assert _read(conn, "ok|a") == (120_000, 150_000)

    def test_only_plausible_min_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|b", 180_000, None)
        _run(path, conn)
        assert _read(conn, "ok|b") == (180_000, None)

    def test_both_none_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|c", None, None)
        _run(path, conn)
        assert _read(conn, "ok|c") == (None, None)

    def test_lower_boundary_30k_unchanged(self, migrated_db):
        # $30_000 is the inclusive floor; must survive.
        path, conn = migrated_db
        _insert(conn, "ok|floor", 30_000, 60_000)
        _run(path, conn)
        assert _read(conn, "ok|floor") == (30_000, 60_000)

    def test_upper_boundary_5m_unchanged(self, migrated_db):
        # $5_000_000 is the inclusive ceiling; must survive.
        path, conn = migrated_db
        _insert(conn, "ok|ceiling", 200_000, 5_000_000)
        _run(path, conn)
        assert _read(conn, "ok|ceiling") == (200_000, 5_000_000)


class TestAcceptanceCriteria:
    def test_no_rows_over_5m_after_migration(self, migrated_db):
        """After migration: no row has salary_min > 5_000_000 OR salary_max > 5_000_000."""
        path, conn = migrated_db
        _insert(conn, "a", 27_500_000, 37_000_000)
        _insert(conn, "b", 22_138_000, 26_367_000)
        _insert(conn, "c", 27_670_000, None)
        _insert(conn, "d", 120_000, 150_000)  # clean
        _run(path, conn)
        n = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE salary_min > 5000000 OR salary_max > 5000000"
        ).fetchone()[0]
        assert n == 0


class TestIdempotence:
    def test_second_run_no_change(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "inflated|a", 27_500_000, 37_000_000)
        _insert(conn, "clean|b", 120_000, 150_000)
        _run(path, conn)
        first = (_read(conn, "inflated|a"), _read(conn, "clean|b"))
        _run(path, conn)
        second = (_read(conn, "inflated|a"), _read(conn, "clean|b"))
        assert first == second
        assert first == ((None, None), (120_000, 150_000))


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0
