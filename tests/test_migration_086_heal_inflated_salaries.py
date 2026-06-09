"""Tests for Migration 86 — null inflated salaries.

Covers:
- Both-fields-inflated ordered pair → both nulled.
- Single inflated salary_min, NULL salary_max → both nulled.
- Single inflated salary_max, NULL salary_min → both nulled.
- Well-ordered plausible rows untouched.
- $5M boundary (exactly equal) untouched.
- Both NULL untouched.
- Idempotent re-run.
- Empty DB no-op.

Mirrors tests/test_migration_069_heal_salary_inversions.py.
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
    def test_both_fields_inflated_ordered_pair_nulled(self, migrated_db):
        """Anthropic-shape: 27.5M/37M (both inflated, sane ratio) → null both."""
        path, conn = migrated_db
        _insert(conn, "anth|safe", 27_500_000, 37_000_000)
        _run(path, conn)
        assert _read(conn, "anth|safe") == (None, None)

    def test_single_inflated_salary_min_max_null_nulled(self, migrated_db):
        """Reddit-shape: 27.67M / NULL (single inflated, no max) → null both."""
        path, conn = migrated_db
        _insert(conn, "reddit|staff", 27_670_000, None)
        _run(path, conn)
        assert _read(conn, "reddit|staff") == (None, None)

    def test_single_inflated_salary_max_min_null_nulled(self, migrated_db):
        """NULL / 20M (single inflated max, no min) → null both."""
        path, conn = migrated_db
        _insert(conn, "x|y", None, 20_000_000)
        _run(path, conn)
        assert _read(conn, "x|y") == (None, None)

    def test_just_over_threshold_salary_max_nulled(self, migrated_db):
        """Boundary just above $5M: 200k / 5,000,001 → null both."""
        path, conn = migrated_db
        _insert(conn, "edge|over", 200_000, 5_000_001)
        _run(path, conn)
        assert _read(conn, "edge|over") == (None, None)


class TestUntouched:
    def test_well_ordered_plausible_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|a", 120_000, 150_000)
        _run(path, conn)
        assert _read(conn, "ok|a") == (120_000, 150_000)

    def test_exactly_at_max_boundary_unchanged(self, migrated_db):
        """salary_max == $5M is at the inclusive bound — must NOT be healed.

        The application-layer check uses ``> _MAX_PLAUSIBLE_SALARY``; the
        migration uses the same operator.
        """
        path, conn = migrated_db
        _insert(conn, "ok|cap", 200_000, 5_000_000)
        _run(path, conn)
        assert _read(conn, "ok|cap") == (200_000, 5_000_000)

    def test_both_none_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|d", None, None)
        _run(path, conn)
        assert _read(conn, "ok|d") == (None, None)

    def test_plausible_min_only_unchanged(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "ok|e", 100_000, None)
        _run(path, conn)
        assert _read(conn, "ok|e") == (100_000, None)


class TestIdempotence:
    def test_second_run_no_change(self, migrated_db):
        path, conn = migrated_db
        _insert(conn, "anth|safe", 27_500_000, 37_000_000)
        _insert(conn, "ok|a", 120_000, 150_000)
        _run(path, conn)
        first = (_read(conn, "anth|safe"), _read(conn, "ok|a"))
        _run(path, conn)
        second = (_read(conn, "anth|safe"), _read(conn, "ok|a"))
        assert first == second
        assert first == ((None, None), (120_000, 150_000))


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        _run(path, conn)
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert before == after == 0


class TestAcceptanceCriterion:
    def test_post_migration_count_query_returns_zero(self, migrated_db):
        """Issue #228 acceptance: COUNT(*) WHERE salary_min > 5M OR salary_max > 5M == 0."""
        path, conn = migrated_db
        _insert(conn, "inflated|1", 27_500_000, 37_000_000)
        _insert(conn, "inflated|2", 22_000_000, 26_000_000)
        _insert(conn, "inflated|3", 12_800_000, None)
        _insert(conn, "ok|1", 100_000, 200_000)
        _run(path, conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE salary_min > 5000000 OR salary_max > 5000000"
        ).fetchone()[0]
        assert count == 0
