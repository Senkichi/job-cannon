"""Migration 20 — jobs.scoring_provider for Sonnet-scoring provider attribution (ATTR-01)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=20,
    description="jobs.scoring_provider for Sonnet-scoring provider attribution (ATTR-01)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN scoring_provider TEXT DEFAULT 'anthropic'",
    ],
)
