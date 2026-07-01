"""Tests for Migration 114 — re-tag first_seen-copy posted_dates mislabeled exact -> proxy.

Covers:
  - exact+copy rows (posted_date = first_seen) are re-tagged to proxy
  - exact+genuine rows (posted_date != first_seen) remain exact
  - migration is idempotent (second apply is a no-op)
  - approximate and proxy rows are untouched
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import MigrationContext
from job_finder.web.migrations import m114_retag_first_seen_copy_to_proxy as m114
from job_finder.web.migrations._runner import _apply_migration


@pytest.fixture
def conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
    os.remove(path)


class TestRetagFirstSeenCopyToProxy:
    def test_exact_copy_retagged_to_proxy(self, conn):
        """Rows with posted_date = first_seen are first_seen copies, should be proxy."""
        # Insert an exact+copy row (the bug case)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('copy', 't', 'c1', '', '[\"Greenhouse\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "'2026-01-01T00:00:00.123456', 'exact')"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'copy'"
        ).fetchone()
        assert row["posted_date_precision"] == "proxy"

    def test_exact_genuine_untouched(self, conn):
        """Rows with posted_date != first_seen are genuine exact timestamps."""
        # Insert an exact+genuine row (posted_date != first_seen)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('genuine', 't', 'c2', '', '[\"Greenhouse\"]', '[]', "
            "'2026-01-05T00:00:00.123456', '2026-01-05T00:00:00.123456', "
            "'2026-01-01T00:00:00.000000', 'exact')"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'genuine'"
        ).fetchone()
        assert row["posted_date_precision"] == "exact"

    def test_approximate_rows_untouched(self, conn):
        """Approximate rows are not affected by this migration."""
        # Insert an approximate row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('approx', 't', 'c3', '', '[\"linkedin\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "'2026-01-01T00:00:00.123456', 'approximate')"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'approx'"
        ).fetchone()
        assert row["posted_date_precision"] == "approximate"

    def test_proxy_rows_untouched(self, conn):
        """Proxy rows are not affected by this migration."""
        # Insert a proxy row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('proxy', 't', 'c4', '', '[\"linkedin\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "'2026-01-01T00:00:00.123456', 'proxy')"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'proxy'"
        ).fetchone()
        assert row["posted_date_precision"] == "proxy"

    def test_idempotent(self, conn):
        """Running the migration twice is a no-op after the first correction."""
        # Insert an exact+copy row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('copy', 't', 'c1', '', '[\"Greenhouse\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "'2026-01-01T00:00:00.123456', 'exact')"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )

        # First run
        _apply_migration(ctx, m114.MIGRATION)
        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'copy'"
        ).fetchone()
        assert row["posted_date_precision"] == "proxy"

        # Second run (should be no-op)
        _apply_migration(ctx, m114.MIGRATION)
        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'copy'"
        ).fetchone()
        assert row["posted_date_precision"] == "proxy"

    def test_null_dates_untouched(self, conn):
        """Rows with NULL posted_date are not affected."""
        # Insert a row with NULL posted_date
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('nodate', 't', 'c5', '', '[\"linkedin\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "NULL, NULL)"
        )
        conn.commit()

        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'nodate'"
        ).fetchone()
        assert row["posted_date_precision"] is None

    def test_trigger_compatibility(self, conn):
        """The migration passes the m095 I-14 pairing/domain triggers."""
        # Insert an exact+copy row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date, posted_date_precision) VALUES "
            "('copy', 't', 'c1', '', '[\"Greenhouse\"]', '[]', "
            "'2026-01-01T00:00:00.123456', '2026-01-01T00:00:00.123456', "
            "'2026-01-01T00:00:00.123456', 'exact')"
        )
        conn.commit()

        # The migration should not trigger the I-14 abort
        ctx = MigrationContext(
            conn=conn, db_path=":memory:", user_data_root=".", initial_version=113
        )
        _apply_migration(ctx, m114.MIGRATION)

        # Verify the update succeeded
        row = conn.execute(
            "SELECT posted_date_precision FROM jobs WHERE dedup_key = 'copy'"
        ).fetchone()
        assert row["posted_date_precision"] == "proxy"
