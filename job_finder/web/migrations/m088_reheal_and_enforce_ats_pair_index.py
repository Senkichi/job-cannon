"""Migration 88 — re-heal (ats_platform, ats_slug) clusters and enforce the
partial UNIQUE index that m076 was supposed to create.

Background: on some existing databases ``idx_companies_ats_pair`` is absent and
up to 9 ``(ats_platform, ats_slug)`` clusters with n>1 remain even though the
DB is at user_version=85.  This happens when m068/m076 ran against an older code
path or the index was later dropped.  This migration is the idempotent self-
repair:

  1. Re-run the m068 heal logic via the shared ``company_dedup`` module (no-op
     when no clusters remain).
  2. ``CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_ats_pair`` — idempotent
     even if the index already exists.

No ``RuntimeError`` pre-flight gate here (unlike m076) — this migration is
explicitly a *repair* migration and must not fail on the very DBs it was
designed to fix.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.company_dedup import heal_ats_slug_clusters
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

_CREATE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_ats_pair "
    "ON companies(ats_platform, ats_slug) "
    "WHERE ats_platform IS NOT NULL AND ats_slug IS NOT NULL"
)


def _heal_and_index(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    # Guard: companies table may not exist on a fresh DB pre-Migration 1.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='companies'"
    ).fetchone()
    if row is None:
        logger.info("m088: companies table not present — no-op")
        return

    # Step 1: heal remaining (ats_platform, ats_slug) clusters.
    stats = heal_ats_slug_clusters(conn)
    logger.info(
        "m088: healed %d cluster(s), merged %d companies, moved %d jobs",
        stats["clusters_resolved"],
        stats["companies_merged"],
        stats["jobs_moved"],
    )

    # Step 2: (re-)create the partial UNIQUE index.  IF NOT EXISTS makes this
    # safe to run when the index is already present.
    conn.execute(_CREATE_INDEX_SQL)
    logger.info("m088: idx_companies_ats_pair ensured")


MIGRATION = Migration(
    version=88,
    description="re-heal ATS slug clusters and enforce idx_companies_ats_pair",
    py=_heal_and_index,
)
