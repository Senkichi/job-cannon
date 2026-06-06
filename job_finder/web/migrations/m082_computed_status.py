"""Migration 82 — computed_status VIRTUAL generated column (I-15).

Phase 49.05 (spec §13 commit 49.05; D-10; F-10; I-15).

Adds ``jobs.computed_status`` as a SQLite **VIRTUAL** GENERATED column that
resolves the three independently-written status signals (``pipeline_status``,
``is_stale``, ``expiry_status``) into one canonical value, eliminating the
F-10 drift (1,944 active-but-stale rows, 957 expired-but-not-stale rows) at the
read boundary.

VIRTUAL is mandatory: SQLite's ``ALTER TABLE ADD COLUMN`` supports VIRTUAL
generated columns but NOT STORED ones (a STORED add requires a full table
rebuild). VIRTUAL computes on read; the expression is cheap and indexable.

No backfill (computed on read). The column cannot be written — INSERT/UPDATE
paths never reference it (it is ``system``-categorized and excluded from the
ParsedJob mapping). The schema-correspondence test reads ``PRAGMA
table_xinfo`` (not ``table_info``) so this hidden/generated column is still
categorized.

Idempotency: the ADD COLUMN sits in ``sql`` so re-runs swallow
``duplicate column name``.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=82,
    description="add computed_status VIRTUAL generated column (I-15)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN computed_status TEXT "
        "GENERATED ALWAYS AS ("
        "  CASE"
        "    WHEN pipeline_status IN "
        "         ('applied','phone_screen','interviewing','offer','rejected','withdrawn')"
        "      THEN pipeline_status"
        "    WHEN is_stale = 1 THEN 'stale'"
        "    WHEN expiry_status = 'expired' THEN 'expired'"
        "    ELSE COALESCE(pipeline_status, 'active')"
        "  END"
        ") VIRTUAL"
    ],
)
