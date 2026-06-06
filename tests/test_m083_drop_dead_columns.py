"""Tests for Phase 49.06 — m083 drops opus_score / eval_blocks / job_archetype."""

from __future__ import annotations

import sqlite3

from job_finder.web.migrations._runner import _apply_migration
from job_finder.web.migrations.m083_drop_dead_columns import MIGRATION as M083
from job_finder.web.migrations.types import MigrationContext

_DEAD = ("opus_score", "eval_blocks", "job_archetype")


def _pre_m083_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT,
                opus_score REAL,
                eval_blocks TEXT,
                job_archetype TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, opus_score, eval_blocks, job_archetype) "
            "VALUES ('k', 't', 1.0, '[]', 'platform_engineering')"
        )
        conn.commit()
    finally:
        conn.close()


def _apply(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        ctx = MigrationContext(conn=conn, db_path=path, user_data_root=".", initial_version=82)
        _apply_migration(ctx, M083)
    finally:
        conn.close()


def _columns(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}
    finally:
        conn.close()


def test_m083_drops_the_three_dead_columns(tmp_db_path):
    _pre_m083_db(tmp_db_path)
    assert _DEAD[0] in _columns(tmp_db_path)  # precondition
    _apply(tmp_db_path)
    cols = _columns(tmp_db_path)
    for dead in _DEAD:
        assert dead not in cols
    assert "dedup_key" in cols  # other columns survive


def test_m083_is_idempotent(tmp_db_path):
    _pre_m083_db(tmp_db_path)
    _apply(tmp_db_path)
    _apply(tmp_db_path)  # re-run after drop — runner swallows 'no such column'
    cols = _columns(tmp_db_path)
    for dead in _DEAD:
        assert dead not in cols


def test_full_migration_chain_has_no_dead_columns(tmp_db_path):
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    cols = _columns(tmp_db_path)
    for dead in _DEAD:
        assert dead not in cols
