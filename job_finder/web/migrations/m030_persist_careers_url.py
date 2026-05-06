"""Migration 30 — companies.careers_url cache for find_careers_url() result."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=30,
    description="companies.careers_url cache for find_careers_url() result",
    sql=[
        "ALTER TABLE companies ADD COLUMN careers_url TEXT DEFAULT NULL",
    ],
)
