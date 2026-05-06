"""Migration 3 — pipeline_detections table for review queue and auto-update log."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=3,
    description="pipeline_detections table for review queue and auto-update log",
    sql=[
        """CREATE TABLE IF NOT EXISTS pipeline_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gmail_message_id TEXT NOT NULL UNIQUE,
            detection_type TEXT NOT NULL,
            job_id TEXT REFERENCES jobs(dedup_key),
            confidence_score INTEGER NOT NULL,
            matched_signals TEXT DEFAULT '[]',
            snippet TEXT DEFAULT '',
            email_subject TEXT DEFAULT '',
            email_from TEXT DEFAULT '',
            email_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_detections_status ON pipeline_detections(status)",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_detections_job_id ON pipeline_detections(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_detections_message_id ON pipeline_detections(gmail_message_id)",
    ],
)
