"""Migration applier — single-migration execution + PRAGMA user_version bump.

Decoupled from `run_migrations` (in `db_migrate.py`) because:
  1. `tests/test_migration.py` imports `_apply_migration` directly, and the
     stable surface is the back-compat re-export in `db_migrate.py`.
  2. The applier is the natural unit of test isolation — a single Migration
     applied to a single connection, returning nothing.

The two `OperationalError` substrings (`duplicate column name`, `no such
column`) cover the idempotency contract for additive and destructive
migrations respectively. Any other OperationalError aborts the migration
loop with the original traceback intact.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations._ledger import max_applied, record_applied
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _apply_migration(ctx: MigrationContext, migration: Migration) -> None:
    """Apply a single migration and update PRAGMA user_version.

    Order: SQL statements first (in declared order), then the optional `py`
    helper. In practice no migration uses both — `py` is reserved for
    migrations that need filesystem or env state (Migration 41), and those
    perform their own DDL inside the helper.

    Per-statement idempotency:

    - `duplicate column name` errors from `ALTER TABLE ADD COLUMN` are caught
      and skipped, enabling re-runs of additive migrations on a populated
      schema.
    - `no such column` errors from `ALTER TABLE DROP COLUMN` are caught and
      skipped, enabling re-runs of destructive migrations after the column
      has already been removed.

    Any other `OperationalError` propagates and aborts the migration loop.
    """
    for stmt in migration.sql:
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            ctx.conn.execute(stmt)
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "duplicate column name" in error_msg:
                continue
            if "no such column" in error_msg:
                continue
            raise

    if migration.py is not None:
        try:
            migration.py(ctx)
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "no such column" not in error_msg:
                raise

    if not isinstance(migration.version, int):
        raise TypeError(f"Migration version must be int, got {type(migration.version)}")

    # Record the migration in the ledger (the authoritative applied-set) and
    # refresh the PRAGMA user_version cache (best-effort, = ledger MAX) in the
    # SAME transaction as the DDL/DML above. Collapsing the prior two-commit
    # pattern into one commit closes the crash-window where the schema change
    # committed but the applied-record did not. record_applied self-bootstraps
    # the ledger, so direct callers on a raw connection work without a prior
    # run_migrations/ensure_ledger.
    record_applied(ctx.conn, migration)
    ctx.conn.execute(f"PRAGMA user_version = {max_applied(ctx.conn)}")
    ctx.conn.commit()
    logger.info("Migration %d applied successfully.", migration.version)
