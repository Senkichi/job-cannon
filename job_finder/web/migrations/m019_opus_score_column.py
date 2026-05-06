"""Migration 19 — jobs.opus_score for gold-standard baseline evaluation."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=19,
    description="jobs.opus_score for gold-standard baseline evaluation",
    sql=[
        "ALTER TABLE jobs ADD COLUMN opus_score REAL DEFAULT NULL",
    ],
)
