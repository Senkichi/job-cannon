"""Migration backup-recency gate.

Migration 41 is destructive — it drops three columns from `jobs`. Before it
runs, this gate confirms that a recent userdata backup exists OR that the
operator has explicitly opted out via `GSD_BACKUP_CONFIRMED=1`. The fail-closed
default protects single-user setups where the rollback path is "restore the
DB from a backup".

Re-exported from `job_finder.web.db_migrate` for back-compat with tests at
`tests/test_migration.py:1182-1240`. New code should import directly from
`job_finder.web.migrations._gate`.
"""

from __future__ import annotations

import glob
import os
import time


class MigrationBlockedError(Exception):
    """Raised by a migration's preflight gate to block destructive schema changes.

    Currently raised by Migration 41 when the backup-recency check fails
    (no recent backup tarball AND GSD_BACKUP_CONFIRMED=1 not set). Callers
    should present the message to the operator and halt; the migration
    will not have mutated any schema or data before the raise.
    """


def _check_backup_recent(
    user_data_root: str | None = None,
    initial_version: int = 0,
) -> None:
    """Preflight gate for Migration 41: require a recent backup OR explicit override.

    Looks for backup_userdata_*.tar.gz files under `user_data_root` (defaults
    to CWD when None). Raises MigrationBlockedError when:
      - No matching backup is found, AND GSD_BACKUP_CONFIRMED != "1"
      - The newest backup is older than 24h, AND GSD_BACKUP_CONFIRMED != "1"

    The env var override exists so operators who use alternate backup schemes
    (time-machine snapshots, zfs datasets, manual .backup copies) can proceed
    after accepting responsibility for the rollback path. Fail-closed default.

    Fresh install bypass: ``initial_version == 0`` means the DB was brand-new
    when this migration run started (no data to lose), so the backup gate is
    skipped.  Checking the DB file's existence is not reliable here because
    migrations 1-40 have already created the file by the time migration 41 runs.
    """
    if os.environ.get("GSD_BACKUP_CONFIRMED") == "1":
        return
    if initial_version == 0:
        return
    root = user_data_root if user_data_root is not None else os.getcwd()

    pattern = os.path.join(root, "backup_userdata_*.tar.gz")
    backups = sorted(glob.glob(pattern), reverse=True)
    if not backups:
        raise MigrationBlockedError(
            "Migration 41 blocked: no backup_userdata_*.tar.gz found in cwd. "
            "Run `bash backup_userdata.sh` first, or set GSD_BACKUP_CONFIRMED=1 "
            "to override (only if you have an alternate backup)."
        )
    age_h = (time.time() - os.path.getmtime(backups[0])) / 3600.0
    if age_h > 24.0:
        raise MigrationBlockedError(
            f"Migration 41 blocked: most recent backup ({backups[0]}) is "
            f"{age_h:.1f}h old (>24h). Run `bash backup_userdata.sh`, or set "
            f"GSD_BACKUP_CONFIRMED=1 to override."
        )
