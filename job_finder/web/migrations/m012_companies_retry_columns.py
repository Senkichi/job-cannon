"""Migration 12 — ATS retry columns on companies — fix for Mig 10 which mistakenly added them to jobs."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=12,
    description="ATS retry columns on companies — fix for Mig 10 which mistakenly added them to jobs",
    sql=[
        "ALTER TABLE companies ADD COLUMN retry_count INTEGER DEFAULT 0",
        "ALTER TABLE companies ADD COLUMN retry_after TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN miss_reason TEXT DEFAULT NULL",
    ],
)
