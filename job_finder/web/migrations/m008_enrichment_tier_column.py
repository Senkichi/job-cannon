"""Migration 8 — Phase 10 cost-optimized enrichment: enrichment_tier column + index + backfill."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=8,
    description="Phase 10 cost-optimized enrichment: enrichment_tier column + index + backfill",
    sql=[
        "ALTER TABLE jobs ADD COLUMN enrichment_tier TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_enrichment_tier ON jobs(enrichment_tier)",
        "UPDATE jobs SET enrichment_tier = 'serpapi' WHERE jd_full IS NOT NULL AND enrichment_tier IS NULL",
    ],
)
