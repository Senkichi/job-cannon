"""Migration 18 — scoring_costs.provider for multi-provider tracking (default 'anthropic')."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=18,
    description="scoring_costs.provider for multi-provider tracking (default 'anthropic')",
    sql=[
        "ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic'",
    ],
)
