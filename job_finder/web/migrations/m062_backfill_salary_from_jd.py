"""Migration 62 — backfill salary_min / salary_max from existing jd_full.

Background: the new ``salary_extractor`` module gives the enrichment
pipeline a deterministic regex pass at salary extraction ahead of the
LLM call (see _apply_post_fetch_extraction). Going forward, freshly
enriched jobs benefit immediately. m062 applies the same regex to
existing rows that already have a jd_full but whose salary fields are
NULL — common case for jobs scraped before this fix.

Scope (conservative):

  - Targets only rows where BOTH ``salary_min`` IS NULL AND
    ``salary_max`` IS NULL. Never overwrites an existing salary that
    came from a source API (LinkedIn API field, SerpAPI extension,
    etc.) — those win even if they happen to disagree with the JD text.

  - Requires a usable ``jd_full`` (length >= 200 chars matches the
    enrichment-tier minimum at MIN_FETCH_JD_CHARS-ish).

  - Salary extracted only when the regex hits both min and max with
    plausible values (both-or-neither — same contract as the live
    path).

Re-running is safe: any row where m062 already populated salary will
no longer match the NULL filter, so subsequent runs are no-ops.

Refs FOLLOWUPS.md ("Investigate + improve salary-info coverage" from
the 2026-05-27 User Bug List).
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext
from job_finder.web.salary_extractor import extract_salary_from_text

logger = logging.getLogger(__name__)

# Minimum jd_full length to attempt extraction. Below this the row was
# almost certainly never properly enriched and won't contain a real
# salary range anyway. Mirrors the live-path threshold loosely.
_MIN_JD_LEN = 200


def _backfill(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m062: jobs table not present, no-op")
        return

    candidates = conn.execute(
        """SELECT dedup_key, jd_full
             FROM jobs
            WHERE salary_min IS NULL
              AND salary_max IS NULL
              AND jd_full IS NOT NULL
              AND LENGTH(jd_full) >= ?""",
        (_MIN_JD_LEN,),
    ).fetchall()

    filled = 0
    for dedup_key, jd_full in candidates:
        salary_min, salary_max = extract_salary_from_text(jd_full)
        if salary_min is None or salary_max is None:
            continue
        conn.execute(
            "UPDATE jobs SET salary_min = ?, salary_max = ? WHERE dedup_key = ?",
            (salary_min, salary_max, dedup_key),
        )
        filled += 1

    logger.info(
        "m062: backfilled salary on %d row(s) from %d candidate JDs",
        filled,
        len(candidates),
    )


MIGRATION = Migration(
    version=62,
    description="backfill salary_min/max from existing jd_full via deterministic regex",
    py=_backfill,
)
