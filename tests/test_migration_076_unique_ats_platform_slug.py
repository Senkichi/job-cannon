"""Tests for Migration 76 — enforce UNIQUE(ats_platform, ats_slug).

Covers:
- ``MIGRATION.version`` is 76.
- After ``run_migrations``, ``PRAGMA user_version`` is at least 76 and the
  partial unique index ``idx_companies_ats_pair`` exists in sqlite_master.
- An INSERT that would create a duplicate non-null ``(ats_platform,
  ats_slug)`` pair raises ``sqlite3.IntegrityError``.
- An UPDATE that would create a duplicate non-null pair raises
  ``sqlite3.IntegrityError``.
- Multiple companies with NULL ``ats_platform`` / NULL ``ats_slug`` are
  allowed (partial-index semantics).
- The migration's pre-flight gate raises ``RuntimeError`` (with cluster
  details) when applied against a DB whose companies table still holds an
  unhealed cluster.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m076_unique_ats_platform_slug import (
    MIGRATION,
    _assert_no_unhealed_dupes,
)
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def _insert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
) -> int:
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_slug,
               ats_probe_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        (name, name, ats_platform, ats_slug, now, now),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def test_migration_declares_version_76():
    assert MIGRATION.version == 76


def test_pragma_user_version_at_least_76_after_migrate(migrated_db_path):
    conn = sqlite3.connect(migrated_db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version >= 76, f"Expected user_version>=76 after run_migrations, got {version}"


def test_unique_partial_index_exists_after_migrate(migrated_db_path):
    conn = sqlite3.connect(migrated_db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_companies_ats_pair'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "idx_companies_ats_pair should be created by Migration 76"


def test_duplicate_non_null_insert_raises_integrity_error(migrated_db_path):
    conn = sqlite3.connect(migrated_db_path)
    try:
        _insert_company(conn, "Acme Corp", ats_platform="greenhouse", ats_slug="acme")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_company(
                conn,
                "Acme Aggregator",
                ats_platform="greenhouse",
                ats_slug="acme",
            )
    finally:
        conn.close()


def test_multiple_null_pairs_allowed(migrated_db_path):
    """Companies without a detected ATS legitimately share (NULL, NULL)."""
    conn = sqlite3.connect(migrated_db_path)
    try:
        _insert_company(conn, "Pending One")
        _insert_company(conn, "Pending Two")
        _insert_company(conn, "Pending Three")
        count = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE ats_platform IS NULL"
        ).fetchone()[0]
        assert count == 3
    finally:
        conn.close()


def test_update_to_clashing_pair_raises_integrity_error(migrated_db_path):
    conn = sqlite3.connect(migrated_db_path)
    try:
        _insert_company(conn, "Acme Corp", ats_platform="greenhouse", ats_slug="acme")
        loser_id = _insert_company(
            conn,
            "Acme Aggregator",
            ats_platform="lever",
            ats_slug="acme-agg",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE companies SET ats_platform='greenhouse', ats_slug='acme' WHERE id = ?",
                (loser_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_apply_fails_loudly_on_unhealed_dupes(tmp_path):
    """Pre-flight gate surfaces unhealed clusters with their members.

    Build a DB through Migration 75, manually seed a duplicate pair, then
    invoke the migration helper directly. The RuntimeError message must
    include both the platform/slug and at least one row id so an operator
    can re-run m068 or fix the data manually.
    """
    from job_finder.web import migrations as mig_pkg
    from job_finder.web.db_migrate import _apply_migration

    fd, path = tempfile.mkstemp(suffix=".db", dir=str(tmp_path))
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ctx = MigrationContext(conn=conn, db_path=path, user_data_root=str(tmp_path))
    try:
        # Apply every migration up to (but not including) 76.
        for m in mig_pkg.MIGRATIONS:
            if m.version >= 76:
                break
            _apply_migration(ctx, m)

        # Seed an unhealed cluster — two companies sharing (platform, slug).
        _insert_company(conn, "Real Corp", ats_platform="greenhouse", ats_slug="dup")
        _insert_company(conn, "Aggregator Inc", ats_platform="greenhouse", ats_slug="dup")

        mig76 = next(m for m in mig_pkg.MIGRATIONS if m.version == 76)
        with pytest.raises(RuntimeError) as excinfo:
            _apply_migration(ctx, mig76)
        msg = str(excinfo.value)
        assert "m076" in msg
        assert "greenhouse" in msg
        assert "dup" in msg
        # At least one member id should be surfaced (id=N pattern).
        assert "id=" in msg

        # Also exercise the helper directly to assert it's the source of the
        # RuntimeError (not _apply_migration wrapping something else).
        with pytest.raises(RuntimeError):
            _assert_no_unhealed_dupes(ctx)
    finally:
        conn.close()
        if os.path.exists(path):
            os.remove(path)
