"""Migration 5 — Phase 5 Intelligence tables: interview_preps, resume_preferences_detected, rejection_reports."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=5,
    description=(
        "Phase 5 Intelligence: interview_preps, resume_preferences_detected, "
        "rejection_reports + jobs.rejection_reviewed + last_drive_polled_at"
    ),
    sql=[
        # interview_preps: stores Opus-generated interview prep per applied job
        """CREATE TABLE IF NOT EXISTS interview_preps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(dedup_key),
            status TEXT NOT NULL DEFAULT 'generating',
            company_brief TEXT DEFAULT NULL,
            predicted_questions TEXT DEFAULT '[]',
            gap_mitigation TEXT DEFAULT '[]',
            questions_to_ask TEXT DEFAULT '[]',
            error_msg TEXT DEFAULT NULL,
            generated_at TEXT NOT NULL,
            cost_usd REAL DEFAULT 0.0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_interview_preps_job_id ON interview_preps(job_id)",
        # resume_preferences_detected: per-preference rows from Drive diff analysis
        """CREATE TABLE IF NOT EXISTS resume_preferences_detected (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(dedup_key),
            preference_type TEXT NOT NULL,
            preference_text TEXT NOT NULL,
            example_before TEXT DEFAULT NULL,
            example_after TEXT DEFAULT NULL,
            accepted INTEGER NOT NULL DEFAULT 1,
            detected_at TEXT NOT NULL,
            applied_at TEXT DEFAULT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prefs_detected_job_id ON resume_preferences_detected(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_prefs_detected_accepted ON resume_preferences_detected(accepted)",
        # rejection_reports: Opus batch analysis reports stored for Dashboard display
        """CREATE TABLE IF NOT EXISTS rejection_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_text TEXT NOT NULL,
            rejections_analyzed INTEGER NOT NULL DEFAULT 0,
            generated_at TEXT NOT NULL,
            cost_usd REAL DEFAULT 0.0
        )""",
        # rejection_reviewed flag on jobs — 0=unreviewed, 1=included in a report
        "ALTER TABLE jobs ADD COLUMN rejection_reviewed INTEGER NOT NULL DEFAULT 0",
        # last_drive_polled_at on resume_generations — Drive feedback poll timestamp
        "ALTER TABLE resume_generations ADD COLUMN last_drive_polled_at TEXT DEFAULT NULL",
    ],
)
