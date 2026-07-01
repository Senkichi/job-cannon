"""Migration 117 — applications table (prepare-layer review queue)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=117,
    description="applications table — prepared application packages awaiting owner review",
    sql=[
        """CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE REFERENCES jobs(dedup_key),
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL,
            resume_content TEXT NOT NULL DEFAULT '',
            form_mapping_json TEXT NOT NULL DEFAULT '{}',
            drafted_answers_json TEXT NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)",
        "CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id)",
    ],
)
