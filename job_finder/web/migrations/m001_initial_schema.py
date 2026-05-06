"""Migration 1 — initial schema: jobs, runs, pipeline_events, email_parse_log, scoring_costs."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=1,
    description="initial schema: jobs, runs, pipeline_events, email_parse_log, scoring_costs",
    sql=[
        # Enable WAL mode -- persistent in the database file after first run.
        "PRAGMA journal_mode=WAL",
        "PRAGMA wal_autocheckpoint=1000",
        # Create the jobs table if it does not yet exist (fresh database).
        # This is identical to db.py._init_tables so both paths produce the same schema.
        """CREATE TABLE IF NOT EXISTS jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            sources TEXT DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            source_id TEXT DEFAULT '',
            salary_min INTEGER,
            salary_max INTEGER,
            description TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            score REAL DEFAULT 0,
            score_breakdown TEXT DEFAULT '{}',
            user_interest TEXT DEFAULT 'unreviewed'
        )""",
        # Create the runs table if it does not yet exist.
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            jobs_fetched INTEGER DEFAULT 0,
            jobs_new INTEGER DEFAULT 0,
            jobs_scored INTEGER DEFAULT 0
        )""",
        # Add new columns to jobs table.
        # These use DEFAULT so they are safe with existing rows.
        # If the column already exists (migration re-run), the error is caught below.
        "ALTER TABLE jobs ADD COLUMN pipeline_status TEXT DEFAULT 'discovered'",
        "ALTER TABLE jobs ADD COLUMN posted_date TEXT",
        "ALTER TABLE jobs ADD COLUMN notes TEXT DEFAULT ''",
        # Supporting tables
        """CREATE TABLE IF NOT EXISTS pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(dedup_key),
            from_status TEXT,
            to_status TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            evidence TEXT DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS email_parse_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            error TEXT DEFAULT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS resume_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            model TEXT NOT NULL,
            doc_url TEXT DEFAULT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL
        )""",
        # Indexes for query performance (IF NOT EXISTS -- safe to re-run)
        "CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_status ON jobs(pipeline_status)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_events_job_id ON pipeline_events(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_email_parse_log_message_id ON email_parse_log(message_id)",
    ],
)
