"""Migration 64 — reset speculative-probe false positives for FP-prone platforms.

Closes audit item B1b from `.planning/ATS-COVERAGE-AUDIT-2026-05-27.md` (v2).

**Background.** The 2026-05-27 audit found that every single speculative-probe
hit for bamboohr / personio / recruitee / breezy in the live corpus came back
with `ats_evidence_trigger IS NULL` — 40 rows total, 100% FP rate. Famous-brand
names (Microsoft, Amazon, Meta, YouTube, Accenture, EY, Leidos, IQVIA, ...)
collided with real SMB tenants that registered the same {slug}={normalized_name}
on these platforms, and the probe returned a true 200 for the wrong company.

Commit B1a (`feat(ats_probe): exclude FP-prone platforms from speculative
ladder`) removed those 4 platforms from the speculative `_PROBES` ladder so
the probe can no longer create new FP rows in this cohort. This migration
cleans up the 40 existing rows.

**Selection criteria** (exactly mirrors the audit's B1 cohort definition):

    ats_probe_status = 'hit'
    AND ats_platform IN ('bamboohr', 'personio', 'recruitee', 'breezy')
    AND ats_evidence_trigger IS NULL

The `ats_evidence_trigger IS NULL` clause is the load-bearing filter — it
distinguishes rows that came from the (now-banned) speculative path from
rows that came from the evidence-based reconcile path. The latter would
have populated `ats_evidence_trigger` and is trusted. This migration leaves
those rows alone.

**Reset action.** For each matching row:

    ats_platform     = NULL
    ats_slug         = NULL
    ats_probe_status = 'pending'   ← lets the new (FP-prone-free) probe
                                     ladder reconsider them on the next
                                     scheduler tick
    miss_reason      = NULL        ← cleared so the next probe pass can
                                     populate it (B4 work-in-progress)

`jobs_found_total` and existing job rows are NOT touched. Those jobs were
ingested from independent feeds (LinkedIn / Glassdoor / DataForSEO / TrueUp)
and the scanner contributed zero of them; nulling the platform/slug only
removes the *attribution claim*, not the *job inventory*.

**Idempotent.** After this migration runs, no row satisfies the WHERE clause,
so a second invocation is a no-op. Rerunning is safe at any future point.

**Why a Python migration, not pure SQL.** The runner expects a `Migration`
value object; SQL-only migrations would still serialize to the same shape.
A `py` helper is used so the row-count log line names the cohort precisely
("reset N FAANG-FP cohort rows") rather than just dumping a `changes` count.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

_FP_PRONE_PLATFORMS = ("bamboohr", "personio", "recruitee", "breezy")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _reset(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if not _table_exists(conn, "companies"):
        logger.info("m064: companies table not present, no-op")
        return

    placeholders = ", ".join(["?"] * len(_FP_PRONE_PLATFORMS))
    where_clause = (
        f"ats_probe_status = 'hit' "
        f"AND ats_platform IN ({placeholders}) "
        f"AND ats_evidence_trigger IS NULL"
    )

    pre_count_row = conn.execute(
        f"SELECT COUNT(*) FROM companies WHERE {where_clause}",
        _FP_PRONE_PLATFORMS,
    ).fetchone()
    pre_count = int(pre_count_row[0]) if pre_count_row else 0

    if pre_count == 0:
        logger.info("m064: no FP-prone speculative-hit rows to reset (already clean)")
        return

    conn.execute(
        f"""UPDATE companies
            SET ats_platform = NULL,
                ats_slug = NULL,
                ats_probe_status = 'pending',
                miss_reason = NULL
            WHERE {where_clause}""",
        _FP_PRONE_PLATFORMS,
    )

    logger.info(
        "m064: reset %d FAANG-FP cohort rows (platform IN %s + NULL evidence)",
        pre_count,
        _FP_PRONE_PLATFORMS,
    )


MIGRATION = Migration(
    version=64,
    description="reset speculative-probe FPs for bamboohr/personio/recruitee/breezy (B1b)",
    py=_reset,
)
