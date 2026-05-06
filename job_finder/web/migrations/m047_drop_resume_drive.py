"""Migration 47 — public-repo cleanup: drop resume_generations / resume_preferences_detected / resume_upload_reviews."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=47,
    description=(
        "public-repo cleanup: drop resume_generations / resume_preferences_detected / "
        "resume_upload_reviews"
    ),
    sql=[
        "DROP INDEX IF EXISTS idx_resume_generations_job_id",
        "DROP INDEX IF EXISTS idx_resume_generations_status",
        "DROP INDEX IF EXISTS idx_prefs_detected_job_id",
        "DROP INDEX IF EXISTS idx_prefs_detected_accepted",
        "DROP TABLE IF EXISTS resume_generations",
        "DROP TABLE IF EXISTS resume_preferences_detected",
        "DROP TABLE IF EXISTS resume_upload_reviews",
    ],
)
