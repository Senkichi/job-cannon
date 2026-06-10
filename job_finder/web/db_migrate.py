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

Safety story
------------
* Downgrade guard: if ``PRAGMA user_version`` is *ahead* of the highest
  migration known to this code version, we raise ``DatabaseNewerThanCodeError``
  immediately, leaving the DB untouched.  The user must upgrade to a matching
  version of Job Cannon before proceeding.

* Backup-before-migrate: when any migration is pending on a **non-empty** DB
  (i.e. at least one row in ``jobs``), a timestamped SQLite backup is written
  to ``<user_data_root>/backups/`` before any migration runs.  This uses the
  sqlite3 ``.backup()`` API for a hot, consistent snapshot.  Simple retention
  keeps the last ``_BACKUP_RETENTION`` copies, deleting the rest.

  Fresh/empty DBs (zero rows in ``jobs``, or DB file does not yet exist) skip
  the backup — there is nothing to lose.
"""

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.migrations import MIGRATIONS, Migration, MigrationContext
from job_finder.web.migrations._gate import MigrationBlockedError, _check_backup_recent
from job_finder.web.migrations._post_hooks import _run_retroactive_dedup_once
from job_finder.web.migrations._runner import _apply_migration

__all__ = [
    "MIGRATIONS",
    "DatabaseNewerThanCodeError",
    "Migration",
    "MigrationBlockedError",
    "MigrationContext",
    "_apply_migration",
    "_check_backup_recent",
    "run_migrations",
]

logger = logging.getLogger(__name__)

# Number of automatic pre-migration backups to keep per database.
_BACKUP_RETENTION = 5

# Max known migration version derived from the discovered MIGRATIONS list.
# This is the single source of truth — no hardcoded constant to drift.
MAX_KNOWN_VERSION: int = max(m.version for m in MIGRATIONS) if MIGRATIONS else 0


class DatabaseNewerThanCodeError(Exception):
    """Raised when the database's user_version exceeds the highest known migration.

    This indicates the database was created (or migrated) by a *newer* version
    of Job Cannon than what is currently installed.  Running the old code against
    the newer schema risks data corruption or silent failures.

    Do NOT attempt to migrate or modify the database.  The error message
    contains the actionable remediation step.
    """


def _backup_db(db_path: str, backups_dir: Path) -> Path | None:
    """Take a timestamped SQLite backup of ``db_path`` into ``backups_dir``.

    Uses sqlite3's ``.backup()`` API for a hot, consistent snapshot.  Applies
    simple retention: after writing the new backup, deletes all but the newest
    ``_BACKUP_RETENTION`` copies.

    Args:
        db_path:     Path to the source SQLite database.
        backups_dir: Directory to write backups into (created if absent).

    Returns:
        Path to the backup file just written, or None if the DB does not exist.
    """
    src = Path(db_path)
    if not src.exists():
        return None

    backups_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"jobs_before_migrate_{ts}.db"

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Retention: keep only the newest _BACKUP_RETENTION backups.
    existing = sorted(backups_dir.glob("jobs_before_migrate_*.db"))
    for old in existing[: max(0, len(existing) - _BACKUP_RETENTION)]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not remove old backup %s", old)

    logger.info("DB backup written to %s", dest)
    return dest


def _db_is_non_empty(db_path: str) -> bool:
    """Return True if ``db_path`` exists and has at least one row in ``jobs``.

    A fresh database (file doesn't exist, or jobs table absent, or zero rows)
    returns False — no data to lose, backup not needed.
    """
    if not Path(db_path).exists():
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()
            return row is not None
        except sqlite3.OperationalError:
            # jobs table doesn't exist yet (brand-new DB pre-migration)
            return False
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def run_migrations(db_path: str, user_data_root: str | None = None) -> None:
    """Run pending migrations against the given SQLite database.

    Idempotent — safe to call on every application startup. Uses
    ``PRAGMA user_version`` to track which migrations have been applied.

    Raises:
        DatabaseNewerThanCodeError: When the database's ``user_version`` exceeds
            the highest migration known to this code version.  The DB is NOT
            modified.  The caller should surface the message to the user and
            exit cleanly.
        MigrationBlockedError: Raised by Migration 41's preflight gate when no
            recent backup exists (and ``GSD_BACKUP_CONFIRMED=1`` is not set).

    After Migration 6 completes (or if it was already applied), runs the
    retroactive deduplication merge once. A sentinel row in ``merge_log``
    (``merge_source='migration_complete'``) tracks that this has run so that
    subsequent startups skip the one-time operation.

    Args:
        db_path: Path to the SQLite database file.
        user_data_root: Directory where user-data backups live. Defaults to
            CWD. Used by Migration 41's backup-recency gate and automatic
            pre-migration backups (written to ``<root>/backups/``).
    """
    root = user_data_root if user_data_root is not None else os.getcwd()
    with standalone_connection(db_path) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # --- Downgrade guard ---
        # If the DB was written by a newer Job Cannon, we must not touch it.
        if current_version > MAX_KNOWN_VERSION:
            raise DatabaseNewerThanCodeError(
                f"This database was created by a newer version of Job Cannon "
                f"(schema version {current_version}, but this installation only "
                f"knows migrations up to {MAX_KNOWN_VERSION}).\n\n"
                f"To fix this, upgrade Job Cannon:\n"
                f"  pipx upgrade job-cannon\n\n"
                f"Or restore a backup from before the upgrade.\n"
                f"The database has NOT been modified."
            )

        # --- Backup-before-migrate (non-empty DBs only) ---
        pending = [m for m in MIGRATIONS if m.version > current_version]
        if pending and _db_is_non_empty(db_path):
            backups_dir = Path(root) / "backups"
            _backup_db(db_path, backups_dir)

        ctx = MigrationContext(
            conn=conn, db_path=db_path, user_data_root=root, initial_version=current_version
        )
        for migration in pending:
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
