"""Schema migration runner for job-finder SQLite database.

Uses PRAGMA user_version to track migration state. Safe to call on every
startup -- idempotent by design.

Migration definitions live in `job_finder.web.migrations`, one file per
version (`m{NNN:03d}_*.py`). Helpers (`_apply_migration`, the backup gate,
the post-migration dedup hook) live in private modules under that package.

This module is the public entry point: it loads the discovered MIGRATIONS,
applies pending migrations, and runs the post-migration retroactive-dedup
hook + the historical comp_data_json fixup. It also re-exports the helper
names that tests at `tests/test_migration.py` import directly (the back-
compat surface — see `__all__`).
"""

import os
import sqlite3

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.migrations import MIGRATIONS, Migration, MigrationContext
from job_finder.web.migrations._gate import MigrationBlockedError, _check_backup_recent
from job_finder.web.migrations._post_hooks import _run_retroactive_dedup_once
from job_finder.web.migrations._runner import _apply_migration

__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationBlockedError",
    "MigrationContext",
    "_apply_migration",
    "_check_backup_recent",
    "run_migrations",
]


def run_migrations(db_path: str, user_data_root: str | None = None) -> None:
    """Run pending migrations against the given SQLite database.

    Idempotent — safe to call on every application startup. Uses
    `PRAGMA user_version` to track which migrations have been applied.

    After Migration 6 completes (or if it was already applied), runs the
    retroactive deduplication merge once. A sentinel row in `merge_log`
    (`merge_source='migration_complete'`) tracks that this has run so that
    subsequent startups skip the one-time operation.

    Args:
        db_path: Path to the SQLite database file.
        user_data_root: Directory where user-data backups live. Defaults to
            CWD. Used by Migration 41's backup-recency gate.
    """
    root = user_data_root if user_data_root is not None else os.getcwd()
    with standalone_connection(db_path) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root, initial_version=current_version)
        for migration in MIGRATIONS:
            if migration.version <= current_version:
                continue
            _apply_migration(ctx, migration)

        # Run retroactive dedup once after Migration 6 or later is present.
        # Sentinel row prevents re-running on subsequent startups.
        final_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if final_version >= 6:
            _run_retroactive_dedup_once(conn)

        # Fixup: ensure comp_data_json column exists (missed in original Migration 7).
        # Required for databases that ran Migration 7 before this column was added —
        # those DBs already have user_version=7 so the migration loop won't re-apply.
        if final_version >= 7:
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN comp_data_json TEXT DEFAULT NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — expected on fresh DBs
