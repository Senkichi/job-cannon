"""Tests for migration m091_careers_rekey.

Verifies:
- MIGRATION exports the correct version and description.
- Stale global 'careers' rows are deleted from corpus_sample + source_health.
- Per-company 'careers:*' rows are untouched.
"""

import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m091_careers_rekey import MIGRATION

# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_migration_version():
    assert MIGRATION.version == 91


def test_migration_description_nonempty():
    assert MIGRATION.description


# ---------------------------------------------------------------------------
# Behavior — replay the migration SQL against a populated schema
# ---------------------------------------------------------------------------


def _migrated_conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(conn, source: str) -> None:
    conn.execute(
        "INSERT INTO corpus_sample (source, surface, raw_text, output_json, captured_at) "
        "VALUES (?, 'careers', 'html', '{}', '')",
        (source,),
    )
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES (?, 'careers', 'healthy', 0, 1.0, '')",
        (source,),
    )
    conn.commit()


def test_m091_deletes_global_careers_rows_keeps_per_company(tmp_path):
    conn = _migrated_conn(tmp_path)
    _seed(conn, "careers")
    _seed(conn, "careers:acme.com")

    # Replay the migration SQL (a fresh DB has already applied m091 with no
    # rows present; the deletes are idempotent by construction).
    for stmt in MIGRATION.sql:
        conn.execute(stmt)
    conn.commit()

    remaining_corpus = {r[0] for r in conn.execute("SELECT source FROM corpus_sample").fetchall()}
    remaining_health = {r[0] for r in conn.execute("SELECT source FROM source_health").fetchall()}
    assert "careers" not in remaining_corpus
    assert "careers" not in remaining_health
    assert "careers:acme.com" in remaining_corpus
    assert "careers:acme.com" in remaining_health


def test_m091_user_version_at_least_91(tmp_path):
    conn = _migrated_conn(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 91
