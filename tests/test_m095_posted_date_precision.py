"""Tests for Migration 95 + precedence-based posted_date upsert (#363).

Covers:
  - m095 column add + backfill (exact for ATS-membership rows, proxy others)
  - I-14 pairing/domain triggers reject inconsistent direct writes
  - upsert precedence: exact overwrites proxy; proxy never overwrites exact;
    equal precision keeps the stored value; NULL-fill still works
  - unmarked dated jobs default to 'proxy' at the boundary
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime

import pytest

from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.db_migrate import run_migrations

_DT_EXACT = datetime(2026, 6, 1, 9, 0, 0)
_DT_PROXY = datetime(2026, 6, 5, 12, 0, 0)


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


def _upsert(conn, *, posted_date=None, precision=None, source="linkedin", url_n=1):
    job = Job(
        title="Senior Data Scientist",
        company="Acme",
        location="Remote",
        source=source,
        source_url=f"https://example.com/{url_n}",
        posted_date=posted_date,
        posted_date_precision=precision,
    )
    return ParsedJob.from_job(job), conn


def _row(conn):
    return conn.execute("SELECT posted_date, posted_date_precision FROM jobs").fetchone()


# ---------------------------------------------------------------------------
# Upsert precedence
# ---------------------------------------------------------------------------


class TestUpsertPrecedence:
    def test_unmarked_dated_insert_defaults_to_proxy(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_PROXY)
        upsert_job(conn, parsed)
        row = _row(conn)
        assert row["posted_date"] == "2026-06-05T12:00:00"
        assert row["posted_date_precision"] == "proxy"

    def test_undated_insert_stores_null_pair(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn)
        upsert_job(conn, parsed)
        row = _row(conn)
        assert row["posted_date"] is None
        assert row["posted_date_precision"] is None

    def test_exact_overwrites_proxy(self, conn):
        """The first-writer-wins lock is gone: ATS exact corrects email proxy."""
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_PROXY)  # email-proxy date
        upsert_job(conn, parsed)
        parsed2, _ = _upsert(conn, posted_date=_DT_EXACT, precision="exact", url_n=2)
        result = upsert_job(conn, parsed2)
        row = _row(conn)
        assert row["posted_date"] == "2026-06-01T09:00:00"
        assert row["posted_date_precision"] == "exact"
        assert result.kind == "updated"

    def test_proxy_does_not_overwrite_exact(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_EXACT, precision="exact")
        upsert_job(conn, parsed)
        parsed2, _ = _upsert(conn, posted_date=_DT_PROXY, url_n=2)  # proxy
        upsert_job(conn, parsed2)
        row = _row(conn)
        assert row["posted_date"] == "2026-06-01T09:00:00"
        assert row["posted_date_precision"] == "exact"

    def test_equal_precision_keeps_existing(self, conn):
        """Repeat exact sightings never churn the date (Ashby repost bumps)."""
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_EXACT, precision="exact")
        upsert_job(conn, parsed)
        parsed2, _ = _upsert(conn, posted_date=_DT_PROXY, precision="exact", url_n=2)
        upsert_job(conn, parsed2)
        assert _row(conn)["posted_date"] == "2026-06-01T09:00:00"

    def test_null_fill_still_works(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn)  # no date
        upsert_job(conn, parsed)
        parsed2, _ = _upsert(conn, posted_date=_DT_PROXY, url_n=2)
        upsert_job(conn, parsed2)
        row = _row(conn)
        assert row["posted_date"] == "2026-06-05T12:00:00"
        assert row["posted_date_precision"] == "proxy"

    def test_approximate_sits_between(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_PROXY)  # proxy
        upsert_job(conn, parsed)
        parsed2, _ = _upsert(conn, posted_date=_DT_EXACT, precision="approximate", url_n=2)
        upsert_job(conn, parsed2)
        assert _row(conn)["posted_date_precision"] == "approximate"
        parsed3, _ = _upsert(conn, posted_date=_DT_PROXY, url_n=3)  # proxy again
        upsert_job(conn, parsed3)
        assert _row(conn)["posted_date_precision"] == "approximate"


# ---------------------------------------------------------------------------
# I-14 triggers
# ---------------------------------------------------------------------------


class TestI14Triggers:
    def test_direct_insert_dated_without_precision_aborts(self, conn):
        with pytest.raises(sqlite3.IntegrityError, match="I-14"):
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, sources, "
                "source_urls, first_seen, last_seen, posted_date) "
                "VALUES ('k', 't', 'c', '', '[]', '[]', '2026-01-01T00:00:00', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )

    def test_direct_update_clearing_date_only_aborts(self, conn):
        from job_finder.db import upsert_job

        parsed, _ = _upsert(conn, posted_date=_DT_EXACT, precision="exact")
        upsert_job(conn, parsed)
        with pytest.raises(sqlite3.IntegrityError, match="I-14"):
            conn.execute("UPDATE jobs SET posted_date = NULL")

    def test_domain_enforced(self, conn):
        with pytest.raises(sqlite3.IntegrityError, match="I-14"):
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, sources, "
                "source_urls, first_seen, last_seen, posted_date, posted_date_precision) "
                "VALUES ('k', 't', 'c', '', '[]', '[]', '2026-01-01T00:00:00', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 'bogus')"
            )


# ---------------------------------------------------------------------------
# m095 backfill classification
# ---------------------------------------------------------------------------


class TestBackfill:
    def _legacy_db(self, tmp_path):
        """Fully migrated DB rewound to the pre-m095 shape (no column/triggers)."""
        path = str(tmp_path / "legacy.db")
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TRIGGER IF EXISTS tg_jobs_posted_date_precision_pairing_ins")
        conn.execute("DROP TRIGGER IF EXISTS tg_jobs_posted_date_precision_pairing_upd")
        conn.execute("ALTER TABLE jobs DROP COLUMN posted_date_precision")
        conn.commit()
        return conn, path

    def test_backfill_tags_ats_membership_exact_others_proxy(self, tmp_path):
        from job_finder.web.migrations.m095_posted_date_precision import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        conn, path = self._legacy_db(tmp_path)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date) VALUES "
            "('gh', 't', 'c1', '', '[\"linkedin\", \"Greenhouse\"]', '[]', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date) VALUES "
            "('email', 't', 'c2', '', '[\"linkedin\"]', '[]', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls, "
            "first_seen, last_seen, posted_date) VALUES "
            "('undated', 't', 'c3', '', '[\"linkedin\"]', '[]', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00', NULL)"
        )
        conn.commit()

        MIGRATION.py(
            MigrationContext(
                conn=conn, db_path=path, user_data_root=str(tmp_path), initial_version=94
            )
        )

        rows = {
            r["dedup_key"]: r["posted_date_precision"]
            for r in conn.execute("SELECT dedup_key, posted_date_precision FROM jobs")
        }
        assert rows == {"gh": "exact", "email": "proxy", "undated": None}
        conn.close()
