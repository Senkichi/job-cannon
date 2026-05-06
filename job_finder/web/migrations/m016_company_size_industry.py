"""Migration 16 — companies.company_size + .industry from DDG enrichment."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=16,
    description="companies.company_size + .industry from DDG enrichment",
    sql=[
        "ALTER TABLE companies ADD COLUMN company_size TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN industry TEXT DEFAULT NULL",
    ],
)
