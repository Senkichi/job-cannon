"""Tests for Migration 65 — add `last_tick_at` heartbeat column.

The migration adds a single nullable TEXT column to `batch_score_sessions`.
The column powers the heartbeat-based polling-timeout fix in
``render_polling_status`` (companion edits in the same session).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from job_finder.web.db_migrate import MIGRATIONS, run_migrations
from job_finder.web.migrations import Migration


def _get(version: int) -> Migration:
    for m in MIGRATIONS:
        if m.version == version:
            return m
    pytest.fail(f"Migration {version} not in MIGRATIONS")


class TestMigration065Shape:
    def test_migration_065_present(self):
        m = _get(65)
        assert m.version == 65
        assert "last_tick_at" in m.description
        assert "heartbeat" in m.description

    def test_migration_065_is_sql_only_no_py_hook(self):
        m = _get(65)
        assert m.py is None
        assert m.sql == [
            "ALTER TABLE batch_score_sessions ADD COLUMN last_tick_at TEXT"
        ]


class TestMigration065Behavior:
    def test_adds_last_tick_at_column_on_fresh_db(self, tmp_db_path):
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            cols = [
                r[1]
                for r in conn.execute("PRAGMA table_info(batch_score_sessions)").fetchall()
            ]
        assert "last_tick_at" in cols, (
            f"m065 did not add last_tick_at to batch_score_sessions. Columns: {cols}"
        )

    def test_column_is_nullable_text(self, tmp_db_path):
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            info = {
                r[1]: {"type": r[2], "notnull": r[3], "dflt_value": r[4]}
                for r in conn.execute("PRAGMA table_info(batch_score_sessions)").fetchall()
            }
        col = info["last_tick_at"]
        assert col["type"].upper() == "TEXT"
        assert col["notnull"] == 0
        assert col["dflt_value"] is None

    def test_user_version_after_run_is_65(self, tmp_db_path):
        run_migrations(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
        assert v == 65

    def test_existing_rows_have_null_last_tick_at(self, tmp_db_path):
        """Pre-existing rows survive the column add with NULL last_tick_at."""
        # Migrate first so the table exists.
        run_migrations(tmp_db_path)
        # Drop the new column to simulate a pre-m065 state.
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            # Sqlite supports DROP COLUMN as of 3.35. Reset user_version to 64
            # to trigger m065 re-application.
            conn.execute("ALTER TABLE batch_score_sessions DROP COLUMN last_tick_at")
            conn.execute("INSERT INTO batch_score_sessions (session_type, status, started_at) "
                         "VALUES ('scoring', 'running', '2026-05-27T00:00:00')")
            conn.execute("PRAGMA user_version = 64")
            conn.commit()

        # Re-run m065.
        run_migrations(tmp_db_path)

        with closing(sqlite3.connect(tmp_db_path)) as conn:
            row = conn.execute(
                "SELECT status, last_tick_at FROM batch_score_sessions"
            ).fetchone()
        assert row[0] == "running"
        assert row[1] is None, (
            "Pre-existing row should have NULL last_tick_at after column add. "
            "render_polling_status COALESCEs to started_at for these."
        )
