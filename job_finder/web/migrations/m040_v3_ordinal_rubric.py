"""Migration 40 — v3.0 ordinal rubric scoring: jobs.classification, .sub_scores_json, .scoring_model + index."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=40,
    description="v3.0 ordinal rubric scoring: jobs.classification, .sub_scores_json, .scoring_model + index",
    sql=[
        "ALTER TABLE jobs ADD COLUMN classification TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN sub_scores_json TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN scoring_model TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_classification ON jobs(classification)",
    ],
)
