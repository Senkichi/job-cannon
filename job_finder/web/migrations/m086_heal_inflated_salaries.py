"""Migration 86 — heal rows where salary_min/salary_max exceed the plausibility ceiling.

Background: the LLM-based `parse_structured_fields` path historically returned
salaries verbatim with no magnitude bound, while the parallel regex path in
`salary_extractor` enforced `[$30K, $5M]`. Inflated values (~100x annual, e.g.
`salary_min=27_500_000` for a $275k role) sailed past the F2 reconcile guard
(`_reconcile_salary_for_write`, m069 retro-fit) whenever the pair was still
ordered (`min <= max`) — only the *inverted* subset was dropped. By the audit
on 2026-06-07, 29 rows had `salary_min > $1M`, 22 of those both-fields-inflated
ordered pairs, 7 single inflated `salary_min` with `salary_max IS NULL`.

The persistence boundary now enforces the magnitude bound at the LLM-parse
site itself (issue #228), so new rows can't land in this state. m086 retro-
fits the same `[$30K, $5M]` ceiling to existing rows: any `salary_min` or
`salary_max` outside the plausible range — including the asymmetric case of
one field set and the other NULL — gets BOTH fields NULL'd. The
both-or-neither salary semantics the rest of the pipeline relies on are
preserved (no half-open ranges leak).

Policy: NULL out both fields when either is out of bounds. Matches the F2
philosophy of dropping corrupt signal over attempting recovery — the
divide-by-100 alternative would assume the magnitude error is exactly 100x,
which is not verified per-row.

Re-running is safe: after this migration, no rows match the inflated cohort
unless a new buggy write slips past the boundary guard.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext
from job_finder.web.salary_extractor import _MAX_PLAUSIBLE_SALARY, _MIN_PLAUSIBLE_SALARY

logger = logging.getLogger(__name__)


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m086: jobs table not present, no-op")
        return

    cursor = conn.execute(
        """UPDATE jobs
              SET salary_min = NULL,
                  salary_max = NULL
            WHERE (salary_min IS NOT NULL AND
                   (salary_min < ? OR salary_min > ?))
               OR (salary_max IS NOT NULL AND
                   (salary_max < ? OR salary_max > ?))""",
        (
            _MIN_PLAUSIBLE_SALARY,
            _MAX_PLAUSIBLE_SALARY,
            _MIN_PLAUSIBLE_SALARY,
            _MAX_PLAUSIBLE_SALARY,
        ),
    )
    healed = cursor.rowcount if cursor.rowcount is not None else 0
    logger.info(
        "m086: healed %d row(s) with implausible salary (bounds=[%s, %s])",
        healed,
        _MIN_PLAUSIBLE_SALARY,
        _MAX_PLAUSIBLE_SALARY,
    )


MIGRATION = Migration(
    version=86,
    description="null out salary_min/salary_max when either is outside [$30K, $5M]",
    py=_heal,
)
