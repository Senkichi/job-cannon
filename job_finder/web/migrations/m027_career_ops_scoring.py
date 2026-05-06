"""Migration 27 — career-ops scoring metadata: jobs.expiry_status, .eval_blocks, .job_archetype."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=27,
    description="career-ops scoring metadata: jobs.expiry_status, .eval_blocks, .job_archetype",
    sql=[
        "ALTER TABLE jobs ADD COLUMN expiry_status TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN eval_blocks TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN job_archetype TEXT DEFAULT NULL",
    ],
)
