"""Migration 2 — AI scoring columns: haiku/sonnet scores, jd_full, fit_analysis, is_stale."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=2,
    description="AI scoring columns: haiku/sonnet scores, jd_full, fit_analysis, is_stale",
    sql=[
        "ALTER TABLE jobs ADD COLUMN haiku_score REAL DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN haiku_summary TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN sonnet_score REAL DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN fit_analysis TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN jd_full TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN is_stale INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_jobs_haiku_score ON jobs(haiku_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_is_stale ON jobs(is_stale)",
    ],
)
