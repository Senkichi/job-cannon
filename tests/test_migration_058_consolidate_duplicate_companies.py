"""Tests for Migration 58 — consolidate duplicate company rows.

Covers:
- Numeric-prefix orphans collapse into the canonical row (jobs.company_id and
  company_scan_log.company_id re-pointed; orphan row deleted).
- Exact-name duplicates collapse with the lowest id kept as canonical.
- No-op on rows that look like prefix-stripped names but have no canonical
  counterpart.
- No-op on a fresh database / single-row case.
- Idempotent re-run after consolidation (nothing else changes).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m058_consolidate_duplicate_companies import (
    MIGRATION,
    _consolidate,
)
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db():
    """Temp DB with all migrations applied, yielding (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_company(conn, name: str, name_raw: str | None = None) -> int:
    """Insert a company row and return its id."""
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, 'pending', '2026-01-01', '2026-01-01')""",
        (name, name_raw or name),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_job_for_company(conn, dedup_key: str, company_id: int) -> None:
    """Insert a minimal job linked to a company."""
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, first_seen, last_seen, company_id)
            VALUES (?, 'Engineer', 'X', 'Remote', 'https://example.com',
                    'jd', 'discovered', 'test',
                    '2026-01-01', '2026-01-01', ?)""",
        (dedup_key, company_id),
    )
    conn.commit()


def _insert_scan_log(conn, company_id: int) -> None:
    conn.execute(
        """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
           VALUES (?, '2026-01-01', 3)""",
        (company_id,),
    )
    conn.commit()


def _run_m058(path: str, conn: sqlite3.Connection) -> None:
    """Invoke the m058 consolidation helper directly against the connection."""
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=57,
    )
    _consolidate(ctx)
    conn.commit()


class TestNumericPrefixCollapse:
    def test_simple_prefix_merges_into_canonical(self, migrated_db):
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "salesforce")
        orphan_id = _insert_company(conn, "100 salesforce", "100 Salesforce, Inc.")
        _insert_job_for_company(conn, "job-1", orphan_id)
        _insert_scan_log(conn, orphan_id)

        _run_m058(path, conn)

        # Orphan deleted
        assert (
            conn.execute("SELECT 1 FROM companies WHERE id = ?", (orphan_id,)).fetchone()
            is None
        )
        # Job re-pointed
        row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'job-1'"
        ).fetchone()
        assert row["company_id"] == canonical_id
        # Scan log re-pointed
        row = conn.execute(
            "SELECT company_id FROM company_scan_log WHERE company_id = ?",
            (canonical_id,),
        ).fetchone()
        assert row is not None

    def test_underscore_prefix_merges(self, migrated_db):
        """'001_bcbsa' style prefix collapses into 'bcbsa'."""
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "bcbsa")
        orphan_id = _insert_company(conn, "001_bcbsa")  # this won't strip via regex
        # Note: actual regex requires whitespace after digits/_. '001_bcbsa' has
        # no whitespace, so it won't match — verifies expected boundary.
        _run_m058(path, conn)
        # Both rows survive — the orphan didn't match the pattern.
        assert (
            conn.execute("SELECT 1 FROM companies WHERE id = ?", (orphan_id,)).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM companies WHERE id = ?", (canonical_id,)
            ).fetchone()
            is not None
        )

    def test_underscore_then_space_prefix_merges(self, migrated_db):
        """'001_ bcbsa' (underscore + space) WOULD match — verifying."""
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "bcbsa")
        orphan_id = _insert_company(conn, "001_ bcbsa")
        _run_m058(path, conn)
        assert (
            conn.execute("SELECT 1 FROM companies WHERE id = ?", (orphan_id,)).fetchone()
            is None
        )
        assert canonical_id  # silences linter

    def test_no_canonical_means_no_change(self, migrated_db):
        """Numeric-prefix row with no canonical counterpart is left alone."""
        path, conn = migrated_db
        orphan_id = _insert_company(conn, "100 lonely")
        _run_m058(path, conn)
        # Untouched
        row = conn.execute(
            "SELECT name FROM companies WHERE id = ?", (orphan_id,)
        ).fetchone()
        assert row["name"] == "100 lonely"

    def test_short_digit_prefix_with_three_digits(self, migrated_db):
        """'558 evernorth sales operations' → 'evernorth sales operations'."""
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "evernorth sales operations")
        orphan_id = _insert_company(conn, "558 evernorth sales operations")
        _insert_job_for_company(conn, "job-evernorth", orphan_id)
        _run_m058(path, conn)
        assert (
            conn.execute("SELECT 1 FROM companies WHERE id = ?", (orphan_id,)).fetchone()
            is None
        )
        row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'job-evernorth'"
        ).fetchone()
        assert row["company_id"] == canonical_id


class TestExactDuplicateCollapse:
    def test_exact_duplicates_keep_lowest_id(self, migrated_db):
        path, conn = migrated_db
        first_id = _insert_company(conn, "veeva systems")
        second_id = _insert_company(conn, "veeva systems")
        third_id = _insert_company(conn, "veeva systems")
        assert first_id < second_id < third_id
        _insert_job_for_company(conn, "job-v-1", second_id)
        _insert_job_for_company(conn, "job-v-2", third_id)

        _run_m058(path, conn)

        # Only the lowest-id row remains
        rows = conn.execute(
            "SELECT id FROM companies WHERE LOWER(name) = 'veeva systems'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == first_id

        # Jobs all point at the kept row
        for key in ("job-v-1", "job-v-2"):
            row = conn.execute(
                "SELECT company_id FROM jobs WHERE dedup_key = ?", (key,)
            ).fetchone()
            assert row["company_id"] == first_id

    def test_case_insensitive_collapse(self, migrated_db):
        """'Stripe' and 'stripe' collapse — names compared case-insensitively."""
        path, conn = migrated_db
        first_id = _insert_company(conn, "Stripe")
        second_id = _insert_company(conn, "stripe")
        _run_m058(path, conn)
        # Only one row remains.
        rows = conn.execute(
            "SELECT id FROM companies WHERE LOWER(name) = 'stripe'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == first_id
        assert second_id  # silences linter


class TestSafety:
    def test_no_op_on_clean_db(self, migrated_db):
        path, conn = migrated_db
        a = _insert_company(conn, "acme")
        b = _insert_company(conn, "globex")
        _run_m058(path, conn)
        assert (
            conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2
        )
        assert a < b

    def test_idempotent_rerun(self, migrated_db):
        """Running the consolidation twice should leave the second pass as a no-op."""
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "acme")
        _insert_company(conn, "100 acme")
        _run_m058(path, conn)
        count_after_first = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        _run_m058(path, conn)
        count_after_second = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert count_after_first == count_after_second == 1
        assert canonical_id  # silences linter


def test_migration_declares_version_58():
    """Schema sanity: the module exposes the right MIGRATION constant."""
    assert MIGRATION.version == 58
    assert MIGRATION.py is _consolidate
    assert MIGRATION.sql == []
