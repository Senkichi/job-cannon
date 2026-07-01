"""Applied-migration ledger — the authoritative set of applied migrations.

This replaces the single-scalar ``PRAGMA user_version`` high-water mark as the
source of truth for *"has migration N been applied?"*. The old encoding —
``pending = [m for m in MIGRATIONS if m.version > current_version]`` — forced a
globally-coordinated total order: every author had to know the current top
number to pick the next one, so two parallel branches would both pick "the
next" number, collide at merge, and the loser (the one sorting at/below the
post-merge ``user_version``) would be **silently skipped forever**.

The ``schema_migrations`` table records each applied migration as a row. "Applied"
becomes set membership (``m.version not in applied_versions(conn)``), so a
migration merged in *below* the current max is absent from the ledger and simply
**runs** — the silent-skip class is gone. ``PRAGMA user_version`` is retained by
the runner only as a redundant, best-effort cache (kept equal to the ledger's
``MAX(version)``) for external inspectors; it is no longer authoritative.

This is the Rails/Django/Flyway/Liquibase convergence point: track a *set* of
applied migrations, not a scalar. Design A in ``docs/architecture/migrations.md``.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from collections.abc import Iterable

from job_finder.json_utils import utc_now_iso
from job_finder.web.migrations.types import Migration

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,   -- migration int identity (unchanged meaning)
    name       TEXT NOT NULL,         -- module basename, for forensics
    checksum   TEXT,                  -- OS-stable hash of normalized sql+py source
    applied_at TEXT NOT NULL          -- naive UTC ISO (store-UTC discipline)
)
"""

_INSERT = (
    "INSERT OR REPLACE INTO schema_migrations (version, name, checksum, applied_at) "
    "VALUES (?, ?, ?, ?)"
)


def ensure_ledger(conn: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` table if absent. Idempotent, cheap.

    Called as the first line of every write path (``record_applied``,
    ``backfill_from_user_version``) so the ledger self-bootstraps even when a
    caller builds a DB via direct ``_apply_migration`` on a raw connection
    without going through ``run_migrations`` (the test suite does this at ~33
    call sites).
    """
    conn.execute(_LEDGER_DDL)


def ledger_exists(conn: sqlite3.Connection) -> bool:
    """Return True if the ``schema_migrations`` table exists (read-only check)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions recorded as applied.

    Empty set when the ledger table does not exist yet (a legacy or fresh DB
    that has not been backfilled). This is the authoritative "applied" set.
    """
    if not ledger_exists(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def has_run(conn: sqlite3.Connection, version: int) -> bool:
    """Return True if migration ``version`` specifically has been applied.

    Replaces the ``final_version >= N`` feature-gate reads in ``run_migrations``.
    Strictly more correct than the ``>=`` comparison: membership answers "did
    migration N run?" and survives out-of-order application, which the scalar
    comparison does not.
    """
    if not ledger_exists(conn):
        return False
    return (
        conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone()
        is not None
    )


def max_applied(conn: sqlite3.Connection) -> int:
    """Return the highest applied version, or 0 if none. Used for the cache."""
    if not ledger_exists(conn):
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def _checksum(migration: Migration) -> str:
    """OS-stable sha256 of the migration's SQL + py source.

    Line endings are normalized to ``\\n`` so a Windows/mac/Linux checkout of the
    same shipped migration produces an identical hash. Stored for forensics and
    future never-edit-a-shipped-migration enforcement; not enforced today.
    """
    parts = ["\n".join(migration.sql)]
    if migration.py is not None:
        try:
            parts.append(inspect.getsource(migration.py))
        except (OSError, TypeError):
            parts.append(repr(migration.py))
    normalized = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _ledger_name(migration: Migration) -> str:
    """Module basename for the ledger, falling back to ``m{version}``."""
    return migration.name or f"m{migration.version}"


def record_applied(conn: sqlite3.Connection, migration: Migration) -> None:
    """Record ``migration`` as applied (INSERT OR REPLACE — idempotent).

    ``INSERT OR REPLACE`` (not a plain ``INSERT``) mirrors the old idempotent
    ``PRAGMA user_version = N`` overwrite: re-applying the same version (the
    rewind-and-reapply test pattern) overwrites the row instead of raising an
    ``IntegrityError`` on the ``version`` primary key.

    Self-bootstraps the ledger so direct ``_apply_migration`` callers work.
    """
    ensure_ledger(conn)
    conn.execute(
        _INSERT,
        (migration.version, _ledger_name(migration), _checksum(migration), utc_now_iso()),
    )


def backfill_from_user_version(
    conn: sqlite3.Connection, migrations: Iterable[Migration], current_version: int
) -> None:
    """Seed the ledger for a legacy DB migrated under the old scalar scheme.

    Marks every *discovered* migration with ``version <= current_version`` as
    applied WITHOUT executing it (Flyway ``baseline`` / Alembic ``stamp``): the
    schema is already there, so no DDL re-runs and no ``py``-helper (e.g. m041's
    destructive DROP COLUMN, the m100/m110/m111 watermark seeders) double-fires.

    Iterating the *discovered set* — not ``range(1, current_version + 1)`` — is
    load-bearing: the real chain has a genuine gap (m096 -> m100, no m097/098/099).
    A range-based backfill would insert phantom rows 97/98/99, and a future
    migration numbered 98 would then be membership-skipped — reintroducing the
    exact bug. One-time and idempotent (INSERT OR REPLACE).
    """
    ensure_ledger(conn)
    now = utc_now_iso()
    rows = [
        (m.version, _ledger_name(m), _checksum(m), now)
        for m in migrations
        if m.version <= current_version
    ]
    conn.executemany(_INSERT, rows)
