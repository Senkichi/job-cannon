"""Migration 31 — companies.careers_crawl_last_at for freshness-based crawler rotation."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=31,
    description="companies.careers_crawl_last_at for freshness-based crawler rotation",
    sql=[
        "ALTER TABLE companies ADD COLUMN careers_crawl_last_at TEXT DEFAULT NULL",
    ],
)
