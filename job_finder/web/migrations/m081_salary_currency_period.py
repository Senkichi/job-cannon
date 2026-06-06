"""Migration 81 — salary_currency + salary_period columns (I-14) + suspect flag.

Phase 49.02 (spec §13 commit 49.02; D-07; F-06; I-14; R-05).

Adds two parser-owned columns:
  - ``salary_currency`` TEXT NOT NULL DEFAULT 'USD'
      CHECK IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN')
  - ``salary_period``   TEXT NOT NULL DEFAULT 'unknown'
      CHECK IN ('annual','hourly','monthly','unknown')

I-14 is enforced by embedding the CHECK in the ``ALTER TABLE ADD COLUMN``
statement — legal in SQLite because the constraint applies to a new column at
creation time (a table-level CHECK would require a full table rebuild).

Backfill (suspect-flagging only — NO blind hourly→annual conversion, D-07):
rows whose ``salary_min`` looks unit-confused (``< 1000`` → likely an hourly
rate mis-stored as annual; ``> 1_000_000`` → likely cents mis-stored as dollars)
get ``'salary_unit_suspect'`` appended to ``unresolved_reasons`` so they surface
on /admin/review. The period stays ``'unknown'`` (the default) rather than being
guessed.

Idempotency: the ADD COLUMNs sit in ``sql`` so re-runs swallow
``duplicate column name``; the backfill checks for the reason code before
appending, so re-running never double-flags.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

_SUSPECT_REASON = "salary_unit_suspect"


def _backfill_suspect(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        is None
    ):
        logger.info("m081: jobs table not present, no-op")
        return

    rows = conn.execute(
        "SELECT dedup_key, unresolved_reasons FROM jobs "
        "WHERE salary_min IS NOT NULL AND (salary_min < 1000 OR salary_min > 1000000)"
    ).fetchall()

    flagged = 0
    for dedup_key, reasons_json in rows:
        try:
            reasons = json.loads(reasons_json) if reasons_json else []
        except (json.JSONDecodeError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []
        if _SUSPECT_REASON in reasons:
            continue  # idempotent — already flagged
        reasons.append(_SUSPECT_REASON)
        conn.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            (json.dumps(reasons), dedup_key),
        )
        flagged += 1

    logger.info("m081: flagged %d row(s) with %s", flagged, _SUSPECT_REASON)


MIGRATION = Migration(
    version=81,
    description="add salary_currency + salary_period (I-14 CHECK) + flag unit-suspect rows",
    sql=[
        "ALTER TABLE jobs ADD COLUMN salary_currency TEXT NOT NULL DEFAULT 'USD' "
        "CHECK(salary_currency IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN'))",
        "ALTER TABLE jobs ADD COLUMN salary_period TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK(salary_period IN ('annual','hourly','monthly','unknown'))",
    ],
    py=_backfill_suspect,
)
