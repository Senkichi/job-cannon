"""Migration 106 — ATS structured-field CAPTURE columns (#451).

Adds three nullable columns that hold structured values captured raw-as-provided
from the ATS public JSON (Greenhouse / Workday / Lever / Ashby / SmartRecruiters)
at ingest. This is the CAPTURE stage of the data-integrity overhaul (epic #393);
normalization / reconciliation of these values is a downstream stage and is
explicitly out of scope here.

  - ``is_remote``        INTEGER — SQLite bool; NULL = unknown (distinct from 0).
  - ``employment_type``  TEXT    — raw, un-normalized (e.g. "FullTime", "Full-time").
  - ``department``       TEXT    — raw, un-normalized department / team string.

The ALTER TABLE statements are not ``IF NOT EXISTS``-guardable in SQLite; the
migration runner's version gate ensures this runs exactly once, and it also
swallows "duplicate column name" so a re-run is idempotent. Existing rows get
NULL (capture is ingest-forward only — no backfill from stored payloads).

See ``m085_direct_url.py`` for the ``Migration`` wrapper shape.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=106,
    description="ats capture: add is_remote + employment_type + department columns (#451)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN is_remote INTEGER DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN employment_type TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN department TEXT DEFAULT NULL",
    ],
)
