"""Migration 39 — drop dead liveness_* columns from jobs (functionality merged into expiry_checker)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=39,
    description="drop dead liveness_* columns from jobs (functionality merged into expiry_checker)",
    sql=[
        "DROP INDEX IF EXISTS idx_jobs_liveness",
        "ALTER TABLE jobs DROP COLUMN liveness_checked_at",
        "ALTER TABLE jobs DROP COLUMN liveness_status",
        "ALTER TABLE jobs DROP COLUMN liveness_reason",
    ],
)
