"""Migration 71 — tag heuristic-only-scored rows with scoring_provider.

Background: jobs.score is populated by the heuristic JobScorer at
ingestion. The v3.0 LLM pass (persist_job_assessment) writes
classification + sub_scores_json AND updates scoring_provider /
scoring_model to identify the LLM that ran. Rows where the LLM never
ran (sub-threshold, filtered, dismissed before scoring) historically
landed with scoring_provider IS NULL — making them look indistinguishable
from rows where the heuristic also never ran. By the audit on
2026-05-28, 2 799 rows had ``score IS NOT NULL`` with
``scoring_provider IS NULL``, presenting a heuristic score to UI
consumers that could not tell it apart from an LLM score.

Going forward, the upsert INSERT path now writes
``scoring_provider='heuristic'`` at row creation, so the boundary is
honest by default. m071 retroactively applies the same tag to existing
rows that match the heuristic-only shape:

  - ``score IS NOT NULL``
  - ``scoring_provider IS NULL``

The LLM path keeps using ``COALESCE`` for the assessment write, so
when an LLM runs on a 'heuristic'-tagged row, the COALESCE prefers the
incoming non-NULL provider value and the tag flips correctly.
``scoring_model`` remains the authoritative discriminator for "LLM
actually ran on this row".

Re-running is safe — after the tag, no rows match the heuristic-only
shape (any remaining ``scoring_provider IS NULL`` row also has
``score IS NULL``, meaning even the heuristic didn't run).
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _tag(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m071: jobs table not present, no-op")
        return

    found = conn.execute(
        """SELECT COUNT(*) FROM jobs
            WHERE score IS NOT NULL AND scoring_provider IS NULL"""
    ).fetchone()[0]
    if found == 0:
        logger.info("m071: no heuristic-only rows to tag")
        return

    conn.execute(
        """UPDATE jobs SET scoring_provider = 'heuristic'
            WHERE score IS NOT NULL AND scoring_provider IS NULL"""
    )
    logger.info("m071: tagged %d row(s) scoring_provider='heuristic'", found)


MIGRATION = Migration(
    version=71,
    description="tag historical heuristic-only-scored rows with scoring_provider='heuristic'",
    py=_tag,
)
