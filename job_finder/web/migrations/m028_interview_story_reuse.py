"""Migration 28 — interview_preps.reusable_stories_json for STAR-story reuse across applications."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=28,
    description="interview_preps.reusable_stories_json for STAR-story reuse across applications",
    sql=[
        "ALTER TABLE interview_preps ADD COLUMN reusable_stories_json TEXT DEFAULT NULL",
    ],
)
