"""Tests for Migration 67 — backfill locations_structured / workplace_type / primary_country_code.

The migration re-parses every existing row's `locations_raw` through
`parse_locations(raw, jd_full=row.jd_full)` and writes the three m066
columns. Idempotent — the parser is a fixed point on its own output.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from job_finder.web.db_migrate import MIGRATIONS, run_migrations
from job_finder.web.migrations import Migration
from job_finder.web.migrations.types import MigrationContext


def _get(version: int) -> Migration:
    for m in MIGRATIONS:
        if m.version == version:
            return m
    pytest.fail(f"Migration {version} not in MIGRATIONS")


class TestMigration067Shape:
    def test_migration_067_present(self):
        m = _get(67)
        assert m.version == 67
        assert "backfill" in m.description
        assert "locations_structured" in m.description

    def test_migration_067_uses_py_hook_not_sql(self):
        """m067 is pure data backfill — no DDL, all work in a Python helper."""
        m = _get(67)
        assert m.py is not None
        assert m.sql == []


class TestMigration067Behavior:
    def test_backfill_resolved_city(self, tmp_path):
        """Single ``San Francisco, CA`` row gets all 3 m066 cols populated."""
        db_path = str(tmp_path / "jobs.db")
        # Apply up to m066 first so schema is ready.
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, sources, source_urls, pipeline_status, "
                "first_seen, last_seen, locations_structured, workplace_type, "
                "primary_country_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, NULL)",
                (
                    "k1",
                    "SWE",
                    "Acme",
                    "San Francisco, CA",
                    json.dumps(["San Francisco, CA"]),
                    "[]",
                    "[]",
                    "discovered",
                    "2026-05-27",
                    "2026-05-27",
                ),
            )
            conn.commit()

        # Re-run migrations: m067 is already at the latest version
        # (PRAGMA user_version=67), so by default it won't re-fire.
        # Force a re-run by manually rolling user_version back to 66.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA user_version = 66")
            conn.commit()
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = ?",
                ("k1",),
            ).fetchone()

        assert r["primary_country_code"] == "US"
        assert r["workplace_type"] == "UNSPECIFIED"
        assert r["locations_structured"] is not None
        parsed = json.loads(r["locations_structured"])
        assert isinstance(parsed, list)
        assert parsed[0]["city"] == "San Francisco"
        assert parsed[0]["region_code"] == "CA"
        assert parsed[0]["country_code"] == "US"

    def test_backfill_uses_jd_full_for_li_remote(self, tmp_path):
        """Row with UNSPECIFIED location + ``#LI-Remote`` in jd_full → REMOTE.

        Verifies SPEC Q3 (jd_full body keyword fallback) is wired through
        the backfill — without it, this row would land with
        workplace_type=UNSPECIFIED.
        """
        db_path = str(tmp_path / "jobs.db")
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, jd_full, sources, source_urls, pipeline_status, "
                "first_seen, last_seen, locations_structured, workplace_type, "
                "primary_country_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, NULL)",
                (
                    "k2",
                    "SWE",
                    "Acme",
                    "Toronto, ON",
                    json.dumps(["Toronto, ON"]),
                    "Senior engineer role. #LI-Remote tag at bottom.",
                    "[]",
                    "[]",
                    "discovered",
                    "2026-05-27",
                    "2026-05-27",
                ),
            )
            conn.execute("PRAGMA user_version = 66")
            conn.commit()
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT workplace_type, primary_country_code FROM jobs WHERE dedup_key = ?",
                ("k2",),
            ).fetchone()
        assert r["workplace_type"] == "REMOTE"
        assert r["primary_country_code"] == "CA"

    def test_backfill_skips_empty_locations_raw(self, tmp_path):
        """Row with locations_raw='[]' and no location → all 3 cols stay NULL.

        Tests m067's own behavior in isolation. Running the full migration
        chain via run_migrations after INSERTing the row would also trigger
        m072, which backfills NULL workplace_type → 'UNSPECIFIED' as a
        default sentinel. That would mask whether m067 itself fabricated a
        value. To pin m067 specifically, we use run_migrations only to set
        up the schema, INSERT the row after all migrations have already
        completed (so m072 cannot see it), then invoke m067's py hook
        directly.
        """
        db_path = str(tmp_path / "jobs.db")
        # Schema setup only: m072 runs here, but the test row doesn't exist
        # yet so its NULL-default backfill has nothing to touch.
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, sources, source_urls, pipeline_status, "
                "first_seen, last_seen, locations_structured, workplace_type, "
                "primary_country_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, NULL)",
                (
                    "k3",
                    "SWE",
                    "Acme",
                    "",
                    "[]",
                    "[]",
                    "[]",
                    "discovered",
                    "2026-05-27",
                    "2026-05-27",
                ),
            )
            conn.commit()

        # Re-fire ONLY m067 against the post-INSERT state.
        with closing(sqlite3.connect(db_path)) as conn:
            _get(67).py(  # type: ignore[misc]
                MigrationContext(
                    conn=conn,
                    db_path=db_path,
                    user_data_root=str(tmp_path),
                )
            )
            conn.commit()

        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = ?",
                ("k3",),
            ).fetchone()
        # Strict: m067 must not fabricate any of the 3 cols when the parser
        # returns []. (m072's NULL-default backfill is bypassed here by
        # design — m072 already ran before the row was inserted.)
        assert r["locations_structured"] is None
        assert r["workplace_type"] is None
        assert r["primary_country_code"] is None

    def test_backfill_idempotent_rerun(self, tmp_path):
        """Re-running m067 produces the same writes (parser is a fixed point)."""
        db_path = str(tmp_path / "jobs.db")
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, sources, source_urls, pipeline_status, "
                "first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "k4",
                    "SWE",
                    "Acme",
                    "London, UK",
                    json.dumps(["London, UK"]),
                    "[]",
                    "[]",
                    "discovered",
                    "2026-05-27",
                    "2026-05-27",
                ),
            )
            conn.execute("PRAGMA user_version = 66")
            conn.commit()
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            r1 = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = ?",
                ("k4",),
            ).fetchone()
            conn.execute("PRAGMA user_version = 66")
            conn.commit()
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            r2 = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = ?",
                ("k4",),
            ).fetchone()
        assert r1 == r2

    def test_backfill_no_op_when_jobs_table_missing(self, tmp_path):
        """m067 is a no-op on a fresh DB where jobs table doesn't yet exist."""
        db_path = str(tmp_path / "jobs.db")
        # Bare migrations through m067 — should succeed without crashing.
        run_migrations(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
        assert v >= 67


class TestMigration067RegistryInvariants:
    def test_migration_count_at_least_67(self):
        """Sentinel: ``len(MIGRATIONS) >= 67`` after m067 lands."""
        assert len(MIGRATIONS) >= 67

    def test_user_version_advances_to_67(self, tmp_path):
        """After a clean migration run the live DB reports user_version >= 67."""
        db_path = str(tmp_path / "jobs.db")
        run_migrations(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] >= 67
