"""Migration 72 — default workplace_type='UNSPECIFIED' for NULL rows.

The schema declares workplace_type as one of REMOTE / HYBRID / ONSITE /
UNSPECIFIED (see ``job_finder.web.location_canonical.WorkplaceType``).
Historically the upsert INSERT path passed NULL when location parsing
extracted no structured location, leaving 464 rows with workplace_type
NULL by the 2026-05-28 audit — most from careers_crawl / careers_page
tiers where the parser couldn't decode the page's location section.

Going forward, the upsert boundary defaults workplace_type to
'UNSPECIFIED' when locations_structured is empty, and the UPDATE
branch coalesces so 'UNSPECIFIED' from a re-ingestion never downgrades
a real value (REMOTE / HYBRID / ONSITE) that an earlier scan
extracted. m072 retroactively normalises the NULLs.

Scope:

  - ``workplace_type IS NULL OR workplace_type = ''`` → 'UNSPECIFIED'.

Re-running is safe — after the backfill no row has NULL/empty
workplace_type unless a brand-new row sneaks past the upsert guard.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _backfill(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m072: jobs table not present, no-op")
        return

    found = conn.execute(
        """SELECT COUNT(*) FROM jobs
            WHERE workplace_type IS NULL OR workplace_type = ''"""
    ).fetchone()[0]
    if found == 0:
        logger.info("m072: no NULL/empty workplace_type rows to backfill")
        return

    conn.execute(
        """UPDATE jobs SET workplace_type = 'UNSPECIFIED'
            WHERE workplace_type IS NULL OR workplace_type = ''"""
    )
    logger.info("m072: backfilled %d row(s) workplace_type='UNSPECIFIED'", found)


MIGRATION = Migration(
    version=72,
    description="default workplace_type='UNSPECIFIED' for rows missing the field",
    py=_backfill,
)
