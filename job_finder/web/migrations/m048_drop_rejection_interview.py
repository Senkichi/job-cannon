"""Migration 48 — public-repo cleanup: drop interview_preps / rejection_reports / rejection_pattern_reports + jobs.rejection_reviewed."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=48,
    description=(
        "public-repo cleanup: drop interview_preps / rejection_reports / "
        "rejection_pattern_reports + jobs.rejection_reviewed"
    ),
    sql=[
        "DROP INDEX IF EXISTS idx_interview_preps_job_id",
        "DROP TABLE IF EXISTS interview_preps",
        "DROP TABLE IF EXISTS rejection_reports",
        "DROP TABLE IF EXISTS rejection_pattern_reports",
        "ALTER TABLE jobs DROP COLUMN rejection_reviewed",
    ],
)
