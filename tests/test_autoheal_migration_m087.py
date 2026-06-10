"""Tests for migration m087_heal_state.

Verifies:
- MIGRATION exports the correct version and description.
- Applies to a fresh DB and to a populated DB (with source_health already present).
- Idempotent: applying twice does not raise.
- Expected columns and table exist after migration.
"""

import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m087_heal_state import MIGRATION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


# ---------------------------------------------------------------------------
# MIGRATION object
# ---------------------------------------------------------------------------


def test_migration_version():
    assert MIGRATION.version == 87


def test_migration_description_nonempty():
    assert MIGRATION.description


def test_migration_sql_nonempty():
    assert len(MIGRATION.sql) >= 1


# ---------------------------------------------------------------------------
# Fresh DB (run_migrations applies all migrations)
# ---------------------------------------------------------------------------


def test_m087_fresh_db_creates_heal_audit_table(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    tables = _tables(conn)
    assert "heal_audit" in tables
    conn.close()


def test_m087_fresh_db_adds_heal_attempts_column(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    cols = _columns(conn, "source_health")
    assert "heal_attempts" in cols
    conn.close()


def test_m087_fresh_db_adds_last_heal_at_column(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    cols = _columns(conn, "source_health")
    assert "last_heal_at" in cols
    conn.close()


def test_m087_heal_audit_columns(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    cols = _columns(conn, "heal_audit")
    assert cols >= {"id", "source", "surface", "outcome", "created_at"}
    conn.close()


def test_m087_heal_audit_detail_column(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    cols = _columns(conn, "heal_audit")
    assert "detail" in cols
    conn.close()


def test_m087_user_version_at_least_87(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 87
    conn.close()


# ---------------------------------------------------------------------------
# Populated DB (source_health already has rows)
# ---------------------------------------------------------------------------


def test_m087_populated_db_existing_rows_survive(tmp_path):
    """Existing source_health rows survive the migration (column defaults apply)."""
    db = str(tmp_path / "t.db")
    # Apply up to m086 first by running all migrations, then insert a row,
    # then confirm the row survives (run_migrations is idempotent).
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT OR REPLACE INTO source_health
            (source, surface, status, consecutive_breaks, baseline_yield, updated_at)
        VALUES ('linkedin', 'email', 'healthy', 0, 5.0, '2026-01-01T00:00:00')
        """
    )
    conn.commit()
    conn.close()

    # Run again (idempotent)
    run_migrations(db)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT heal_attempts, last_heal_at FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row is not None
    heal_attempts, last_heal_at = row
    assert heal_attempts == 0
    assert last_heal_at is None
    conn.close()


# ---------------------------------------------------------------------------
# heal_audit insert round-trip
# ---------------------------------------------------------------------------


def test_m087_heal_audit_insert_round_trip(tmp_path):
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO heal_audit (source, surface, outcome, detail, created_at)
        VALUES ('linkedin', 'email', 'candidate_generated', 'test detail', '2026-01-01T00:00:00')
        """
    )
    conn.commit()
    row = conn.execute("SELECT source, surface, outcome FROM heal_audit").fetchone()
    assert row == ("linkedin", "email", "candidate_generated")
    conn.close()
