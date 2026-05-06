"""Migration 17 — companies.homepage_probe_attempted_at for retry-avoidance in homepage discovery."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=17,
    description="companies.homepage_probe_attempted_at for retry-avoidance in homepage discovery",
    sql=[
        "ALTER TABLE companies ADD COLUMN homepage_probe_attempted_at TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_companies_homepage_probe_attempted_at ON companies(homepage_probe_attempted_at)",
    ],
)
