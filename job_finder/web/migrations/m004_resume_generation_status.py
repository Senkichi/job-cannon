"""Migration 4 — resume_generations status tracking columns + indexes for async workflow."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=4,
    description="resume_generations status tracking columns + indexes for async workflow",
    sql=[
        # Add status tracking columns; defaults ensure legacy rows remain valid.
        "ALTER TABLE resume_generations ADD COLUMN generation_type TEXT DEFAULT 'single'",
        "ALTER TABLE resume_generations ADD COLUMN status TEXT DEFAULT 'done'",
        "ALTER TABLE resume_generations ADD COLUMN strategy TEXT DEFAULT NULL",
        "ALTER TABLE resume_generations ADD COLUMN error_msg TEXT DEFAULT NULL",
        # Indexes for resume generation queries
        "CREATE INDEX IF NOT EXISTS idx_resume_generations_job_id ON resume_generations(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_resume_generations_status ON resume_generations(status)",
    ],
)
