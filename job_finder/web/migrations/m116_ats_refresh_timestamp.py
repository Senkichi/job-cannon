"""Migration 115 — ATS mutable refresh-timestamp CAPTURE column.

Adds one nullable column holding the mutable last-updated/refresh timestamp
captured raw-as-provided from the ATS public JSON at ingest. CAPTURE stage of
the data-integrity overhaul (epic #393); normalization/consumption (the
divergence/repost flag) is a downstream stage, out of scope here.

  - ``ats_refreshed_at``  TEXT — raw ISO-8601 refresh timestamp; NULL = unknown.

Greenhouse is the only head scanner that exposes a usable mutable field on its
public endpoint (``updated_at``). Lever/Ashby/SmartRecruiters/Workday public
endpoints expose no such field, so this column stays NULL for them.

The ALTER is not IF-NOT-EXISTS-guardable in SQLite; the runner's version gate
runs it exactly once and swallows "duplicate column name" so a re-run is
idempotent. Existing rows get NULL (ingest-forward only — no backfill).

See ``m106_ats_structured_fields.py`` for the wrapper shape.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=115,
    description="ats capture: add ats_refreshed_at column (mutable refresh timestamp)",
    sql=["ALTER TABLE jobs ADD COLUMN ats_refreshed_at TEXT DEFAULT NULL"],
)
