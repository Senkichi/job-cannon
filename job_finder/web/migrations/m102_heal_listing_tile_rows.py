"""Migration 102 — heal existing result-count / category-landing tile rows (#211).

Background: prior to the #211 fix the careers crawler's static tier harvested
careers-page *category landing* links as if they were single postings. A link
like ``https://www.capitalonecareers.com/category/data-science-jobs/...`` has
anchor text ``"84 Data Scientist Jobs"`` — which ordered-words-matches the
target title "Data Scientist" and so passed the keyword gate. The whole
category page (nav chrome + search form + marketing copy) became ``jd_full``,
the row was scored, and several were classified ``apply`` and surfaced on the
apply list as if they were applyable postings.

The new pipeline hard-drops these at ``ParsedJob.from_job`` (I-14,
``ListingTileError``) and in the static tier early-exit, but the rows already
written before the fix remain in the jobs table — polluting the apply list and
wasting scoring spend. This migration removes them.

Scope (conservative, mirroring m059):

  - title matches ``is_listing_tile`` — the exact predicate the live filter
    uses, so this heal stays consistent with what now enters the pipeline.
  - pipeline_status = 'discovered' (the system default): never delete a row
    the user (or stale-detector) has moved past 'discovered'. We'd rather
    leave a junky title visible on a touched row than nuke something the user
    interacted with.
  - NOT scoped by source: a count tile is categorically not a posting no
    matter which ingestion path produced it. The known offenders were
    ``source=careers_page`` (the audit's "4 Capital One tiles", two of them
    whitespace-variant dups — ties #212); scanning the whole DB also catches
    any future-imported variant.

Idempotent: a clean DB (or a DB already healed) yields no candidates. The next
crawl of each affected company simply will not re-create the tile (the live
filter now rejects it).

Refs #211 (R1a, cluster A); 2026-06-08 jobs.db audit.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.careers_crawler._title_filters import is_listing_tile
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _heal_listing_tiles(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    # Defensive: jobs table must exist (always true past v1).
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m102: jobs table not present, no-op")
        return

    # Pre-filter in SQL (cheap, indexable) to untouched rows whose title
    # starts with a digit — the listing-tile shape requires a leading count,
    # so a digit-leading prefix is a safe, lossless narrowing of the scan.
    # The authoritative match is the Python predicate (regex), applied below.
    candidates = conn.execute(
        """SELECT dedup_key, title
             FROM jobs
            WHERE pipeline_status = 'discovered'
              AND title IS NOT NULL
              AND title GLOB '[0-9]*'"""
    ).fetchall()

    deleted = 0
    for dedup_key, title in candidates:
        if not is_listing_tile(title or ""):
            continue
        conn.execute("DELETE FROM jobs WHERE dedup_key = ?", (dedup_key,))
        deleted += 1
        logger.info(
            "m102: deleted result-count tile row dedup_key=%r title=%r",
            dedup_key,
            title,
        )

    logger.info("m102: removed %d result-count / category-landing tile rows", deleted)


MIGRATION = Migration(
    version=102,
    description="heal result-count / category-landing tile rows (#211)",
    py=_heal_listing_tiles,
)
