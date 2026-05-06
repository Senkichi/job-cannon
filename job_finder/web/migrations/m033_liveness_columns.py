"""Migration 33 — liveness checker columns on jobs (later dropped by Mig 39)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=33,
    description="liveness checker columns on jobs (later dropped by Mig 39)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN liveness_checked_at TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN liveness_status TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN liveness_reason TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_liveness ON jobs(liveness_checked_at, pipeline_status)",
    ],
)
