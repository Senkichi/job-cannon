"""Migration 73 — delete jobs whose company is in COMPANY_DENYLIST.

Background: aggregator placeholder names ("Jobgether", "Mercor",
"RemoteHunter", "Crossing Hurdles", "Unknown", "Medical Jobs",
"Clinical Jobs") appear in source feeds (linkedin / dataforseo /
monster) as the "company" field of jobs that are actually re-posts of
positions at real companies. The backfill code-path correctly skipped
creating company records for these names — but the jobs themselves were
already persisted with NULL company_id, polluting the jobs table and
the company-filter dropdown.

Audit on 2026-05-28 found 42 such orphan rows (Jobgether 22, Mercor 15,
RemoteHunter 4, Crossing Hurdles 1). The upsert boundary now rejects
denylisted names at ingest, so no new orphan can land. m073 retroactively
deletes the existing 42 (and any earlier ones that survived).

Scope: ``WHERE LOWER(company) IN denylist AND company_id IS NULL``.
The company_id NULL guard ensures we don't accidentally remove rows
that got linked to a real company despite the name overlap (e.g. a
genuine company called "Mercor" — none today, but defensive).

Re-running is safe — after the delete, no rows match the filter.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.config import COMPANY_DENYLIST
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _drop(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m073: jobs table not present, no-op")
        return

    if not COMPANY_DENYLIST:
        logger.info("m073: COMPANY_DENYLIST empty, no-op")
        return

    placeholders = ",".join("?" * len(COMPANY_DENYLIST))
    params = tuple(COMPANY_DENYLIST)

    found = conn.execute(
        f"""SELECT COUNT(*) FROM jobs
             WHERE LOWER(company) IN ({placeholders})
               AND company_id IS NULL""",
        params,
    ).fetchone()[0]
    if found == 0:
        logger.info("m073: no denylist-name orphans to delete")
        return

    conn.execute(
        f"""DELETE FROM jobs
             WHERE LOWER(company) IN ({placeholders})
               AND company_id IS NULL""",
        params,
    )
    logger.info("m073: deleted %d denylist-name orphan job(s)", found)


MIGRATION = Migration(
    version=73,
    description="delete jobs whose company name is in COMPANY_DENYLIST and company_id IS NULL",
    py=_drop,
)
