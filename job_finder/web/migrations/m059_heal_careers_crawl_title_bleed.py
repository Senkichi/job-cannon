"""Migration 59 — heal existing careers_crawl title-bleed rows.

Background: prior to commit ab8e8b8 (2026-05-27) the careers_crawler
extraction step did not reject "metadata-blob" titles — strings where the
underlying HTML glued the title together with description, location,
posting date, and req ID without separator whitespace. The result was
rows with titles like::

    "Senior Data Scientist - GenAI ... SQL2354308|Chennai, Tamil Nadu"
    "Job TitleTech Lead AnalystPost levelNPSA-9Apply byApr-29-26AgencyUNDP..."
    "Engineer - $120,000 - $160,000 - Senior - Multiple locations"

The new pipeline drops these candidates before persistence, but the
already-polluted rows remain in the jobs table, polluting the UI and
wasting scoring spend.

This migration deletes the polluted rows, scoped conservatively:

  - sources is JSON-equal to ['careers_crawl']: multi-source rows are
    left alone because another source may have produced the bad title
    (out of scope for this fix) and the row may carry valid data from
    those other sources.
  - pipeline_status = 'discovered' (the system-default): we never
    delete a row that has moved past 'discovered', regardless of who
    moved it (user review, stale-detector archive, etc.). Conservative
    over surgical — we'd rather leave a junky title visible on an
    archived row than nuke a row the user touched.
  - title matches _is_metadata_blob from careers_crawler._title_filters
    — the exact same predicate the new pipeline uses to drop rows
    before they enter persistence, so this heal is consistent with
    the live filter.

Re-running is safe: each pass starts fresh and finds no candidates on
a clean DB or after the previous heal has run. The next careers_crawl
run on each affected company will re-discover any salvageable jobs
with proper titles (or skip them if the source page still produces a
metadata blob — which is the correct behavior).

Refs FOLLOWUPS.md ("Existing careers_crawl rows with title bleed are
not cleaned up", commit ab8e8b8).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.careers_crawler._title_filters import _is_metadata_blob
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _is_careers_crawl_only(sources_json: str | None) -> bool:
    """True when the row's sources list is exactly ['careers_crawl']."""
    if not sources_json:
        return False
    try:
        sources = json.loads(sources_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(sources, list):
        return False
    return sources == ["careers_crawl"]


def _heal_title_bleed(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    # Defensive: jobs table must exist. Should always be true past v1, but a
    # weirdly-staged DB upgrade would explode otherwise.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m059: jobs table not present, no-op")
        return

    # Candidate set: untouched careers_crawl rows. Filter pipeline_status
    # in SQL (cheap, indexable), then filter sources + predicate in Python
    # (need JSON parse + multi-clause heuristic). The jobs table is keyed by
    # dedup_key (TEXT PRIMARY KEY), not an integer id.
    candidates = conn.execute(
        """SELECT dedup_key, title, sources
             FROM jobs
            WHERE pipeline_status = 'discovered'
              AND title IS NOT NULL
              AND sources LIKE '%careers_crawl%'"""
    ).fetchall()

    deleted = 0
    for dedup_key, title, sources_json in candidates:
        if not _is_careers_crawl_only(sources_json):
            continue
        if not _is_metadata_blob(title or ""):
            continue
        conn.execute("DELETE FROM jobs WHERE dedup_key = ?", (dedup_key,))
        deleted += 1
        # Log a truncated preview of the offending title for audit. Full
        # blobs can be 500+ chars — head/tail keeps the log readable.
        preview = title if len(title) <= 100 else (title[:60] + "..." + title[-30:])
        logger.info(
            "m059: deleted careers_crawl title-bleed row dedup_key=%r title=%r",
            dedup_key,
            preview,
        )

    logger.info(
        "m059: removed %d careers_crawl-only rows with metadata-blob titles",
        deleted,
    )


MIGRATION = Migration(
    version=59,
    description="heal careers_crawl title-bleed rows (metadata-blob titles)",
    py=_heal_title_bleed,
)
