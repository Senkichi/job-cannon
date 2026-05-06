"""Migration 37 — companies.careers_nav_recipe — Haiku-discovered navigation recipe for cached AI replays."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=37,
    description="companies.careers_nav_recipe — Haiku-discovered navigation recipe for cached AI replays",
    sql=[
        "ALTER TABLE companies ADD COLUMN careers_nav_recipe TEXT DEFAULT NULL",
    ],
)
