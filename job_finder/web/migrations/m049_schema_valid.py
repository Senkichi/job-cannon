"""Migration 49 — scoring_costs.schema_valid for canary telemetry (default NULL)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=49,
    description="scoring_costs.schema_valid for canary telemetry (default NULL)",
    sql=[
        "ALTER TABLE scoring_costs ADD COLUMN schema_valid INTEGER DEFAULT NULL",
    ],
)
