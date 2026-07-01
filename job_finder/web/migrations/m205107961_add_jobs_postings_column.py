"""Migration 205107961 — add jobs postings column (#640).

Adds a ``postings`` TEXT column to the jobs table, defaulting to an empty JSON
array ``'[]'``. This column stores per-row posting sub-entities for direct ATS
sightings, each carrying its own ``{ats_platform, source_id, apply_url,
locations_structured, workplace_type, confidence}`` tuple. The column is
added with a default so existing rows get ``'[]'`` (no backfill in this phase —
that is Phase 4).

The ALTER TABLE statement is not ``IF NOT EXISTS``-guardable in SQLite; the
migration runner's version gate ensures this runs exactly once, and it also
swallows "duplicate column name" so a re-run is idempotent.

See ``m106_ats_structured_fields.py`` for the additive-column shape.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=205107961,
    description="add jobs postings column (#640)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN postings TEXT DEFAULT '[]'",
    ],
)
