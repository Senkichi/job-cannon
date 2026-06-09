"""Migration 86 — null out implausibly-inflated salaries.

Background: the post-fetch quick-tier LLM extractor
(``parse_structured_fields`` in ``enrichment_tiers``) historically returned
salary integers verbatim, while the parallel regex path enforced a
``[$30K, $5M]`` plausibility bound. The LLM occasionally emitted ~100×-
inflated values (e.g. ``salary_min=27_500_000`` on a $275K role) that the
F2 ``_reconcile_salary_for_write`` net only caught when the pair was
inverted. Both-fields-inflated ordered pairs and single-inflated
``salary_min`` with NULL ``salary_max`` persisted verbatim and corrupted
the scorer's ``salary_range`` signal.

The LLM path now mirrors the regex bound at the parse boundary, so new
writes can't land in this state. m086 retroactively applies the same
policy to existing rows.

Policy mirrors the now-shared bound in ``salary_extractor``:

  - ``salary_min > 5_000_000`` OR ``salary_max > 5_000_000`` → null BOTH
    fields. The salary blob can't be trusted; m062 will re-attempt
    extraction from jd_full on a future run if a clean value is
    recoverable.
  - Otherwise → no action.

Both-or-neither salary semantics are preserved — an asymmetric drop would
leave a half-open range the rest of the pipeline does not expect.

Re-running is safe: after this migration, no rows match
``salary_min > 5_000_000 OR salary_max > 5_000_000`` unless a new buggy
write slips past the boundary guard.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Mirror of salary_extractor._MAX_PLAUSIBLE_SALARY. Inlined here so the
# migration's policy is preserved if the extractor constant ever moves
# — migrations are frozen-in-time semantics (MI-4); they must not
# silently follow application-code drift.
_MAX_PLAUSIBLE_SALARY = 5_000_000


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m086: jobs table not present, no-op")
        return

    inflated = conn.execute(
        """SELECT dedup_key, salary_min, salary_max
             FROM jobs
            WHERE (salary_min IS NOT NULL AND salary_min > ?)
               OR (salary_max IS NOT NULL AND salary_max > ?)""",
        (_MAX_PLAUSIBLE_SALARY, _MAX_PLAUSIBLE_SALARY),
    ).fetchall()

    for dedup_key, _smin, _smax in inflated:
        conn.execute(
            "UPDATE jobs SET salary_min = NULL, salary_max = NULL WHERE dedup_key = ?",
            (dedup_key,),
        )

    logger.info(
        "m086: nulled salary on %d row(s) with implausibly-inflated salary (bound=$%d)",
        len(inflated),
        _MAX_PLAUSIBLE_SALARY,
    )


MIGRATION = Migration(
    version=86,
    description="null salaries on rows where salary_min or salary_max > $5M",
    py=_heal,
)
