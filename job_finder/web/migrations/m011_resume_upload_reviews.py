"""Migration 11 — resume_upload_reviews table (RESUME-01, Phase 17)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=11,
    description="resume_upload_reviews table (RESUME-01, Phase 17)",
    sql=[
        """CREATE TABLE IF NOT EXISTS resume_upload_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending'
        )""",
    ],
)
