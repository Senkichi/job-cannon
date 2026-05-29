"""Migration 74 — disable scan_enabled for companies we know can't be scanned.

Background: ``scan_enabled INTEGER DEFAULT 1`` (m007) means every new
company is auto-enabled for scanning at creation time. Companies that
later prove unscannable (probe returned 'miss' AND no ats_platform was
ever determined) keep scan_enabled=1 — misleading the admin UI / costs
dashboard and bloating queries that filter on it.

Audit on 2026-05-28 found 2 848 such rows. The scanner's Phase A
query already requires ``ats_probe_status = 'hit'``, so there's no
functional waste — but the flag's semantics drift: 'enabled to scan'
should mean 'will actually be scanned'.

m074 disables the flag for the demonstrably-unscannable cohort:

  - ``ats_probe_status = 'miss'``
  - AND (``ats_platform IS NULL`` OR ``ats_platform = ''``)

These rows have been probed and no ATS platform was detected. They
won't be reachable until something rediscovers an ATS for them (at
which point the probe will flip to 'hit' and scan_enabled can be
re-enabled by the operator or by a future probe-success path).

Pending-probe rows and 'hit' rows are intentionally left alone — they
may still resolve to a real platform.

Re-running is safe — after the toggle, the filter matches nothing.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _disable(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'companies'"
    ).fetchone()
    if row is None:
        logger.info("m074: companies table not present, no-op")
        return

    found = conn.execute(
        """SELECT COUNT(*) FROM companies
            WHERE scan_enabled = 1
              AND ats_probe_status = 'miss'
              AND (ats_platform IS NULL OR ats_platform = '')"""
    ).fetchone()[0]
    if found == 0:
        logger.info("m074: no unscannable scan_enabled=1 rows to disable")
        return

    conn.execute(
        """UPDATE companies SET scan_enabled = 0
            WHERE scan_enabled = 1
              AND ats_probe_status = 'miss'
              AND (ats_platform IS NULL OR ats_platform = '')"""
    )
    logger.info("m074: disabled scan_enabled on %d unscannable row(s)", found)


MIGRATION = Migration(
    version=74,
    description="disable scan_enabled for companies with ats_probe_status='miss' and no ats_platform",
    py=_disable,
)
