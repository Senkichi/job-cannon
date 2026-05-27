"""Tests for Migration 63 — merge companies by shared job board.

Covers:
- (ats_platform, ats_slug) clusters collapse to the row with highest
  ``jobs_found_total`` (ties broken by lowest id).
- Canonical careers_url clusters collapse the same way.
- The (platform, slug) pass runs BEFORE the URL pass, so a row that
  shares ats_slug with another collapses there even if it ALSO shares
  careers_url with a third — no double-merge confusion.
- _canonical_careers_url normalizes scheme / www / case / trailing slash
  / query string into a single key.
- Companies with NULL ats_platform/slug AND NULL careers_url are NOT
  touched (the user's "ncidia"/"2100 nvidia usa" case requires either
  a successful re-probe or a name-based heuristic, not job-board merge).
- Idempotent re-run.
- No-op on a fresh empty DB.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m063_merge_companies_by_job_board import (
    MIGRATION,
    _canonical_careers_url,
    _merge,
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


def _insert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
    careers_url: str | None = None,
    jobs_found_total: int = 0,
) -> int:
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_slug, careers_url,
               jobs_found_total, ats_probe_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', '2026-01-01', '2026-01-01')""",
        (name, name, ats_platform, ats_slug, careers_url, jobs_found_total),
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


def _run_m063(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=62,
    )
    _merge(ctx)
    conn.commit()


def _company_exists(conn: sqlite3.Connection, company_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone()
    return row is not None


def test_migration_declares_version_63():
    assert MIGRATION.version == 63


class TestCanonicalCareersUrl:
    """The URL canonicalizer is the heart of the URL pass. Lock in its
    contract — two inputs that should be equal must canonicalize to the
    same string; two inputs that should differ must not."""

    @pytest.mark.parametrize(
        "a, b",
        [
            ("https://example.com/careers/", "https://example.com/careers"),
            ("https://example.com/careers", "http://example.com/careers"),
            ("https://www.example.com/careers", "https://example.com/careers"),
            ("https://Example.com/careers", "https://example.com/careers"),
            ("https://example.com/careers?ref=x", "https://example.com/careers"),
            ("https://example.com/careers#top", "https://example.com/careers"),
            # Bare host gets a stub scheme added so urlparse populates netloc.
            ("example.com/careers", "https://example.com/careers"),
        ],
    )
    def test_equal_inputs_produce_equal_keys(self, a, b):
        assert _canonical_careers_url(a) == _canonical_careers_url(b), (
            f"{a!r} and {b!r} should canonicalize to the same key"
        )

    @pytest.mark.parametrize(
        "a, b",
        [
            ("https://example.com/careers", "https://example.com/jobs"),
            ("https://example.com/careers", "https://different.com/careers"),
            ("https://example.com/team-a", "https://example.com/team-b"),
        ],
    )
    def test_distinct_inputs_produce_distinct_keys(self, a, b):
        assert _canonical_careers_url(a) != _canonical_careers_url(b)

    def test_empty_returns_empty(self):
        assert _canonical_careers_url("") == ""
        assert _canonical_careers_url(None) == ""
        assert _canonical_careers_url("  ") == ""


class TestMergeByAtsSlug:
    def test_same_platform_slug_merges(self, migrated_db):
        path, conn = migrated_db
        keep = _insert_company(
            conn, "Sony Interactive Entertainment",
            ats_platform="greenhouse", ats_slug="sonyinteractiveentertainmentglobal",
            jobs_found_total=42,
        )
        orphan = _insert_company(
            conn, "PlayStation",
            ats_platform="greenhouse", ats_slug="sonyinteractiveentertainmentglobal",
            jobs_found_total=3,
        )
        _insert_job_for_company(conn, "test|j1", orphan)

        _run_m063(path, conn)

        assert _company_exists(conn, keep)
        assert not _company_exists(conn, orphan)
        # Jobs re-pointed
        company_after = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'test|j1'"
        ).fetchone()[0]
        assert company_after == keep

    def test_canonical_pick_is_highest_jobs_found_total(self, migrated_db):
        """When ats_slug matches, the row with more historical jobs wins
        regardless of insertion order. Keeps the more-active row."""
        path, conn = migrated_db
        first = _insert_company(
            conn, "TRM",
            ats_platform="ashby", ats_slug="trm-labs",
            jobs_found_total=2,
        )
        second = _insert_company(
            conn, "TRM Labs",
            ats_platform="ashby", ats_slug="trm-labs",
            jobs_found_total=15,
        )

        _run_m063(path, conn)

        assert not _company_exists(conn, first)
        assert _company_exists(conn, second)

    def test_different_platforms_dont_merge(self, migrated_db):
        """Same slug across different platforms is still two different
        boards (greenhouse vs lever 'acme' are unrelated endpoints)."""
        path, conn = migrated_db
        a = _insert_company(conn, "Acme", ats_platform="greenhouse", ats_slug="acme")
        b = _insert_company(conn, "Acme", ats_platform="lever", ats_slug="acme")

        _run_m063(path, conn)

        assert _company_exists(conn, a)
        assert _company_exists(conn, b)

    def test_null_slug_means_no_board_identity(self, migrated_db):
        """Companies with NULL ats_platform/slug AND NULL careers_url cannot
        be merged — they have no board signal. The user's 'ncidia' /
        '2100 nvidia usa' duplicates fall into this cohort if they were
        never probed."""
        path, conn = migrated_db
        a = _insert_company(conn, "ncidia")
        b = _insert_company(conn, "2100 nvidia usa")

        _run_m063(path, conn)

        assert _company_exists(conn, a)
        assert _company_exists(conn, b)


class TestMergeByCareersUrl:
    def test_same_canonical_url_merges(self, migrated_db):
        path, conn = migrated_db
        keep = _insert_company(
            conn, "Empower Retirement",
            careers_url="https://jobs.empower.com/",
            jobs_found_total=10,
        )
        orphan = _insert_company(
            conn, "Empower",
            careers_url="http://www.jobs.empower.com",
            jobs_found_total=1,
        )

        _run_m063(path, conn)

        assert _company_exists(conn, keep)
        assert not _company_exists(conn, orphan)

    def test_trailing_query_string_normalized(self, migrated_db):
        path, conn = migrated_db
        keep = _insert_company(
            conn, "Yahoo",
            careers_url="https://www.yahooinc.com/careers/",
            jobs_found_total=5,
        )
        orphan = _insert_company(
            conn, "Yahoo!",
            careers_url="https://yahooinc.com/careers?utm_source=referral",
            jobs_found_total=0,
        )

        _run_m063(path, conn)

        assert _company_exists(conn, keep)
        assert not _company_exists(conn, orphan)

    def test_different_paths_dont_merge(self, migrated_db):
        path, conn = migrated_db
        a = _insert_company(conn, "AcmeJobs", careers_url="https://acme.com/jobs")
        b = _insert_company(conn, "AcmeCareers", careers_url="https://acme.com/careers")

        _run_m063(path, conn)

        assert _company_exists(conn, a)
        assert _company_exists(conn, b)


class TestSlugPassRunsBeforeUrlPass:
    """Pass 1 (slug) collapses first; Pass 2 (URL) only sees the survivors."""

    def test_slug_match_collapses_even_when_url_also_matches(self, migrated_db):
        path, conn = migrated_db
        keep = _insert_company(
            conn, "Big Co",
            ats_platform="greenhouse", ats_slug="bigco",
            careers_url="https://bigco.com/careers",
            jobs_found_total=20,
        )
        orphan = _insert_company(
            conn, "BigCo Inc",
            ats_platform="greenhouse", ats_slug="bigco",
            careers_url="https://www.bigco.com/careers/",
            jobs_found_total=0,
        )

        _run_m063(path, conn)

        # Single merge; no double-counting / no leftover orphan.
        assert _company_exists(conn, keep)
        assert not _company_exists(conn, orphan)
        assert conn.execute(
            "SELECT COUNT(*) FROM companies WHERE name LIKE '%BigCo%' OR name LIKE 'Big Co'"
        ).fetchone()[0] == 1


class TestIdempotence:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        keep = _insert_company(
            conn, "Co A",
            ats_platform="lever", ats_slug="shared",
            jobs_found_total=5,
        )
        orphan = _insert_company(
            conn, "Co B",
            ats_platform="lever", ats_slug="shared",
            jobs_found_total=1,
        )

        _run_m063(path, conn)
        assert _company_exists(conn, keep)
        assert not _company_exists(conn, orphan)

        _run_m063(path, conn)  # second pass: no clusters with >1 row
        assert _company_exists(conn, keep)
        assert conn.execute(
            "SELECT COUNT(*) FROM companies WHERE ats_platform = 'lever' AND ats_slug = 'shared'"
        ).fetchone()[0] == 1


class TestEmptyDatabase:
    def test_no_companies_is_noop(self, migrated_db):
        path, conn = migrated_db
        _run_m063(path, conn)  # must not raise
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
