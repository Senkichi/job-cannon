"""Migration 36 — companies.careers_crawl_tier — last successful extraction tier (static/url_param/playwright/api_cached)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=36,
    description=(
        "companies.careers_crawl_tier — last successful extraction tier "
        "(static/url_param/playwright/api_cached)"
    ),
    sql=[
        "ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT DEFAULT NULL",
    ],
)
