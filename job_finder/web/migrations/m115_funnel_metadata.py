"""Migration 115 — funnel metadata column for ingestion reconciliation identity (issue #587)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=115,
    description="funnel metadata column for ingestion reconciliation identity (issue #587)",
    sql=[
        # Add metadata column to runs table for storing funnel reconciliation dict
        "ALTER TABLE runs ADD COLUMN metadata TEXT DEFAULT '{}'",
    ],
)
