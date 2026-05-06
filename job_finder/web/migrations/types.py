"""Migration value types.

The MIGRATIONS list (currently still in `db_migrate.py`, will move to
per-version files in S6.3) is a list of `Migration` objects. Each migration
is either:

  - a sequence of SQL statement strings (`sql=[...]`), or
  - a Python callable that takes a `MigrationContext` (`py=...`).

Migrations may use both — `sql` runs first, then `py` — but in practice each
migration uses only one of the two. The `py` form is reserved for migrations
that need filesystem or environment state beyond a raw SQL connection
(currently only Migration 41's backup-recency gate).

`MigrationContext` carries everything a `py`-helper might need so the helper
remains pure and testable without reaching into module globals.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MigrationContext:
    """State passed to migration `py`-helpers by `run_migrations`.

    Attributes:
        conn: An open SQLite connection at the migration boundary. The helper
            is responsible for `execute`-ing its DDL/DML; `_apply_migration`
            commits and bumps `PRAGMA user_version` after the helper returns.
        db_path: Absolute path to the SQLite DB file the migration is running
            against. Useful for migrations that need to fork a side-channel
            connection or compute paths relative to the DB.
        user_data_root: Directory where user-data backups live. Defaults to
            CWD in `run_migrations`. Migration 41 uses this to glob for
            `backup_userdata_*.tar.gz` to enforce the backup-recency gate.
    """

    conn: sqlite3.Connection
    db_path: str
    user_data_root: str


@dataclass(frozen=True)
class Migration:
    """A single schema migration.

    Attributes:
        version: 1-based migration number. PRAGMA user_version is set to this
            after the migration commits successfully. **Never renumber a
            shipped migration** — it is load-bearing for every existing
            user's database (MI-4).
        description: One-line summary suitable for log output.
        sql: Discrete SQL statements run in order. Each statement is executed
            in its own `conn.execute` call so per-statement idempotency
            (`duplicate column name`, `no such column`) can be swallowed
            without aborting the migration.
        py: Optional Python helper invoked with a `MigrationContext` after
            `sql` runs. Used by migrations that need filesystem or env state
            (Migration 41's backup-recency gate).
    """

    version: int
    description: str
    sql: list[str] = field(default_factory=list)
    py: Callable[[MigrationContext], None] | None = None
