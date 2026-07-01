"""Tests for the set-membership migration ledger (Design A).

These lock in the property the old scalar ``PRAGMA user_version`` scheme lacked:
"applied" is set membership in ``schema_migrations``, so a migration merged in
*below* the current max still RUNS instead of being silently skipped. Also covers
the one-time legacy backfill, the duplicate-version guard, and the ledger-based
downgrade (orphan) guard.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from job_finder.web.db_migrate import MAX_KNOWN_VERSION, DatabaseNewerThanCodeError, run_migrations
from job_finder.web.migrations import MIGRATIONS, _verify_unique_versions
from job_finder.web.migrations._ledger import (
    applied_versions,
    backfill_from_user_version,
    ensure_ledger,
    has_run,
    max_applied,
)
from job_finder.web.migrations.types import Migration

_KNOWN_VERSIONS = {m.version for m in MIGRATIONS}


def _uv(db_path: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_records_full_ledger(tmp_path):
    """A fresh migrate records every discovered migration in the ledger and the
    user_version cache equals the highest applied version."""
    db = str(tmp_path / "jobs.db")
    run_migrations(db)
    with closing(sqlite3.connect(db)) as conn:
        assert applied_versions(conn) == _KNOWN_VERSIONS
        assert max_applied(conn) == MAX_KNOWN_VERSION
    assert _uv(db) == MAX_KNOWN_VERSION  # cache tracks ledger MAX


def test_out_of_order_migration_still_applies(tmp_path):
    """THE regression: a below-max migration absent from the ledger is re-applied.

    Under the old scheme, once user_version passed N, a migration at version N
    could never run again (N > current is false) — the silent-skip bug. With the
    ledger, membership drives application, so forgetting a mid-chain version makes
    it pending again and it runs.
    """
    db = str(tmp_path / "jobs.db")
    run_migrations(db)
    # Forget a mid-chain migration (simulates one that merged in below the max).
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 50")
        conn.commit()
        assert not has_run(conn, 50)
    run_migrations(db)
    with closing(sqlite3.connect(db)) as conn:
        assert has_run(conn, 50), "a below-max migration absent from the ledger must re-apply"
        assert applied_versions(conn) == _KNOWN_VERSIONS


def test_backfill_marks_applied_without_executing(tmp_path):
    """backfill seeds the ledger from user_version, honoring real gaps, and never
    marks a version that has no migration file (no phantom 97/98/99)."""
    db = str(tmp_path / "jobs.db")
    run_migrations(db)  # get the real schema in place
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TABLE schema_migrations")  # simulate a legacy DB
        conn.commit()
        ensure_ledger(conn)
        backfill_from_user_version(conn, MIGRATIONS, 106)
        conn.commit()
        seeded = applied_versions(conn)
    expected = {v for v in _KNOWN_VERSIONS if v <= 106}
    assert seeded == expected
    assert {97, 98, 99} & seeded == set(), "gap versions must never be marked applied"
    assert max(seeded) <= 106


def test_legacy_db_backfills_then_applies_pending(tmp_path):
    """A legacy DB (no ledger) at user_version=113 backfills 1..113 then applies
    the genuinely-pending 114..max — no re-run of already-applied migrations."""
    db = str(tmp_path / "jobs.db")
    run_migrations(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TABLE schema_migrations")
        conn.execute("PRAGMA user_version = 113")
        conn.commit()
    run_migrations(db)  # first run under the new scheme
    with closing(sqlite3.connect(db)) as conn:
        applied = applied_versions(conn)
    assert applied == _KNOWN_VERSIONS
    assert all(v in applied for v in range(114, MAX_KNOWN_VERSION + 1))
    assert _uv(db) == MAX_KNOWN_VERSION


def test_duplicate_version_guard_raises():
    """The discovery completeness guard rejects two migrations sharing a version."""
    dupes = [
        Migration(version=1, description="a", sql=["SELECT 1"]),
        Migration(version=1, description="b", sql=["SELECT 1"]),
    ]
    with pytest.raises(ValueError, match="Duplicate migration version 1"):
        _verify_unique_versions(dupes)
    # A unique list passes.
    _verify_unique_versions(
        [Migration(version=1, description="a"), Migration(version=2, description="b")]
    )


def test_downgrade_orphan_guard_raises(tmp_path):
    """A ledger recording a migration this code does not know → DatabaseNewerThanCodeError.

    This is the ledger-based half of the downgrade guard: it fires even when the
    user_version cache is stale/low, and names the unknown migration.
    """
    db = str(tmp_path / "jobs.db")
    run_migrations(db)
    # Record a future migration this code doesn't ship (cache left at 117 so the
    # scalar fast-path does NOT fire — we're exercising the orphan check).
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (999999, 'm999999_from_the_future', 'x', '2099-01-01T00:00:00')"
        )
        conn.commit()
    with pytest.raises(DatabaseNewerThanCodeError, match="999999"):
        run_migrations(db)


def test_scalar_downgrade_guard_still_fires(tmp_path):
    """The retained scalar fast-path still refuses a DB whose cache is ahead of
    the highest known migration (belt-and-suspenders with the orphan check)."""
    db = str(tmp_path / "jobs.db")
    run_migrations(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"PRAGMA user_version = {MAX_KNOWN_VERSION + 1}")
        conn.commit()
    with pytest.raises(DatabaseNewerThanCodeError):
        run_migrations(db)


def _load_generator():
    path = Path(__file__).resolve().parent.parent / "scripts" / "new_migration.py"
    spec = importlib.util.spec_from_file_location("new_migration", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_mints_version_above_legacy_and_avoids_collisions():
    """The authoring generator mints a monotonic epoch-second stamp above the
    legacy range and bumps past a same-second collision (32-bit-safe)."""
    gen = _load_generator()
    v = gen._mint_version(set())
    assert v > MAX_KNOWN_VERSION
    assert v < 2**31, "must fit SQLite's 32-bit user_version cache"
    # Collision-avoidance: if the stamp is already taken, mint the next free int.
    assert gen._mint_version({v}) == v + 1
    assert gen._slugify("Add Widget Column!") == "add_widget_column"
