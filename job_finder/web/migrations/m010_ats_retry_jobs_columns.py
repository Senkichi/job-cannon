"""Migration 10 — ATS retry columns on jobs (DEBT-01, Phase 14) — later dropped by Migration 13."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=10,
    description="ATS retry columns on jobs (DEBT-01, Phase 14) — later dropped by Migration 13",
    sql=[
        "ALTER TABLE jobs ADD COLUMN ats_retry_count INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN ats_last_error TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN ats_retry_after TEXT DEFAULT NULL",
    ],
)
