"""Migration 53 — create onboarding_state table."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=53,
    description="create onboarding_state table",
    sql=[
        """CREATE TABLE IF NOT EXISTS onboarding_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            onboarding_complete INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    ],
)
