"""Helpers for tests that stage pre-m078-contract historical data.

Migration tests for migrations m056..m072 build a DB with ``run_migrations``
(which now goes to HEAD, including m078's contract-invariant triggers) and then
stage intentionally *dirty* historical rows — inverted salaries, NULL
scoring_provider, invalid workplace_type, junk/short jd_full — to verify that
the migration-under-test heals them. The m078 triggers would (correctly) reject
that dirty staging, but they are an anachronism for a test exercising a
pre-m078 migration. These helpers let such a test build the schema and then
remove the m078 contract enforcement so historical data can be staged.

``run_migrations_without_contract`` is a drop-in replacement for
``run_migrations`` — import it aliased so existing call sites need no change:

    from tests.helpers.contract_triggers import (
        run_migrations_without_contract as run_migrations,
    )
"""

from __future__ import annotations

import sqlite3

from job_finder.web.db_migrate import run_migrations as _run_migrations

_CONTRACT_INDEX = "ix_jobs_company_source_id"


def drop_contract_triggers(conn: sqlite3.Connection) -> None:
    """Drop every m078 contract trigger (and the I-11 unique index) on ``conn``.

    Idempotent. Discovers triggers by the ``tg_jobs_%`` naming convention so it
    stays correct if the invariant set changes.
    """
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'tg_jobs_%'"
        ).fetchall()
    ]
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP INDEX IF EXISTS {_CONTRACT_INDEX}")
    conn.commit()


def run_migrations_without_contract(db_path: str, user_data_root: str | None = None) -> None:
    """Run all migrations to HEAD, then drop the m078 contract enforcement.

    Drop-in replacement for ``job_finder.web.db_migrate.run_migrations`` for
    migration tests that stage pre-contract historical data.

    Migration tests for m056..m072 routinely rewind ``PRAGMA user_version`` and
    replay the chain over intentionally dirty rows (inverted salaries, junk
    jd_full, etc.). m078's preflight would (correctly, for production) HALT on
    that dirty data. Here we suppress the preflight for the duration of the run
    — the intervening healer migrations (m069/m071/m072) still run, and we drop
    the resulting triggers afterward so the test can keep staging historical
    rows. Production ``run_migrations`` keeps its preflight intact.
    """
    from job_finder.web.migrations import m078_contract_invariants as m078

    original = m078._assert_no_violators
    m078._assert_no_violators = lambda conn: None  # type: ignore[assignment]
    try:
        _run_migrations(db_path, user_data_root)
    finally:
        m078._assert_no_violators = original  # type: ignore[assignment]

    conn = sqlite3.connect(db_path)
    try:
        drop_contract_triggers(conn)
    finally:
        conn.close()
