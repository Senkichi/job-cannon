"""Migration 58 — consolidate duplicate company rows.

Background: historical parser bugs produced two kinds of orphan rows in the
companies table:

1. **Numeric-prefix artifacts** from email parsers that accidentally captured
   a leading number or req-ID, e.g. ``100 Salesforce``, ``001_BCBSA``,
   ``558 evernorth sales operations``. These were classified as distinct
   companies because the leading prefix changed the normalized name.
   FOLLOWUPS.md (2026-05-27 audit) lists 12 known pairs that the prior
   prefix-strip migration couldn't merge because the canonical row already
   existed and ``upsert_company`` doesn't merge.

2. **Exact-name duplicates** from different ingestion paths, e.g. two
   ``veeva systems`` rows, two ``judi health`` rows. The companies table
   doesn't enforce uniqueness on ``name`` so these accumulate over time.

For each detected orphan↔canonical pair this migration:

  - Re-points ``jobs.company_id`` to the canonical row.
  - Re-points ``company_scan_log.company_id`` to the canonical row.
  - Re-points ``company_research.company_id`` (rows from m029) — wrapped in
    a table-exists check because the m029 migration ran at the same time as
    m047/m048 and one fork of the schema may not have the table.
  - DELETEs the orphan row.

This is a one-shot heal — it consumes whatever state happens to exist now
and is a no-op on fresh databases or on databases where the prefix-strip /
exact-dup pattern doesn't appear. Detection is data-driven (queries the
companies table at migration time), not hardcoded to the 12 specific IDs in
FOLLOWUPS.md — those IDs only exist in the original author's DB.

Refs FOLLOWUPS.md ("Duplicate company rows after prefix-strip migration"
and User Generated Bug List item 5).
"""

from __future__ import annotations

import logging
import re
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Numeric/dash/underscore prefix patterns that indicate parser-artifact rows.
# Matches "100 ", "001_", "558 ", "00100 " — leading digits (1-6) followed by
# an optional underscore and any whitespace. Anchored at string start.
_NUMERIC_PREFIX_RE = re.compile(r"^\d{1,6}_?\s+")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _repoint_and_delete(conn: sqlite3.Connection, orphan_id: int, canonical_id: int) -> None:
    """Re-point all known FK references from orphan_id to canonical_id, then
    delete the orphan row. Called once per (orphan, canonical) pair."""
    conn.execute(
        "UPDATE jobs SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    conn.execute(
        "UPDATE company_scan_log SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    # m029 added company_research; some upgraded DBs may not have it (fork of
    # m029-skip exists in the wild — guarded for safety).
    if _table_exists(conn, "company_research"):
        conn.execute(
            "UPDATE company_research SET company_id = ? WHERE company_id = ?",
            (canonical_id, orphan_id),
        )
    conn.execute("DELETE FROM companies WHERE id = ?", (orphan_id,))


def _find_canonical_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    """Return the lowest id among companies with the given lowercased name.
    Returns None when no match exists."""
    row = conn.execute(
        "SELECT id FROM companies WHERE LOWER(name) = LOWER(?) ORDER BY id ASC LIMIT 1",
        (name,),
    ).fetchone()
    return row[0] if row else None


def _consolidate(ctx: MigrationContext) -> None:
    conn = ctx.conn

    # Defensive: companies table must exist. Migration 7 created it; if we got
    # here past version 57 it does, but a fresh DB created out-of-order would
    # break without this guard.
    if not _table_exists(conn, "companies"):
        logger.info("m058: companies table not present, no-op")
        return

    repointed_prefix = 0
    repointed_exact = 0

    # --- Pass 1: numeric-prefix orphans ---
    rows = conn.execute("SELECT id, name, name_raw FROM companies ORDER BY id ASC").fetchall()
    for orphan_id, orphan_name, _name_raw in rows:
        if not orphan_name:
            continue
        match = _NUMERIC_PREFIX_RE.match(orphan_name)
        if not match:
            continue
        stripped = orphan_name[match.end() :].strip()
        if not stripped:
            continue
        canonical_id = _find_canonical_by_name(conn, stripped)
        if canonical_id is None or canonical_id == orphan_id:
            continue
        _repoint_and_delete(conn, orphan_id, canonical_id)
        repointed_prefix += 1
        logger.info(
            "m058: merged numeric-prefix orphan id=%d (%r) into canonical id=%d (%r)",
            orphan_id,
            orphan_name,
            canonical_id,
            stripped,
        )

    # --- Pass 2: exact-name duplicates (case-insensitive) ---
    # Group by lowercased name and keep the lowest id as canonical. Any other
    # ids for the same name are orphans. This runs AFTER pass 1 so the
    # numeric-prefix merges don't produce false collisions here.
    dup_groups = conn.execute(
        """SELECT LOWER(name) AS lname, GROUP_CONCAT(id) AS ids, COUNT(*) AS n
             FROM companies
             WHERE name IS NOT NULL AND name != ''
             GROUP BY LOWER(name)
             HAVING n > 1"""
    ).fetchall()
    for lname, id_csv, _n in dup_groups:
        ids = sorted(int(x) for x in id_csv.split(","))
        canonical_id = ids[0]
        for orphan_id in ids[1:]:
            _repoint_and_delete(conn, orphan_id, canonical_id)
            repointed_exact += 1
            logger.info(
                "m058: merged exact-dup orphan id=%d into canonical id=%d (%r)",
                orphan_id,
                canonical_id,
                lname,
            )

    logger.info(
        "m058: consolidated %d numeric-prefix orphans + %d exact-name orphans",
        repointed_prefix,
        repointed_exact,
    )


MIGRATION = Migration(
    version=58,
    description="consolidate duplicate company rows (numeric-prefix artifacts + exact-name duplicates)",
    py=_consolidate,
)
