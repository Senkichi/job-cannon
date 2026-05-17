"""Migration 54 — onboarding_state.wizard_data for inter-step wizard state (Phase 42)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=54,
    description="onboarding_state.wizard_data for inter-step wizard state (Phase 42)",
    sql=[
        "ALTER TABLE onboarding_state ADD COLUMN wizard_data TEXT DEFAULT '{}'",
    ],
)
