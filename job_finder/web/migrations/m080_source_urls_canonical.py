"""Migration 80 — source_urls_raw forensic column + source_urls canonicalization.

Phase 49.01 (NG-03 complement):

Three-step schema change:

  1. ``ALTER TABLE jobs ADD COLUMN source_urls_raw TEXT`` (JSON list; default NULL)
  2. Backfill: ``UPDATE jobs SET source_urls_raw = source_urls WHERE source_urls_raw IS NULL``
     — saves the pre-canonicalization originals before we rewrite them.
  3. Rewrite: for every row, parse the ``source_urls`` JSON array, canonicalize
     each URL via ``canonicalize_url``, and write the result back.  Row-iteration
     (not a single SQL UPDATE) because SQLite cannot call Python helpers inline
     without a UDF.

After this migration:
  - ``source_urls`` contains canonical URLs (tracking params stripped, sorted).
  - ``source_urls_raw`` contains the original URLs for forensic backtracking.
  - New writes (via ``upsert_job``) carry the same invariant because
    ``canonicalize_url`` runs in ``ParsedJob.from_job`` before ``upsert_job`` is
    called.

Down helper: drops the column (``ALTER TABLE DROP COLUMN`` — SQLite 3.35+,
available in Python 3.12+ shipped with SQLite ≥ 3.35).

NG-03: canonical URL is NOT used as a dedup key in this phase.

Reference:
    .planning/specs/2026-05-29-ingestion-contract-enforcement.md §13 commit 49.01.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext
from job_finder.web.url_canonical import canonicalize_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Up helper
# ---------------------------------------------------------------------------


def _up(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        is None
    ):
        logger.info("m080: jobs table not present, no-op")
        return

    # Step 1 — add column (idempotent: swallow "duplicate column" errors).
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_urls_raw TEXT")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
        logger.debug("m080: source_urls_raw column already present, skipping ADD COLUMN")

    # Step 2 — backfill raw from current source_urls BEFORE canonicalizing.
    # This preserves the original URLs as forensic evidence.
    conn.execute("UPDATE jobs SET source_urls_raw = source_urls WHERE source_urls_raw IS NULL")
    conn.commit()

    # Step 3 — rewrite source_urls to canonical form (row-by-row; needs Python helper).
    rows = conn.execute(
        "SELECT dedup_key, source_urls FROM jobs WHERE source_urls IS NOT NULL"
    ).fetchall()

    updated = 0
    for dedup_key, source_urls_json in rows:
        try:
            raw_list = json.loads(source_urls_json) if source_urls_json else []
        except (json.JSONDecodeError, TypeError):
            logger.debug("m080: skipping dedup_key=%r — source_urls not valid JSON", dedup_key)
            continue

        if not isinstance(raw_list, list):
            continue

        canonical_list = [canonicalize_url(u)[0] for u in raw_list if u]
        canonical_json = json.dumps(canonical_list)

        if canonical_json != source_urls_json:
            conn.execute(
                "UPDATE jobs SET source_urls = ? WHERE dedup_key = ?",
                (canonical_json, dedup_key),
            )
            updated += 1

    conn.commit()

    logger.info(
        "m080: added source_urls_raw column; canonicalized source_urls for %d of %d row(s)",
        updated,
        len(rows),
    )


# ---------------------------------------------------------------------------
# Down helper
# ---------------------------------------------------------------------------


def _down(ctx: MigrationContext) -> None:
    """Reverse migration 80 — drop source_urls_raw column.

    Note: source_urls tracking params are NOT restored — the down path only
    removes the forensic column.  URL content is considered ephemeral / best-
    effort; restoring original tracking params from source_urls_raw would
    require swapping the columns and that is explicitly out of scope (NG-03).
    """
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        is None
    ):
        return

    try:
        conn.execute("ALTER TABLE jobs DROP COLUMN source_urls_raw")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "no such column" not in str(exc).lower():
            raise
        logger.debug("m080: source_urls_raw column already absent, no-op")


# ---------------------------------------------------------------------------
# Migration declaration
# ---------------------------------------------------------------------------

MIGRATION = Migration(
    version=80,
    description="add source_urls_raw forensic column; canonicalize source_urls (strip tracking params)",
    py=_up,
)
