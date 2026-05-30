"""Migration 51 — consolidate user_activity batch_score_haiku/sonnet -> batch_score."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=51,
    description="consolidate user_activity batch_score_haiku/sonnet -> batch_score",
    sql=[
        "UPDATE user_activity SET action='batch_score' "
        "WHERE action IN ('batch_score_haiku', 'batch_score_sonnet')",
    ],
)
