"""Tests for migration m113 — drop the vestigial ``jobs.score`` column.

m113 finishes the v3.0 "Plan 4" migration tail: it retires the I-03 contract
triggers (whose WHEN clause references ``NEW.score``), drops ``idx_jobs_score``,
then drops the ``score`` column itself. These tests assert the column / index /
triggers are gone at HEAD, that pre-existing rows survive the drop, and that the
migration is idempotent.
"""

from __future__ import annotations

import os
import sqlite3

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations import MIGRATIONS, MigrationContext
from job_finder.web.migrations import m113_drop_legacy_score_column as m113
from job_finder.web.migrations._runner import _apply_migration

# The two I-03 triggers (m078) that reference NEW.score and are retired by m113.
_I03_TRIGGERS = {
    "tg_jobs_scoring_provider_when_scored_ins",
    "tg_jobs_scoring_provider_when_scored_upd",
}


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


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _trigger_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def test_m113_drops_score_column_index_and_i03_triggers_at_head(tmp_path):
    """At fully-migrated HEAD the score column, its index, and I-03 are gone."""
    db_path = str(tmp_path / "head.db")
    run_migrations(db_path)  # fresh DB → initial_version 0 → backup gate bypassed
    conn = sqlite3.connect(db_path)
    try:
        cols = _jobs_columns(conn)
        assert "score" not in cols, "m113 should drop the jobs.score column"
        # score_breakdown is out of scope for this drop and must survive.
        assert "score_breakdown" in cols
        assert "idx_jobs_score" not in _index_names(conn)
        assert _I03_TRIGGERS.isdisjoint(_trigger_names(conn)), (
            "I-03 triggers reference NEW.score and must be retired by m113"
        )
    finally:
        conn.close()


def test_m113_preserves_existing_rows(tmp_path, monkeypatch):
    """A row that existed before m113 (carrying a score value) survives the drop."""
    db_path = str(tmp_path / "pre.db")
    _apply_up_to(db_path, 112)  # score column + I-03 triggers still present here

    with standalone_connection(db_path) as conn:
        # scoring_provider satisfies the (still-live) I-03 trigger for a scored row.
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "score, scoring_provider) VALUES "
            "('acme|engineer', 'Engineer', 'Acme', 'Remote', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00', 7.5, 'heuristic')"
        )
        conn.commit()
        assert "score" in _jobs_columns(conn)

    # m113's backup gate sees initial_version=112 (>0); use the documented override
    # rather than fabricating a tarball backup.
    monkeypatch.setenv("GSD_BACKUP_CONFIRMED", "1")
    run_migrations(db_path, user_data_root=str(tmp_path))

    with standalone_connection(db_path) as conn:
        assert "score" not in _jobs_columns(conn)
        # Query by company (stable across the standing dedup/title post-hooks).
        row = conn.execute(
            "SELECT title, company, scoring_provider FROM jobs WHERE company='Acme'"
        ).fetchone()
        assert row is not None, "the pre-m113 row was lost during the column drop"
        assert row[0] == "Engineer"
        assert row[2] == "heuristic"


def test_m113_is_idempotent(tmp_path, monkeypatch):
    """Re-applying m113 after the column is already gone is a no-op, not an error."""
    db_path = str(tmp_path / "idem.db")
    _apply_up_to(db_path, 112)
    monkeypatch.setenv("GSD_BACKUP_CONFIRMED", "1")

    root = str(tmp_path)
    with standalone_connection(db_path) as conn:
        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=root, initial_version=112
        )
        _apply_migration(ctx, m113.MIGRATION)  # first apply: drops column/index/triggers
        # Second apply must be a no-op: DROP ... IF EXISTS + the "no such column"
        # swallow in _apply_migration for re-run destructive migrations.
        _apply_migration(ctx, m113.MIGRATION)
        assert "score" not in _jobs_columns(conn)
