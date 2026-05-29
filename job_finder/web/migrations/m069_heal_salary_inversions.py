"""Migration 69 — heal rows where salary_min > salary_max.

Background: parsers from several sources (DataForSEO numeric extracts,
Workday salary blobs, a Glassdoor unit-confusion bug) wrote rows where
``salary_min`` exceeds ``salary_max``. By the audit on 2026-05-28, 9
rows had this state. The persistence boundary now enforces the
invariant via ``_normalize_salary`` in db._jobs, so new rows can't
land in this state — m069 retroactively applies the same rule to
existing rows.

Policy mirrors ``_normalize_salary``:

  - Both NULL or only one set → no action.
  - min <= max → no action (idempotent).
  - min > max and ``max/min`` after swap is <= 10 → swap them (same
    unit, parser just emitted them reversed).
  - Otherwise → null both. The salary blob can't be trusted to share
    a unit; m062 will re-attempt extraction from jd_full on a future
    run if a clean range becomes recoverable.

Re-running is safe: after this migration, no rows match
``salary_min > salary_max`` unless a new buggy write slips past the
boundary guard.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m069: jobs table not present, no-op")
        return

    inverted = conn.execute(
        """SELECT dedup_key, salary_min, salary_max
             FROM jobs
            WHERE salary_min IS NOT NULL
              AND salary_max IS NOT NULL
              AND salary_min > salary_max"""
    ).fetchall()

    swapped = 0
    nulled = 0
    for dedup_key, smin, smax in inverted:
        lo, hi = smax, smin
        if lo <= 0 or hi / lo > 10:
            conn.execute(
                "UPDATE jobs SET salary_min = NULL, salary_max = NULL WHERE dedup_key = ?",
                (dedup_key,),
            )
            nulled += 1
        else:
            conn.execute(
                "UPDATE jobs SET salary_min = ?, salary_max = ? WHERE dedup_key = ?",
                (lo, hi, dedup_key),
            )
            swapped += 1

    logger.info(
        "m069: healed %d inverted-salary row(s) — %d swapped, %d nulled",
        len(inverted),
        swapped,
        nulled,
    )


MIGRATION = Migration(
    version=69,
    description="heal rows where salary_min > salary_max (swap or null)",
    py=_heal,
)
