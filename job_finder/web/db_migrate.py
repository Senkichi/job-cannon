"""Schema migration runner for job-finder SQLite database.

Uses PRAGMA user_version to track migration state. Safe to call on every
startup -- idempotent by design.

Pattern: Each migration is a list of SQL statements. WAL PRAGMA statements
are run via execute() (PRAGMA needs its own transaction). DDL statements
are run individually so that "duplicate column name" errors can be caught
per-statement without aborting the whole migration.
"""

import logging
import sqlite3

from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Each migration is a list of SQL statement strings.
# Storing as discrete strings avoids semicolon-splitting hazards in comments.
MIGRATIONS = [
    # Migration 1: Create/extend jobs table, create supporting tables, add indexes.
    [
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

    # Migration 2: Add AI scoring columns and indexes.
    [
        "ALTER TABLE jobs ADD COLUMN haiku_score REAL DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN haiku_summary TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN sonnet_score REAL DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN fit_analysis TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN jd_full TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN is_stale INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_jobs_haiku_score ON jobs(haiku_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_is_stale ON jobs(is_stale)",
    ],

    # Migration 3: Add pipeline_detections table for the review queue and
    # auto-update log. Each row tracks one Gmail message and its classification.
    [
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

    # Migration 4: Extend resume_generations table with status tracking columns
    # for async generation workflow. Adds indexes for common query patterns.
    [
        # Add status tracking columns; defaults ensure legacy rows remain valid.
        "ALTER TABLE resume_generations ADD COLUMN generation_type TEXT DEFAULT 'single'",
        "ALTER TABLE resume_generations ADD COLUMN status TEXT DEFAULT 'done'",
        "ALTER TABLE resume_generations ADD COLUMN strategy TEXT DEFAULT NULL",
        "ALTER TABLE resume_generations ADD COLUMN error_msg TEXT DEFAULT NULL",
        # Indexes for resume generation queries
        "CREATE INDEX IF NOT EXISTS idx_resume_generations_job_id ON resume_generations(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_resume_generations_status ON resume_generations(status)",
    ],

    # Migration 5: Add Phase 5 Intelligence tables — interview prep, resume
    # preferences feedback loop, rejection analysis reports — plus new columns
    # on existing tables for rejection review tracking and Drive poll timestamps.
    [
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

    # Migration 6: Phase 6 Data Quality — batch score session tracking, merge log
    # for smart deduplication, and new job columns for location normalization and
    # description reformatting. Also fixes first_seen timestamps retroactively.
    [
        # batch_score_sessions: tracks async batch scoring runs (for progress UI)
        """CREATE TABLE IF NOT EXISTS batch_score_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            scored INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT NULL,
            error_msg TEXT DEFAULT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_batch_score_sessions_status ON batch_score_sessions(status)",

        # merge_log: audit trail for deduplication merges
        """CREATE TABLE IF NOT EXISTS merge_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL,
            merged_key TEXT NOT NULL,
            merge_source TEXT NOT NULL DEFAULT 'migration',
            merged_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_merge_log_canonical ON merge_log(canonical_key)",

        # New columns on jobs for Phase 6 enrichment
        "ALTER TABLE jobs ADD COLUMN locations_raw TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN description_reformatted INTEGER DEFAULT 0",

        # Retroactive first_seen fix: update Gmail jobs' first_seen to the earliest
        # email_parse_log timestamp where the date matches (best-effort; per Research
        # Pitfall 5, matching is by date proximity since email_parse_log uses run-level
        # IDs not per-message IDs). SerpAPI jobs have no email date and are unaffected.
        """UPDATE jobs SET first_seen = (
            SELECT MIN(epl.processed_at)
            FROM email_parse_log epl
            WHERE DATE(epl.processed_at) = DATE(jobs.first_seen)
        ) WHERE EXISTS (
            SELECT 1 FROM email_parse_log epl
            WHERE DATE(epl.processed_at) = DATE(jobs.first_seen)
        )""",
    ],

    # Migration 7: Phase 7 Company Tracking & ATS Discovery — companies registry,
    # company_scan_log for scan history, and jobs.company_id FK to link jobs to
    # their company record. Enables proactive ATS discovery via Lever/Greenhouse/Ashby.
    [
        # companies: one row per tracked company with ATS probe state
        """CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL,
            homepage_url TEXT DEFAULT NULL,
            ats_platform TEXT DEFAULT NULL,
            ats_slug TEXT DEFAULT NULL,
            ats_probe_status TEXT DEFAULT 'pending',
            ats_probe_attempted_at TEXT DEFAULT NULL,
            scan_enabled INTEGER DEFAULT 1,
            last_scanned_at TEXT DEFAULT NULL,
            jobs_found_total INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",

        # company_scan_log: scan history with FK to companies
        """CREATE TABLE IF NOT EXISTS company_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            scanned_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            error TEXT DEFAULT NULL
        )""",

        # jobs.company_id FK to link jobs to their company record
        "ALTER TABLE jobs ADD COLUMN company_id INTEGER DEFAULT NULL",

        # jobs.comp_data_json stores ATS compensation data (equity, bonus, benefits)
        # as JSON from Ashby/Lever probes for Haiku compensation context scoring.
        "ALTER TABLE jobs ADD COLUMN comp_data_json TEXT DEFAULT NULL",

        # Indexes for companies queries
        "CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name)",
        "CREATE INDEX IF NOT EXISTS idx_companies_ats_platform ON companies(ats_platform)",
        "CREATE INDEX IF NOT EXISTS idx_companies_ats_probe_status ON companies(ats_probe_status)",
        "CREATE INDEX IF NOT EXISTS idx_companies_scan_enabled ON companies(scan_enabled)",
        "CREATE INDEX IF NOT EXISTS idx_company_scan_log_company_id ON company_scan_log(company_id)",
    ],

    # Migration 8: Phase 10 Cost-Optimized Enrichment — enrichment_tier column
    # tracks the highest enrichment tier attempted per job. Values: 'free', 'ddg',
    # 'haiku', 'serpapi', 'sonnet', 'exhausted'. Existing enriched jobs are marked
    # 'serpapi' (the highest tier that could have run before this phase). Practical
    # impact is zero: those jobs already have jd_full and won't be re-enriched.
    [
        "ALTER TABLE jobs ADD COLUMN enrichment_tier TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_enrichment_tier ON jobs(enrichment_tier)",
        "UPDATE jobs SET enrichment_tier = 'serpapi' WHERE jd_full IS NOT NULL AND enrichment_tier IS NULL",
    ],

    # Migration 9: user_activity table — INST-01 (Phase 16).
    # Stores user action events for activity analytics and audit trails.
    [
        """CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_id TEXT DEFAULT NULL,
            metadata TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_user_activity_action ON user_activity(action)",
        "CREATE INDEX IF NOT EXISTS idx_user_activity_occurred_at ON user_activity(occurred_at DESC)",
    ],

    # Migration 10: ATS retry columns — DEBT-01 (Phase 14).
    # Supports exponential backoff retry tracking for ATS probe failures.
    [
        "ALTER TABLE jobs ADD COLUMN ats_retry_count INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN ats_last_error TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN ats_retry_after TEXT DEFAULT NULL",
    ],

    # Migration 11: resume_upload_reviews table — RESUME-01 (Phase 17).
    # Stores uploaded resume PDF metadata and review pipeline state.
    [
        """CREATE TABLE IF NOT EXISTS resume_upload_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending'
        )""",
    ],

    # Migration 12: ATS retry columns on companies table — DEBT-01 (Phase 14).
    # Supports exponential backoff retry tracking for transient ATS probe failures.
    # Separate from Migration 10 which incorrectly added these to the jobs table.
    [
        "ALTER TABLE companies ADD COLUMN retry_count INTEGER DEFAULT 0",
        "ALTER TABLE companies ADD COLUMN retry_after TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN miss_reason TEXT DEFAULT NULL",
    ],

    # Migration 13: Drop dead ATS retry columns from jobs table — Phase 19 cleanup.
    # Migration 10 (Phase 14) added ats_retry_count/ats_last_error/ats_retry_after to jobs in error.
    # The correct columns (retry_count/retry_after/miss_reason) were added to companies in Migration 12.
    # These dead columns are unused in all production code paths and safe to remove.
    # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; confirmed 3.49.1 in this environment.
    [
        "ALTER TABLE jobs DROP COLUMN ats_retry_count",
        "ALTER TABLE jobs DROP COLUMN ats_last_error",
        "ALTER TABLE jobs DROP COLUMN ats_retry_after",
    ],

    # Migration 14: Phase 30 Infrastructure — expiry tracking + validation report storage.
    # Serves both Phase 31 (expiry detection) and Phase 32 (resume quality).
    [
        "ALTER TABLE jobs ADD COLUMN expiry_checked_at TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_expiry_checked_at ON jobs(expiry_checked_at)",
        "ALTER TABLE resume_generations ADD COLUMN validation_report TEXT DEFAULT NULL",
    ],

    # Migration 15: Phase 40 Data Quality — clean poison jd_full values, delete garbage
    # notification rows, and promote long descriptions to jd_full. Three data fixes:
    #
    # Fix A: LinkedIn login page text stored as jd_full (~dozens of rows).
    #   Null out jd_full and reset enrichment_tier to 'ddg' so re-enrichment
    #   resumes from Haiku tier (skips free tier which would hit login wall again).
    #
    # Fix B: Garbage job rows with notification text in title (3 rows).
    #   Delete outright — these are not real jobs.
    #
    # Fix C: Long descriptions (>200 chars) never promoted to jd_full (99 rows).
    #   Copy description to jd_full where jd_full IS NULL. This surfaces existing
    #   good descriptions to Sonnet without re-fetching.
    [
        # Fix A: Null out LinkedIn login page jd_full values
        "UPDATE jobs SET jd_full = NULL, enrichment_tier = 'ddg' WHERE jd_full LIKE '%signing you in%' OR jd_full LIKE '%sign in or join%'",

        # Fix B: Delete garbage notification rows
        "DELETE FROM jobs WHERE title LIKE '%receive notifications%'",

        # Fix C: Promote long descriptions to jd_full
        "UPDATE jobs SET jd_full = SUBSTR(description, 1, 8000) WHERE jd_full IS NULL AND description IS NOT NULL AND LENGTH(description) > 200",
    ],

    # Migration 16: Add company enrichment columns to companies table.
    # enrich_company_info() returns company_size and industry from DuckDuckGo —
    # previously discarded; now persisted for scoring context and UI display.
    [
        "ALTER TABLE companies ADD COLUMN company_size TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN industry TEXT DEFAULT NULL",
    ],

    # Migration 17: Add homepage_probe_attempted_at column to companies table.
    # Enables retry-avoidance in run_homepage_discovery — companies already
    # attempted (whether found or not) are skipped on subsequent runs.
    [
        "ALTER TABLE companies ADD COLUMN homepage_probe_attempted_at TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_companies_homepage_probe_attempted_at ON companies(homepage_probe_attempted_at)",
    ],

    # Migration 18: Add provider column to scoring_costs table.
    # Tracks which provider (anthropic, gemini, ollama) handled each API call.
    # Default 'anthropic' for all existing rows (pre-multi-provider).
    [
        "ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic'",
    ],

    # Migration 19: Add opus_score column for Opus baseline evaluation.
    # Stores Opus-generated scores as gold-standard baseline for model comparison.
    [
        "ALTER TABLE jobs ADD COLUMN opus_score REAL DEFAULT NULL",
    ],

    # Migration 20: Provider attribution for Sonnet scoring (ATTR-01).
    # Records which provider produced each Sonnet score.
    # Default 'anthropic' for all existing rows (pre-cascade).
    [
        "ALTER TABLE jobs ADD COLUMN scoring_provider TEXT DEFAULT 'anthropic'",
    ],

    # Migration 21: Clean up stub JDs — title restatements from AI enrichment.
    # When all free tiers failed, AI extraction echoed the title as jd_full
    # (e.g., "Data Manager at Mochi Health" for a Data Manager role).
    # NULL out these stubs and reset enrichment_tier so jobs can be re-enriched.
    # Also NULL out sonnet_score/fit_analysis for stubs that were scored with garbage input.
    [
        """UPDATE jobs
           SET jd_full = NULL,
               enrichment_tier = NULL
           WHERE jd_full IS NOT NULL
             AND LENGTH(jd_full) < 200
             AND enrichment_tier IN ('haiku', 'sonnet', 'exhausted')""",
        """UPDATE jobs
           SET sonnet_score = NULL,
               fit_analysis = NULL
           WHERE jd_full IS NULL
             AND sonnet_score IS NOT NULL""",
    ],

    # Migration 22: Clean up chrome-polluted JDs — scraped website chrome, LinkedIn
    # login walls, company overview pages, and search result pages stored as jd_full.
    # These pass length checks but contain no usable job description content.
    # NULL out jd_full + scores and reset enrichment_tier for re-enrichment.
    [
        # LinkedIn login wall pages (most common: 151 jobs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (jd_full LIKE '%Agree & Join LinkedIn%'
               OR jd_full LIKE '%Join or sign in to find your next job%'
               OR jd_full LIKE '%Join to apply for the%')""",
        # Cookie banners in first 300 chars of jd_full
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (SUBSTR(jd_full, 1, 300) LIKE '%cookie%'
               OR SUBSTR(jd_full, 1, 300) LIKE '%Close this dialog%'
               OR SUBSTR(jd_full, 1, 300) LIKE '%third-party partners%')""",
        # Built In company overview pages (not JDs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (jd_full LIKE '%View All Jobs at%'
               OR jd_full LIKE '%Recently Posted Jobs at%'
               OR jd_full LIKE '%Similar Companies Hiring%')""",
        # LinkedIn search results pages (not individual JDs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND jd_full LIKE '%Past month%Past week%Past 24 hours%'""",
    ],

    # Migration 23: Recalibrate jobs_found_total from cumulative to current count
    # and add jobs_matched column to company_scan_log.
    [
        """UPDATE companies SET jobs_found_total = (
            SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
        )""",
        "ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER DEFAULT NULL",
    ],

    # Migration 24: Add composite index on email_parse_log(sender, processed_at)
    # to support the per-message Gmail dedup query:
    #   WHERE sender = 'gmail' AND processed_at >= datetime('now', ?)
    # The composite covers both filter columns; single-column (processed_at)
    # would scan all senders unnecessarily as the table grows.
    [
        "CREATE INDEX IF NOT EXISTS idx_email_parse_log_sender_processed_at"
        " ON email_parse_log(sender, processed_at)",
    ],
]


def run_migrations(db_path: str) -> None:
    """Run pending migrations against the given SQLite database.

    Idempotent -- safe to call on every application startup. Uses
    PRAGMA user_version to track which migrations have been applied.

    After Migration 6 completes (or if it was already applied), runs the
    retroactive deduplication merge once. A sentinel row in merge_log
    (merge_source='migration_complete') tracks that this has run so that
    subsequent startups skip the one-time operation.

    Args:
        db_path: Path to the SQLite database file.
    """
    with standalone_connection(db_path) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        pending = MIGRATIONS[current_version:]

        for i, statements in enumerate(pending, start=current_version + 1):
            _apply_migration(conn, i, statements)

        # Run retroactive dedup once after Migration 6 or later is present.
        # Sentinel row prevents re-running on subsequent startups.
        final_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if final_version >= 6:
            _run_retroactive_dedup_once(conn)

        # Fixup: ensure comp_data_json column exists (missed in original Migration 7).
        # Required for databases that ran Migration 7 before this column was added —
        # those DBs already have user_version=7 so the migration loop won't re-apply.
        if final_version >= 7:
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN comp_data_json TEXT DEFAULT NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — expected on fresh DBs

        # Fixup: ensure homepage_probe_attempted_at column exists on companies table.
        # Migration 17 added this column, but DBs that reached user_version=23 via a
        # path where Migration 17 was inserted after the fact never had it applied —
        # the migration loop skips entries at indices below current_version.
        if final_version >= 17:
            try:
                conn.execute(
                    "ALTER TABLE companies ADD COLUMN homepage_probe_attempted_at TEXT DEFAULT NULL"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_companies_homepage_probe_attempted_at"
                    " ON companies(homepage_probe_attempted_at)"
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists — expected on fresh DBs


def _run_retroactive_dedup_once(conn: sqlite3.Connection) -> None:
    """Run retroactive dedup merge exactly once (guarded by sentinel in merge_log).

    Checks for a sentinel row with merge_source='migration_complete'. If not
    found, runs run_retroactive_dedup, inserts the sentinel, and logs the result.
    Inserts a runs table entry for activity feed visibility.

    Args:
        conn: Open SQLite connection (must have migration 6 applied).
    """
    try:
        sentinel = conn.execute(
            "SELECT id FROM merge_log WHERE merge_source = 'migration_complete' LIMIT 1"
        ).fetchone()
        if sentinel is not None:
            return  # Already ran -- skip

        # Import here to avoid circular import at module load time
        from datetime import datetime as _dt
        from job_finder.web.dedup_normalizer import run_retroactive_dedup

        merged_count = run_retroactive_dedup(conn)
        now_iso = _dt.now().isoformat()

        # Insert sentinel row to mark completion
        conn.execute("""
            INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at)
            VALUES ('__sentinel__', '__sentinel__', 'migration_complete', ?)
        """, (now_iso,))
        conn.commit()

        if merged_count > 0:
            # Add activity feed entry so the user sees the merge count
            try:
                conn.execute("""
                    INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                    VALUES (?, 'dedup_migration', ?, 0, 0)
                """, (now_iso, merged_count))
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log dedup migration run: %s", e)

            print(f"[db_migrate] Retroactive dedup: merged {merged_count} duplicate jobs.")

            # Queue merged canonical rows for re-scoring (nullify AI scores)
            try:
                canonical_keys = conn.execute(
                    "SELECT canonical_key FROM merge_log WHERE merge_source = 'migration'"
                ).fetchall()
                for row in canonical_keys:
                    conn.execute("""
                        UPDATE jobs SET haiku_score = NULL, sonnet_score = NULL, fit_analysis = NULL
                        WHERE dedup_key = ?
                    """, (row[0],))
                conn.commit()
            except Exception as e:
                logger.warning("Failed to queue merged rows for re-scoring: %s", e)
        else:
            print("[db_migrate] Retroactive dedup: no duplicates found.")

    except Exception as e:
        logger.warning("Retroactive dedup failed (non-fatal): %s", e)


def _apply_migration(
    conn: sqlite3.Connection, version: int, statements: list
) -> None:
    """Apply a single migration (list of SQL statements) and update user_version.

    Each statement is executed individually so that "duplicate column name"
    errors from ALTER TABLE ADD COLUMN can be caught and skipped without
    aborting the rest of the migration. This enables idempotent re-runs.

    PRAGMA statements are handled the same way as DDL -- execute() in its own
    commit so that journal_mode=WAL takes effect immediately.

    Args:
        conn: Open SQLite connection.
        version: The migration version number (1-based).
        statements: List of SQL statement strings for this migration.
    """
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "duplicate column name" in error_msg:
                # Column already exists -- safe to skip for idempotent re-runs
                continue
            else:
                raise

    # Commit once per migration (not per statement)
    conn.commit()

    # Update version counter after all statements succeed
    assert isinstance(version, int), f"Migration version must be int, got {type(version)}"
    conn.execute("PRAGMA user_version = " + str(int(version)))
    conn.commit()
    print(f"[db_migrate] Migration {version} applied successfully.")
