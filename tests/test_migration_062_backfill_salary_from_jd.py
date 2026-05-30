"""Tests for Migration 62 — backfill salary_min/max from existing jd_full.

Covers:
- Rows with jd_full containing a plausible salary range get back-filled.
- Rows that already have salary_min OR salary_max are left alone (source-
  API values win — never overwrite).
- Rows with short / missing jd_full are skipped.
- Rows whose jd_full contains no salary range are left alone.
- Idempotent re-run (no further changes on second invocation).
- Empty DB no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m062_backfill_salary_from_jd import (
    MIGRATION,
    _backfill,
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


_LONG_JD = (
    "About this role: we're hiring a Senior Engineer to lead our "
    "platform team. You'll work on backend services, scaling, and "
    "infrastructure improvements. " * 5  # pad past _MIN_JD_LEN
)


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    jd_full: str | None,
    salary_min: int | None = None,
    salary_max: int | None = None,
) -> str:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources,
               first_seen, last_seen, salary_min, salary_max)
            VALUES (?, 'Engineer', 'X', 'Remote', '[]',
                    ?, 'discovered', '["test"]',
                    '2026-01-01', '2026-01-01', ?, ?)""",
        (dedup_key, jd_full, salary_min, salary_max),
    )
    conn.commit()
    return dedup_key


def _read_salary(conn: sqlite3.Connection, dedup_key: str) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT salary_min, salary_max FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return row["salary_min"], row["salary_max"]


def _run_m062(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=61,
    )
    _backfill(ctx)
    conn.commit()


def test_migration_declares_version_62():
    assert MIGRATION.version == 62


class TestBackfillFromJd:
    def test_jd_with_dollar_range_backfills(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Compensation: $120,000 - $150,000 per year."
        _insert_job(conn, "test|jd_dollar", jd)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|jd_dollar") == (120_000, 150_000)

    def test_jd_with_k_notation_backfills(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Salary range: 140K-180K."
        _insert_job(conn, "test|jd_k", jd)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|jd_k") == (140_000, 180_000)

    def test_jd_with_no_salary_text_left_alone(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "test|no_salary", _LONG_JD)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|no_salary") == (None, None)


class TestSourceApiSalaryPreserved:
    """Source APIs that did report salary (LinkedIn JSON, SerpAPI extensions)
    win over JD-extracted regex hits. Never overwrite an existing value."""

    def test_existing_min_left_alone(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Compensation: $120,000 - $150,000 per year."
        _insert_job(conn, "test|existing_min", jd, salary_min=110_000, salary_max=None)
        _run_m062(path, conn)
        # Row had salary_min set, so the whole row is skipped — both
        # values are preserved as-is (max stays NULL).
        assert _read_salary(conn, "test|existing_min") == (110_000, None)

    def test_existing_max_left_alone(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Compensation: $120,000 - $150,000 per year."
        _insert_job(conn, "test|existing_max", jd, salary_min=None, salary_max=200_000)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|existing_max") == (None, 200_000)

    def test_both_existing_left_alone(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Compensation: $120,000 - $150,000 per year."
        _insert_job(conn, "test|both", jd, salary_min=110_000, salary_max=140_000)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|both") == (110_000, 140_000)


class TestSkipsShortOrMissingJd:
    def test_null_jd_full_skipped(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "test|null_jd", None)
        _run_m062(path, conn)
        assert _read_salary(conn, "test|null_jd") == (None, None)

    def test_short_jd_full_skipped(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "test|short_jd", "$120K - $150K")  # < _MIN_JD_LEN
        _run_m062(path, conn)
        assert _read_salary(conn, "test|short_jd") == (None, None)


class TestIdempotence:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        jd = _LONG_JD + " Salary range: 140K-180K."
        _insert_job(conn, "test|round_trip", jd)

        _run_m062(path, conn)
        first = _read_salary(conn, "test|round_trip")
        assert first == (140_000, 180_000)

        _run_m062(path, conn)
        second = _read_salary(conn, "test|round_trip")
        assert second == first


class TestEmptyDatabase:
    def test_no_jobs_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m062(path, conn)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert count == 0
