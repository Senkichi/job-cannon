"""Migration 13 — drop dead ATS retry columns from jobs (Phase 19 cleanup of Mig 10's mistake).

SQLite 3.35+ supports ALTER TABLE DROP COLUMN; confirmed 3.49.1 in this environment.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=13,
    description="drop dead ATS retry columns from jobs (Phase 19 cleanup of Mig 10's mistake)",
    sql=[
        "ALTER TABLE jobs DROP COLUMN ats_retry_count",
        "ALTER TABLE jobs DROP COLUMN ats_last_error",
        "ALTER TABLE jobs DROP COLUMN ats_retry_after",
    ],
)
