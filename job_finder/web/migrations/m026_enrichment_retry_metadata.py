"""Migration 26 — enrichment retry metadata on companies + last_scanned_at index."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=26,
    description="enrichment retry metadata on companies + last_scanned_at index",
    sql=[
        "ALTER TABLE companies ADD COLUMN enrichment_attempts INTEGER DEFAULT 0",
        "ALTER TABLE companies ADD COLUMN enrichment_last_attempted_at TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN enrichment_backoff_until TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN enrichment_last_error TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_companies_last_scanned_at ON companies(last_scanned_at)",
    ],
)
