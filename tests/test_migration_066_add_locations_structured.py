"""Tests for Migration 66 — add canonical location columns to `jobs`.

The migration adds three nullable TEXT columns:

  - locations_structured (JSON-serialized list[JobLocation])
  - workplace_type      (REMOTE/HYBRID/ONSITE/UNSPECIFIED)
  - primary_country_code (ISO 3166-1 alpha-2)

All NULL on existing rows. No write-site reaches the new columns yet —
Commit C wires `upsert_job` and the Layer-1 scanners. m066 is pure
schema-add and zero-risk for existing reads (`location` / `locations_raw`
are untouched).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from job_finder.web.db_migrate import MIGRATIONS
from job_finder.web.migrations import Migration
from tests.helpers.contract_triggers import (
    run_migrations_without_contract as run_migrations,
)


def _get(version: int) -> Migration:
    for m in MIGRATIONS:
        if m.version == version:
            return m
    pytest.fail(f"Migration {version} not in MIGRATIONS")


class TestMigration066Shape:
    def test_migration_066_present(self):
        m = _get(66)
        assert m.version == 66
        assert "locations_structured" in m.description
        assert "workplace_type" in m.description
        assert "primary_country_code" in m.description

    def test_migration_066_is_sql_only_no_py_hook(self):
        m = _get(66)
        assert m.py is None
        assert m.sql == [
            "ALTER TABLE jobs ADD COLUMN locations_structured TEXT",
            "ALTER TABLE jobs ADD COLUMN workplace_type TEXT",
            "ALTER TABLE jobs ADD COLUMN primary_country_code TEXT",
        ]


class TestMigration066Behavior:
    def test_adds_all_three_columns_on_fresh_db(self, tmp_db_path):
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        for expected in ("locations_structured", "workplace_type", "primary_country_code"):
            assert expected in cols, f"m066 did not add {expected} to jobs. Columns: {cols}"

    def test_columns_are_nullable_text(self, tmp_db_path):
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            info = {
                r[1]: {"type": r[2], "notnull": r[3], "dflt_value": r[4]}
                for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
        for col_name in ("locations_structured", "workplace_type", "primary_country_code"):
            col = info[col_name]
            assert col["type"].upper() == "TEXT", f"{col_name} should be TEXT, got {col['type']}"
            assert col["notnull"] == 0, f"{col_name} must be nullable"
            assert col["dflt_value"] is None, f"{col_name} must have no default"

    def test_user_version_after_run_is_at_least_66(self, tmp_db_path):
        """After a clean migration run, PRAGMA user_version is >= 66.

        Forward-compat: round-10/11 convention. Future migrations don't
        need to re-edit this assertion. The m066-specific check is the
        column-add (covered by the cols-present tests above).
        """
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
        assert v >= 66

    def test_existing_legacy_columns_untouched(self, tmp_db_path):
        """`location` and `locations_raw` survive the column add unchanged.

        SPEC guarantee: m066 is purely additive; the display/filter strings
        every blueprint, template, and rescue path already reads stay as-is.
        """
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        assert "location" in cols, "m066 must not drop legacy `location` column"
        assert "locations_raw" in cols, "m066 must not drop legacy `locations_raw` column"

    def test_existing_rows_get_backfilled_through_m067(self, tmp_db_path):
        """A pre-m066 row gets the three new columns populated by m066+m067.

        Originally asserted the columns stayed NULL after just m066 ran
        — that guarantee held only between m066 and m067 shipping in the
        same release set. Now that m067 (backfill) is in the migration
        chain, a full run re-parses each legacy row's locations_raw and
        fills the three columns. Verifies the upgrade path end-to-end:
          - legacy `location` / `locations_raw` preserved (m066 invariant)
          - structured cols populated from parser output (m067 invariant)
        """
        run_migrations(tmp_db_path)
        # Reset state to simulate pre-m066: drop the new columns and roll
        # version back so re-running re-applies m066 and m067.
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            conn.execute("ALTER TABLE jobs DROP COLUMN locations_structured")
            conn.execute("ALTER TABLE jobs DROP COLUMN workplace_type")
            conn.execute("ALTER TABLE jobs DROP COLUMN primary_country_code")
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, first_seen, last_seen) "
                "VALUES ('legacy-1', 'SWE', 'Acme', 'San Francisco, CA', "
                "'San Francisco, CA', '2026-05-27T00:00:00', '2026-05-27T00:00:00')"
            )
            conn.execute("PRAGMA user_version = 65")
            conn.commit()

        run_migrations(tmp_db_path)

        with closing(sqlite3.connect(tmp_db_path)) as conn:
            row = conn.execute(
                "SELECT location, locations_raw, locations_structured, "
                "workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = 'legacy-1'"
            ).fetchone()
        assert row[0] == "San Francisco, CA"  # legacy location preserved
        assert row[1] == "San Francisco, CA"  # legacy locations_raw preserved
        # m067 backfill writes:
        assert row[2] is not None, "m067 must populate locations_structured"
        assert row[3] == "UNSPECIFIED"
        assert row[4] == "US"

    def test_new_columns_accept_writes_after_migration(self, tmp_db_path):
        """Schema admits the JSON/string values the parser is destined to write.

        Sanity smoke: an UPDATE writing the three columns succeeds and reads
        back verbatim. Commit C wires this for real via `upsert_job`.
        """
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, "
                "locations_raw, first_seen, last_seen) "
                "VALUES ('w-1', 'SWE', 'Acme', 'Remote - US', 'Remote - US', "
                "'2026-05-27T00:00:00', '2026-05-27T00:00:00')"
            )
            conn.execute(
                "UPDATE jobs SET locations_structured = ?, "
                "workplace_type = ?, primary_country_code = ? "
                "WHERE dedup_key = 'w-1'",
                (
                    '[{"country_code":"US","workplace_type":"REMOTE"}]',
                    "REMOTE",
                    "US",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT locations_structured, workplace_type, primary_country_code "
                "FROM jobs WHERE dedup_key = 'w-1'"
            ).fetchone()
        assert row[0] == '[{"country_code":"US","workplace_type":"REMOTE"}]'
        assert row[1] == "REMOTE"
        assert row[2] == "US"

    def test_idempotent_when_columns_already_present(self, tmp_db_path):
        """Re-running migrations on an already-migrated DB is a no-op.

        Verifies the `duplicate column name` swallow path in the runner
        does not break re-application — the standard `_apply_migration`
        invariant from m001 onward.
        """
        run_migrations(tmp_db_path)
        run_migrations(tmp_db_path)  # second call must not raise

        with closing(sqlite3.connect(tmp_db_path)) as conn:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        assert v >= 66
        # Columns are not duplicated.
        assert cols.count("locations_structured") == 1
        assert cols.count("workplace_type") == 1
        assert cols.count("primary_country_code") == 1


class TestMigration066RegistryInvariants:
    """Cross-check that m066 plugs into the auto-discovery pipeline cleanly."""

    def test_migrations_list_has_at_least_66_entries(self):
        assert len(MIGRATIONS) >= 66

    def test_max_migration_version_is_at_least_66(self):
        assert max(m.version for m in MIGRATIONS) >= 66

    def test_versions_strictly_monotonic_through_at_least_66(self):
        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions), "MIGRATIONS not sorted by version"
        assert len(set(versions)) == len(versions), "Duplicate migration version"
        assert versions[-1] >= 66
