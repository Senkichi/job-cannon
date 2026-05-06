"""Migration 35 — companies.careers_api_endpoint cache for direct HTTP crawls (skip Playwright)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=35,
    description="companies.careers_api_endpoint cache for direct HTTP crawls (skip Playwright)",
    sql=[
        "ALTER TABLE companies ADD COLUMN careers_api_endpoint TEXT DEFAULT NULL",
    ],
)
