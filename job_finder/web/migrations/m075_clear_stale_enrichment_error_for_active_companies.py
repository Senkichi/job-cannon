"""Migration 75 — clear stale enrichment_last_error for demonstrably-active companies.

Background: ``companies.enrichment_last_error`` records the last
failure of the company-enrichment pipeline (LinkedIn-style metadata
fetch — size, industry, etc.). 'no_signals_found' is the value
written when that pipeline ran but couldn't pull anything useful.

Pre-launch audit on 2026-05-28 found 624 companies tagged
``enrichment_last_error = 'no_signals_found'`` — initially flagged
as "dead long-tail companies", but a closer look showed:

  - 622 / 624 have jobs in the jobs table
  - 161 of them are ``ats_probe_status = 'hit' AND scan_enabled = 1``
    (fully wired into the ATS scanner)
  - 851 jobs were ingested via this cohort in the last 30 days

The tag is a stale artifact from the enrichment pipeline's view of
the company — independent of whether the ATS scanner / careers crawler
can find jobs at the company. Operators reading the admin dashboard
saw 624 'errored' companies that were actually working fine.

m075 clears the field for the cohort where there's clear positive
evidence the company is active:

  - ``ats_probe_status = 'hit'`` (ATS confirmed), OR
  - ``jobs_found_total > 0`` (the scanner has produced at least one
    job through this company), OR
  - ``last_scanned_at >= datetime('now','-30 days')`` (scanner has
    recently visited this company)

These companies don't have a useful "enrichment error" to display —
the tag was alarmist noise. Companies that are genuinely
unreachable keep the tag for legitimate diagnostic value.

Re-running is safe — the filter only matches rows still carrying the
stale tag.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _clear(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'companies'"
    ).fetchone()
    if row is None:
        logger.info("m075: companies table not present, no-op")
        return

    where = """enrichment_last_error = 'no_signals_found'
               AND (
                   ats_probe_status = 'hit'
                   OR jobs_found_total > 0
                   OR (last_scanned_at IS NOT NULL
                       AND last_scanned_at >= datetime('now','-30 days'))
               )"""
    found = conn.execute(f"SELECT COUNT(*) FROM companies WHERE {where}").fetchone()[0]
    if found == 0:
        logger.info("m075: no stale 'no_signals_found' labels to clear")
        return

    conn.execute(
        f"""UPDATE companies SET enrichment_last_error = NULL
            WHERE {where}"""
    )
    logger.info(
        "m075: cleared stale enrichment_last_error='no_signals_found' on %d active company row(s)",
        found,
    )


MIGRATION = Migration(
    version=75,
    description="clear enrichment_last_error='no_signals_found' for demonstrably-active companies",
    py=_clear,
)
