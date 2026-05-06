"""Migration 14 — Phase 30 infrastructure: jobs.expiry_checked_at + index, resume_generations.validation_report."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=14,
    description="Phase 30 infrastructure: jobs.expiry_checked_at + index, resume_generations.validation_report",
    sql=[
        "ALTER TABLE jobs ADD COLUMN expiry_checked_at TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_expiry_checked_at ON jobs(expiry_checked_at)",
        "ALTER TABLE resume_generations ADD COLUMN validation_report TEXT DEFAULT NULL",
    ],
)
