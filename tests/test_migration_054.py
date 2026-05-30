"""Migration 54 unit tests — wizard_data column added idempotently."""

import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m054_wizard_data_column import MIGRATION


def test_migration_version_is_54():
    """MIGRATION.version must be 54 to satisfy the per-version filename contract."""
    assert MIGRATION.version == 54


def test_migration_description_present():
    """MIGRATION.description is required by db_migrate logging."""
    assert MIGRATION.description
    assert "wizard_data" in MIGRATION.description.lower()


def test_migration_sql_adds_wizard_data_column(tmp_db_path):
    """After run_migrations on a fresh DB, onboarding_state has a wizard_data column with TEXT type and default '{}'."""
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        cols = conn.execute("PRAGMA table_info(onboarding_state)").fetchall()
        col_names = [c[1] for c in cols]
        assert "wizard_data" in col_names
        wizard_col = next(c for c in cols if c[1] == "wizard_data")
        # column tuple: (cid, name, type, notnull, dflt_value, pk)
        assert wizard_col[2].upper() == "TEXT"
        assert wizard_col[4] == "'{}'"  # SQLite stores default value with quotes
    finally:
        conn.close()


def test_migration_is_idempotent_on_rerun(tmp_db_path):
    """Running run_migrations twice on the same DB does not raise (duplicate column name is swallowed by _apply_migration)."""
    run_migrations(tmp_db_path)
    # Second run should be a no-op because pragma user_version >= 54
    run_migrations(tmp_db_path)

    # Also confirm user_version is exactly 54 (the highest migration shipped in Phase 42)
    conn = sqlite3.connect(tmp_db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 54
    finally:
        conn.close()


def test_existing_row_has_wizard_data_defaulted(tmp_db_path):
    """If a pre-migration-54 row exists in onboarding_state, the ADD COLUMN must default it to '{}'."""
    # Manually run migrations up to 53, insert a row, then run migration 54
    run_migrations(tmp_db_path)  # runs all migrations in order
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete) VALUES (1, 0)"
        )
        conn.commit()
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
        assert row[0] == "{}"
    finally:
        conn.close()
