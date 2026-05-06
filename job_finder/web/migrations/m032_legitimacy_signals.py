"""Migration 32 — jobs.legitimacy_note for ghost-job detection signals."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=32,
    description="jobs.legitimacy_note for ghost-job detection signals",
    sql=[
        "ALTER TABLE jobs ADD COLUMN legitimacy_note TEXT DEFAULT NULL",
    ],
)
