"""Migration 50 — rename vestigial enrichment_tier literals to low/mid/high."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=50,
    description="rename vestigial enrichment_tier literals haiku/sonnet -> low/mid",
    sql=[
        "UPDATE jobs SET enrichment_tier='low' WHERE enrichment_tier='haiku'",
        "UPDATE jobs SET enrichment_tier='mid' WHERE enrichment_tier='sonnet'",
        # 'opus' presumably never appeared in this column (no historical
        # caller wrote it) but rewrite defensively for idempotency.
        "UPDATE jobs SET enrichment_tier='high' WHERE enrichment_tier='opus'",
    ],
)
