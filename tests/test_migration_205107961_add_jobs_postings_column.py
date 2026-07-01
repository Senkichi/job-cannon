"""Tests for migration m205107961 — add jobs.postings column (#640)."""

from __future__ import annotations

import sqlite3

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import MIGRATIONS, MigrationContext
from job_finder.web.migrations._runner import _apply_migration
from job_finder.web.migrations.m205107961_add_jobs_postings_column import MIGRATION


def _jobs_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}


def test_migration_adds_postings_column_empty_db(tmp_path):
    """Applying to an empty DB adds the postings column with default '[]'."""
    db_path = str(tmp_path / "empty.db")
    run_migrations(db_path)  # fresh DB → all migrations including m205107961

    with standalone_connection(db_path) as conn:
        cols = _jobs_columns(conn)
        assert "postings" in cols, "m205107961 should add the jobs.postings column"

        # Verify default value on a new row
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('test|engineer', 'Engineer', 'TestCo', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = 'test|engineer'"
        ).fetchone()
        assert row is not None
        assert row[0] == "[]", "new rows should default to '[]'"


def test_migration_adds_postings_column_populated_db(tmp_path):
    """Applying to a populated DB adds the column and existing rows get '[]'."""
    db_path = str(tmp_path / "populated.db")

    # Apply migrations up to just before m205107961
    root = str(tmp_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=0)
        for migration in MIGRATIONS:
            if migration.version < MIGRATION.version:
                _apply_migration(ctx, migration)

        # Insert a row before the migration
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('test|engineer', 'Engineer', 'TestCo', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        assert "postings" not in _jobs_columns(conn)

    # Now apply m205107961
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=0)
        _apply_migration(ctx, MIGRATION)

        assert "postings" in _jobs_columns(conn)

        # Verify existing row got the default
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = 'test|engineer'"
        ).fetchone()
        assert row is not None
        assert row[0] == "[]", "existing rows should get '[]' default"


def test_migration_is_idempotent(tmp_path):
    """Re-applying the migration is a no-op (swallows 'duplicate column name')."""
    db_path = str(tmp_path / "idem.db")
    root = str(tmp_path)

    # First run full migrations to get the schema
    run_migrations(db_path)

    with standalone_connection(db_path) as conn:
        assert "postings" in _jobs_columns(conn)

        # Now try to re-apply just this migration (should be a no-op)
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=0)
        _apply_migration(ctx, MIGRATION)
        assert "postings" in _jobs_columns(conn)

        # Verify we can still read/write the column
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('test|engineer', 'Engineer', 'TestCo', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = 'test|engineer'"
        ).fetchone()
        assert row is not None
        assert row[0] == "[]"
