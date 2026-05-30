"""Migration 70 — delete Glassdoor jobs persisted with company='Unknown'.

Background: the Glassdoor email parser had three code paths that all
defaulted ``company`` to the string ``"Unknown"`` when the company-name
CSS selector failed to match. The ingestion path then upserted these
into ``jobs`` with ``company='Unknown'`` and ``company_id`` IS NULL —
unresolvable orphans that polluted the company dropdown and inflated
the job count without contributing real signal.

By the audit on 2026-05-28, 80 rows from the last 7 days alone matched
this shape; an unknown larger count existed all-time. The parser fix
(``glassdoor_parser.py`` returns None when company can't be extracted,
across all three card-shape variants) prevents new rows from landing.
m070 retroactively deletes the existing orphans.

Scope is deliberately narrow — we only delete rows where ALL three
shape signatures match simultaneously:

  - ``company = 'Unknown'`` exactly (no other parser writes this
    literal — verified across the parsers package).
  - ``sources`` references glassdoor as a JSON-array element.
  - ``company_id`` IS NULL (an attached company_id would mean some
    later path resolved the row even if the parser fumbled the field;
    we leave those alone).

Re-running is safe — after the delete, no rows match the filter.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _drop(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m070: jobs table not present, no-op")
        return

    # Count + delete in one connection. JSON LIKE on the sources string
    # is fine here — the column stores a JSON array literal like
    # ``["glassdoor"]`` or ``["serpapi", "glassdoor"]``.
    where = "company = 'Unknown' AND sources LIKE '%\"glassdoor\"%' AND company_id IS NULL"
    found = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}").fetchone()[0]
    if found == 0:
        logger.info("m070: no Glassdoor 'Unknown' orphans to delete")
        return

    conn.execute(f"DELETE FROM jobs WHERE {where}")
    logger.info("m070: deleted %d Glassdoor 'Unknown' orphan job(s)", found)


MIGRATION = Migration(
    version=70,
    description="delete Glassdoor jobs persisted with company='Unknown' (parser orphans)",
    py=_drop,
)
