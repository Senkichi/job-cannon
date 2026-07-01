"""Tests for migration m116 — add ats_refreshed_at column (#575).

m116 adds a nullable TEXT column ``ats_refreshed_at`` to the jobs table for
capturing the mutable refresh timestamp from ATS public JSON (Greenhouse
``updated_at``). This is the CAPTURE stage of the data-integrity overhaul
(epic #393); normalization/consumption (the divergence/repost flag) is a
downstream stage, out of scope here.

These tests assert the column is added at HEAD, that existing rows get NULL
(ingest-forward only — no backfill), and that the migration is idempotent.
"""

from __future__ import annotations

import os
import sqlite3

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import MIGRATIONS, MigrationContext
from job_finder.web.migrations import m116_ats_refresh_timestamp as m116
from job_finder.web.migrations._runner import _apply_migration


def _apply_up_to(db_path: str, max_version: int) -> None:
    """Apply every migration with version <= max_version to db_path."""
    root = os.path.dirname(db_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=0)
        for migration in MIGRATIONS:
            if migration.version <= max_version:
                _apply_migration(ctx, migration)


def _jobs_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}


def test_m116_adds_ats_refreshed_at_column_at_head(tmp_path):
    """At fully-migrated HEAD the ats_refreshed_at column exists and is nullable."""
    db_path = str(tmp_path / "head.db")
    run_migrations(db_path)  # fresh DB → initial_version 0 → backup gate bypassed
    conn = sqlite3.connect(db_path)
    try:
        cols = _jobs_columns(conn)
        assert "ats_refreshed_at" in cols, "m116 should add the ats_refreshed_at column"
        # Verify the column is nullable by inserting a row with NULL
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) VALUES "
            "('acme|engineer', 'Engineer', 'Acme', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT ats_refreshed_at FROM jobs WHERE dedup_key='acme|engineer'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, "ats_refreshed_at should be NULL by default"
    finally:
        conn.close()


def test_m116_existing_rows_get_null(tmp_path):
    """A row that existed before m116 gets NULL for ats_refreshed_at (no backfill)."""
    db_path = str(tmp_path / "pre.db")
    _apply_up_to(db_path, 114)  # ats_refreshed_at column does not exist yet

    with standalone_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) VALUES "
            "('acme|engineer', 'Engineer', 'Acme', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        assert "ats_refreshed_at" not in _jobs_columns(conn)

    # Apply m116
    root = str(tmp_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=root, initial_version=114
        )
        _apply_migration(ctx, m116.MIGRATION)

    with standalone_connection(db_path) as conn:
        assert "ats_refreshed_at" in _jobs_columns(conn)
        row = conn.execute(
            "SELECT ats_refreshed_at FROM jobs WHERE dedup_key='acme|engineer'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, "Existing rows should get NULL (no backfill)"


def test_m116_is_idempotent(tmp_path):
    """Re-applying m116 after the column is already added is a no-op, not an error."""
    db_path = str(tmp_path / "idem.db")
    _apply_up_to(db_path, 114)

    root = str(tmp_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=root, initial_version=114
        )
        _apply_migration(ctx, m116.MIGRATION)  # first apply: adds column
        # Second apply must be a no-op: the runner swallows "duplicate column name"
        _apply_migration(ctx, m116.MIGRATION)
        assert "ats_refreshed_at" in _jobs_columns(conn)
