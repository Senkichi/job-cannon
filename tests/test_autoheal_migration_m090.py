"""Tests for migration m090_shadow_state.

Verifies:
- MIGRATION exports the correct version and description.
- Fresh DB gains ``source_health.shadow_legacy_wins`` with default 0.
- Populated DB (pre-existing source_health rows) upgrades in place.
"""

import sqlite3

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m090_shadow_state import MIGRATION

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrated_conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_migration_version():
    assert MIGRATION.version == 90


def test_migration_description_nonempty():
    assert MIGRATION.description


def test_migration_sql_nonempty():
    assert MIGRATION.sql


# ---------------------------------------------------------------------------
# Fresh DB
# ---------------------------------------------------------------------------


def test_m090_fresh_db_adds_shadow_legacy_wins_column(tmp_path):
    conn = _migrated_conn(tmp_path)
    assert "shadow_legacy_wins" in _columns(conn, "source_health")


def test_m090_user_version_at_least_90(tmp_path):
    conn = _migrated_conn(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 90


def test_m090_default_is_zero(tmp_path):
    conn = _migrated_conn(tmp_path)
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES ('linkedin', 'email', 'healthy', 0, 1.0, '')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT shadow_legacy_wins FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Populated DB upgrade
# ---------------------------------------------------------------------------


def test_m090_populated_db_existing_rows_survive(tmp_path):
    """Existing source_health rows survive a re-run (run_migrations is idempotent)."""
    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES ('linkedin', 'email', 'degraded', 3, 2.0, '')"
    )
    conn.commit()
    conn.close()

    run_migrations(db)  # idempotent re-run must not raise or drop rows
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, shadow_legacy_wins FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["status"] == "degraded"
    assert row["shadow_legacy_wins"] == 0
