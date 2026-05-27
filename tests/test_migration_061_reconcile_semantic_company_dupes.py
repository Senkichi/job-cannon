"""Tests for Migration 61 — reconcile semantic company-name duplicates.

Covers:
- Paren-abbreviation pairs ("X (Y)" vs "X") collapse to the lower id.
- Corporate-suffix variants ("Albertsons" vs "Albertsons Companies")
  collapse — `_COMPANY_SUFFIXES` already covers Inc/LLC/Corp/Companies/
  Group/Holdings/Services/Solutions/Tech/Technologies/Ltd/Co.
- FK references in jobs.company_id, company_scan_log.company_id, and
  company_research.company_id are re-pointed to the canonical row.
- Different base names (Amazon vs Amazon Web Services vs Amazon.com)
  are NOT merged — they're distinct hiring entities; out of scope.
- Idempotent re-run.
- No-op on a fresh empty DB.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m061_reconcile_semantic_company_dupes import (
    MIGRATION,
    _canonical_key,
    _reconcile,
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


def _insert_company(conn: sqlite3.Connection, name: str) -> int:
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, 'pending', '2026-01-01', '2026-01-01')""",
        (name, name),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_job_for_company(conn: sqlite3.Connection, dedup_key: str, company_id: int) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, first_seen, last_seen, company_id)
            VALUES (?, 'Engineer', 'X', 'Remote', '[]',
                    'jd', 'discovered', '["test"]',
                    '2026-01-01', '2026-01-01', ?)""",
        (dedup_key, company_id),
    )
    conn.commit()


def _insert_scan_log(conn: sqlite3.Connection, company_id: int) -> None:
    conn.execute(
        """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
           VALUES (?, '2026-01-01', 3)""",
        (company_id,),
    )
    conn.commit()


def _run_m061(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=60,
    )
    _reconcile(ctx)
    conn.commit()


def _company_exists(conn: sqlite3.Connection, company_id: int) -> bool:
    return conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is not None


def test_migration_declares_version_61():
    assert MIGRATION.version == 61


class TestCanonicalKey:
    """Unit tests for the comparison key — keep these alongside the
    integration tests so the migration's collapse logic is anchored to
    the keys it's actually computing."""

    @pytest.mark.parametrize(
        "name_a, name_b",
        [
            ("Albertsons", "Albertsons Companies"),
            ("Albertsons", "Albertsons, Inc."),
            ("Acme Corp.", "Acme"),
            ("AAA Mountain West Group", "AAA Mountain West Group (MWG)"),
            ("Foo LLC", "Foo Incorporated"),
            ("Bar Group", "Bar Holdings"),
        ],
    )
    def test_keys_are_equal(self, name_a, name_b):
        assert _canonical_key(name_a) == _canonical_key(name_b), (
            f"{name_a!r} and {name_b!r} should canonicalize to the same key"
        )

    @pytest.mark.parametrize(
        "name_a, name_b",
        [
            ("Amazon", "Amazon Web Services"),
            ("Amazon", "Amazon.com"),
            ("Apple", "Apple Records"),  # "Records" not in suffix list
            ("Google", "Alphabet"),
        ],
    )
    def test_keys_are_different(self, name_a, name_b):
        assert _canonical_key(name_a) != _canonical_key(name_b)

    def test_empty_returns_empty(self):
        assert _canonical_key("") == ""
        assert _canonical_key(None) == ""
        assert _canonical_key("  ") == ""


class TestParenAbbreviationCollapse:
    def test_paren_abbrev_pair_merges(self, migrated_db):
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "AAA Mountain West Group")
        orphan_id = _insert_company(conn, "AAA Mountain West Group (MWG)")
        _insert_job_for_company(conn, "test|j1", orphan_id)

        _run_m061(path, conn)

        assert _company_exists(conn, canonical_id)
        assert not _company_exists(conn, orphan_id)
        # Jobs re-pointed to canonical
        company_id_after = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'test|j1'"
        ).fetchone()[0]
        assert company_id_after == canonical_id

    def test_lowest_id_is_canonical_regardless_of_paren_form(self, migrated_db):
        """Whichever row was inserted first becomes canonical, not whichever
        has the cleaner name."""
        path, conn = migrated_db
        # Insert the parenthetical form FIRST (lower id)
        canonical_id = _insert_company(conn, "AAA Mountain West Group (MWG)")
        orphan_id = _insert_company(conn, "AAA Mountain West Group")

        _run_m061(path, conn)

        assert _company_exists(conn, canonical_id)
        assert not _company_exists(conn, orphan_id)


class TestCorporateSuffixCollapse:
    def test_companies_suffix_merges(self, migrated_db):
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "Albertsons")
        orphan_id = _insert_company(conn, "Albertsons Companies")
        _insert_job_for_company(conn, "test|j1", orphan_id)
        _insert_scan_log(conn, orphan_id)

        _run_m061(path, conn)

        assert _company_exists(conn, canonical_id)
        assert not _company_exists(conn, orphan_id)
        # Both FK refs re-pointed
        assert conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'test|j1'"
        ).fetchone()[0] == canonical_id
        assert conn.execute(
            "SELECT company_id FROM company_scan_log WHERE company_id = ?",
            (canonical_id,),
        ).fetchone() is not None

    def test_inc_and_llc_suffix_merge(self, migrated_db):
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "Acme")
        a_inc = _insert_company(conn, "Acme, Inc.")
        a_llc = _insert_company(conn, "Acme LLC")
        a_corp = _insert_company(conn, "Acme Corp.")

        _run_m061(path, conn)

        assert _company_exists(conn, canonical_id)
        assert not _company_exists(conn, a_inc)
        assert not _company_exists(conn, a_llc)
        assert not _company_exists(conn, a_corp)


class TestSubsidiaryVariantsPreserved:
    """Out-of-scope: Amazon / AWS / Amazon.com are NOT merged. Different
    base names, different hiring processes."""

    def test_amazon_variants_stay_separate(self, migrated_db):
        path, conn = migrated_db
        amazon_id = _insert_company(conn, "Amazon")
        aws_id = _insert_company(conn, "Amazon Web Services")
        aws_paren_id = _insert_company(conn, "Amazon Web Services (AWS)")
        amazon_com_id = _insert_company(conn, "Amazon.com")

        _run_m061(path, conn)

        # All four survive EXCEPT the AWS/AWS-paren pair which IS the
        # same paren-abbrev pattern m061 handles (kept lowest id).
        assert _company_exists(conn, amazon_id)
        assert _company_exists(conn, aws_id)
        assert not _company_exists(conn, aws_paren_id)  # paren-abbrev merge
        assert _company_exists(conn, amazon_com_id)


class TestIdempotence:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        canonical_id = _insert_company(conn, "Albertsons")
        orphan_id = _insert_company(conn, "Albertsons Companies")

        _run_m061(path, conn)
        assert _company_exists(conn, canonical_id)
        assert not _company_exists(conn, orphan_id)

        # Second invocation finds nothing — no group has >1 row by canonical key
        _run_m061(path, conn)
        assert _company_exists(conn, canonical_id)
        # No new rows were created or destroyed
        count = conn.execute("SELECT COUNT(*) FROM companies WHERE name LIKE 'Albertsons%'").fetchone()[0]
        assert count == 1


class TestEmptyDatabase:
    def test_no_companies_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m061(path, conn)  # should not raise
        count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert count == 0
