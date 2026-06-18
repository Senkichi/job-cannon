"""Migration 97 — dismiss historical aggregator/re-poster listings (#213).

Background: aggregators / re-posters (Virtual Vocations, ProSidian,
SynergisticIT) re-list *other* employers' jobs under their own brand. Their
postings legitimately score apply/consider — the underlying job is real — but
the row is attributed to the aggregator, is heavily duplicated, and routes the
user to a paywalled re-listing instead of the employer's own ATS. They polluted
the actionable apply/consider list.

The pre-scoring gate (``should_exclude`` → ``get_company_denylist``) now seeds
these names in the company denylist and matches on ``normalize_company`` so
legal-entity-suffix variants ("Virtual Vocations Inc") fire. New ingests are
auto-dismissed at scoring time. m097 retroactively applies the same demotion to
rows that were already classified before the gate existed.

Guard (matches the live gate's state guard in scoring_runner.run_scoring):
  - Only rows whose ``pipeline_status = 'discovered'`` transition to
    ``'dismissed'``. Rows the user has touched (applied / reviewing / archived /
    already dismissed) are NEVER modified — the gate must not overwrite manual
    pipeline state.

Frozen-in-time semantics (MI-4): the aggregator seed list is inlined here in
NORMALIZED form rather than imported from config, so the migration's effect is
stable even as the live denylist grows. These are exactly the #213 named
offenders. SynergisticIT appears both spaced and unspaced in the wild and
normalize_company does not collapse the space, so both variants are listed.

Re-running is safe: after this migration no 'discovered' row whose
normalized company is in the seed set remains; a fresh ingest of the same
aggregator is auto-dismissed by the live gate before it would ever reach this
state.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.normalizers import normalize_company
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Normalized (normalize_company output) aggregator/re-poster names this
# migration demotes. Mirrors the #213 seed added to config._RAW_COMPANY_DENYLIST.
_AGGREGATOR_DENYLIST = frozenset(
    {
        "virtual vocations",
        "prosidian consulting",
        "synergisticit",
        "synergistic it",
    }
)


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if table is None:
        logger.info("m097: jobs table not present, no-op")
        return

    # Pull only the cohort the guard can act on: currently 'discovered'.
    # Normalize each company in Python (no SQL UDF dependency in migrations) and
    # compare against the frozen seed set.
    rows = conn.execute(
        "SELECT dedup_key, company FROM jobs WHERE pipeline_status = 'discovered'"
    ).fetchall()

    dismissed = 0
    for dedup_key, company in rows:
        if not company:
            continue
        if normalize_company(company) in _AGGREGATOR_DENYLIST:
            conn.execute(
                "UPDATE jobs SET pipeline_status = 'dismissed' WHERE dedup_key = ?",
                (dedup_key,),
            )
            dismissed += 1

    logger.info(
        "m097: dismissed %d historical aggregator/re-poster row(s) (#213) "
        "(discovered -> dismissed; manual pipeline state untouched)",
        dismissed,
    )


MIGRATION = Migration(
    version=97,
    description="dismiss historical aggregator/re-poster listings matching the #213 denylist seed",
    py=_heal,
)
