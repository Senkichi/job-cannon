"""Schema migration runner for job-finder SQLite database.

Uses PRAGMA user_version to track migration state. Safe to call on every
startup -- idempotent by design.

Each migration is a `Migration` value object (`job_finder.web.migrations.types`)
with an explicit version number, a short description, an ordered list of SQL
statements, and an optional Python helper. WAL PRAGMAs are run via
`Connection.execute` (PRAGMA needs its own transaction). DDL statements are
run individually so that "duplicate column name" / "no such column" errors
can be swallowed per-statement, enabling idempotent re-runs.
"""

import glob
import logging
import os
import sqlite3
import time

from job_finder.web.db_helpers import standalone_connection
from job_finder.web.migrations import Migration, MigrationContext

logger = logging.getLogger(__name__)


class MigrationBlockedError(Exception):
    """Raised by a migration's preflight gate to block destructive schema changes.

    Currently raised by Migration 41 when the backup-recency check fails
    (no recent backup tarball AND GSD_BACKUP_CONFIRMED=1 not set). Callers
    should present the message to the operator and halt; the migration
    will not have mutated any schema or data before the raise.
    """


def _check_backup_recent(user_data_root: str | None = None) -> None:
    """Preflight gate for Migration 41: require a recent backup OR explicit override.

    Looks for backup_userdata_*.tar.gz files under `user_data_root` (defaults
    to CWD when None). Raises MigrationBlockedError when:
      - No matching backup is found, AND GSD_BACKUP_CONFIRMED != "1"
      - The newest backup is older than 24h, AND GSD_BACKUP_CONFIRMED != "1"

    The env var override exists so operators who use alternate backup schemes
    (time-machine snapshots, zfs datasets, manual .backup copies) can proceed
    after accepting responsibility for the rollback path. Fail-closed default.
    """
    if os.environ.get("GSD_BACKUP_CONFIRMED") == "1":
        return
    root = user_data_root if user_data_root is not None else os.getcwd()
    pattern = os.path.join(root, "backup_userdata_*.tar.gz")
    backups = sorted(glob.glob(pattern), reverse=True)
    if not backups:
        raise MigrationBlockedError(
            "Migration 41 blocked: no backup_userdata_*.tar.gz found in cwd. "
            "Run `bash backup_userdata.sh` first, or set GSD_BACKUP_CONFIRMED=1 "
            "to override (only if you have an alternate backup)."
        )
    age_h = (time.time() - os.path.getmtime(backups[0])) / 3600.0
    if age_h > 24.0:
        raise MigrationBlockedError(
            f"Migration 41 blocked: most recent backup ({backups[0]}) is "
            f"{age_h:.1f}h old (>24h). Run `bash backup_userdata.sh`, or set "
            f"GSD_BACKUP_CONFIRMED=1 to override."
        )


def _migration_41_drop_legacy_scores(ctx: MigrationContext) -> None:
    """Migration 41: drop legacy haiku_score/haiku_summary/sonnet_score columns.

    Preflight: backup-recency gate (see _check_backup_recent).

    Drops:
        - haiku_score, haiku_summary, sonnet_score columns
        - idx_jobs_haiku_score index

    Preserves:
        - fit_analysis (now holds v3.0 rationale payload)
        - scoring_provider, scoring_model
        - eval_blocks, opus_score, score, job_archetype, legitimacy_note
        - classification, sub_scores_json (v3 scoring surface from Mig 40)

    No inline rollback -- recovery path is a DB restore from the gated backup.
    Idempotent via "no such column" handling in _apply_migration.
    """
    _check_backup_recent(ctx.user_data_root)
    ctx.conn.execute("DROP INDEX IF EXISTS idx_jobs_haiku_score")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN haiku_score")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN haiku_summary")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN sonnet_score")


# fmt: off
MIGRATIONS: list[Migration] = [
    Migration(
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
    ),
    Migration(
        version=2,
        description="AI scoring columns: haiku/sonnet scores, jd_full, fit_analysis, is_stale",
        sql=[
            "ALTER TABLE jobs ADD COLUMN haiku_score REAL DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN haiku_summary TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN sonnet_score REAL DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN fit_analysis TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN jd_full TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN is_stale INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_jobs_haiku_score ON jobs(haiku_score DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_is_stale ON jobs(is_stale)",
        ],
    ),
    Migration(
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
    ),
    Migration(
        version=4,
        description="resume_generations status tracking columns + indexes for async workflow",
        sql=[
            # Add status tracking columns; defaults ensure legacy rows remain valid.
            "ALTER TABLE resume_generations ADD COLUMN generation_type TEXT DEFAULT 'single'",
            "ALTER TABLE resume_generations ADD COLUMN status TEXT DEFAULT 'done'",
            "ALTER TABLE resume_generations ADD COLUMN strategy TEXT DEFAULT NULL",
            "ALTER TABLE resume_generations ADD COLUMN error_msg TEXT DEFAULT NULL",
            # Indexes for resume generation queries
            "CREATE INDEX IF NOT EXISTS idx_resume_generations_job_id ON resume_generations(job_id)",
            "CREATE INDEX IF NOT EXISTS idx_resume_generations_status ON resume_generations(status)",
        ],
    ),
    Migration(
        version=5,
        description="Phase 5 Intelligence: interview_preps, resume_preferences_detected, rejection_reports + jobs.rejection_reviewed + last_drive_polled_at",
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
    ),
    Migration(
        version=6,
        description="Phase 6 data quality: batch_score_sessions, merge_log, locations_raw, retroactive first_seen fix",
        sql=[
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
    ),
    Migration(
        version=7,
        description="Phase 7 companies & ATS discovery: companies, company_scan_log, jobs.company_id, jobs.comp_data_json",
        sql=[
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
    ),
    Migration(
        version=8,
        description="Phase 10 cost-optimized enrichment: enrichment_tier column + index + backfill",
        sql=[
            "ALTER TABLE jobs ADD COLUMN enrichment_tier TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_jobs_enrichment_tier ON jobs(enrichment_tier)",
            "UPDATE jobs SET enrichment_tier = 'serpapi' WHERE jd_full IS NOT NULL AND enrichment_tier IS NULL",
        ],
    ),
    Migration(
        version=9,
        description="user_activity table for activity analytics and audit trails (INST-01, Phase 16)",
        sql=[
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
    ),
    Migration(
        version=10,
        description="ATS retry columns on jobs (DEBT-01, Phase 14) — later dropped by Migration 13",
        sql=[
            "ALTER TABLE jobs ADD COLUMN ats_retry_count INTEGER DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN ats_last_error TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN ats_retry_after TEXT DEFAULT NULL",
        ],
    ),
    Migration(
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
    ),
    Migration(
        version=12,
        description="ATS retry columns on companies — fix for Mig 10 which mistakenly added them to jobs",
        sql=[
            "ALTER TABLE companies ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE companies ADD COLUMN retry_after TEXT DEFAULT NULL",
            "ALTER TABLE companies ADD COLUMN miss_reason TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=13,
        description="drop dead ATS retry columns from jobs (Phase 19 cleanup of Mig 10's mistake)",
        sql=[
            # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; confirmed 3.49.1 in this environment.
            "ALTER TABLE jobs DROP COLUMN ats_retry_count",
            "ALTER TABLE jobs DROP COLUMN ats_last_error",
            "ALTER TABLE jobs DROP COLUMN ats_retry_after",
        ],
    ),
    Migration(
        version=14,
        description="Phase 30 infrastructure: jobs.expiry_checked_at + index, resume_generations.validation_report",
        sql=[
            "ALTER TABLE jobs ADD COLUMN expiry_checked_at TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_jobs_expiry_checked_at ON jobs(expiry_checked_at)",
            "ALTER TABLE resume_generations ADD COLUMN validation_report TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=15,
        description="Phase 40 data quality: clean LinkedIn login pages, garbage notification rows, promote long descriptions to jd_full",
        sql=[
            # Fix A: Null out LinkedIn login page jd_full values
            "UPDATE jobs SET jd_full = NULL, enrichment_tier = 'ddg' WHERE jd_full LIKE '%signing you in%' OR jd_full LIKE '%sign in or join%'",
            # Fix B: Delete garbage notification rows
            "DELETE FROM jobs WHERE title LIKE '%receive notifications%'",
            # Fix C: Promote long descriptions to jd_full
            "UPDATE jobs SET jd_full = SUBSTR(description, 1, 8000) WHERE jd_full IS NULL AND description IS NOT NULL AND LENGTH(description) > 200",
        ],
    ),
    Migration(
        version=16,
        description="companies.company_size + .industry from DDG enrichment",
        sql=[
            "ALTER TABLE companies ADD COLUMN company_size TEXT DEFAULT NULL",
            "ALTER TABLE companies ADD COLUMN industry TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=17,
        description="companies.homepage_probe_attempted_at for retry-avoidance in homepage discovery",
        sql=[
            "ALTER TABLE companies ADD COLUMN homepage_probe_attempted_at TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_companies_homepage_probe_attempted_at ON companies(homepage_probe_attempted_at)",
        ],
    ),
    Migration(
        version=18,
        description="scoring_costs.provider for multi-provider tracking (default 'anthropic')",
        sql=[
            "ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic'",
        ],
    ),
    Migration(
        version=19,
        description="jobs.opus_score for gold-standard baseline evaluation",
        sql=[
            "ALTER TABLE jobs ADD COLUMN opus_score REAL DEFAULT NULL",
        ],
    ),
    Migration(
        version=20,
        description="jobs.scoring_provider for Sonnet-scoring provider attribution (ATTR-01)",
        sql=[
            "ALTER TABLE jobs ADD COLUMN scoring_provider TEXT DEFAULT 'anthropic'",
        ],
    ),
    Migration(
        version=21,
        description="clean stub jd_full title-restatements; null out scores for jobs whose JD is now NULL",
        sql=[
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
    ),
    Migration(
        version=22,
        description="clean chrome-polluted jd_full: LinkedIn walls, cookie banners, Built-In overviews, search results",
        sql=[
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
    ),
    Migration(
        version=23,
        description="recalibrate companies.jobs_found_total from cumulative to current count + company_scan_log.jobs_matched",
        sql=[
            """UPDATE companies SET jobs_found_total = (
                SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
            )""",
            "ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER DEFAULT NULL",
        ],
    ),
    Migration(
        version=24,
        description="composite index on email_parse_log(sender, processed_at) for per-message Gmail dedup query",
        sql=[
            "CREATE INDEX IF NOT EXISTS idx_email_parse_log_sender_processed_at"
            " ON email_parse_log(sender, processed_at)",
        ],
    ),
    Migration(
        version=25,
        description="clean Eightfold/Phenom PCS SPA shell garbage in jd_full (themeOptions JSON)",
        sql=[
            # Fix A: exhausted-tier jobs — agentic enricher picks up jd_full IS NULL
            """UPDATE jobs
               SET jd_full = NULL,
                   sonnet_score = NULL,
                   fit_analysis = NULL
               WHERE jd_full LIKE '%"themeOptions"%'
                 AND enrichment_tier = 'exhausted'""",
            # Fix B: non-exhausted jobs — reset to re-run free tier cleanly
            """UPDATE jobs
               SET jd_full = NULL,
                   enrichment_tier = NULL,
                   sonnet_score = NULL,
                   fit_analysis = NULL
               WHERE jd_full LIKE '%"themeOptions"%'
                 AND (enrichment_tier IS NULL OR enrichment_tier != 'exhausted')""",
        ],
    ),
    Migration(
        version=26,
        description="enrichment retry metadata on companies + last_scanned_at index",
        sql=[
            "ALTER TABLE companies ADD COLUMN enrichment_attempts INTEGER DEFAULT 0",
            "ALTER TABLE companies ADD COLUMN enrichment_last_attempted_at TEXT DEFAULT NULL",
            "ALTER TABLE companies ADD COLUMN enrichment_backoff_until TEXT DEFAULT NULL",
            "ALTER TABLE companies ADD COLUMN enrichment_last_error TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_companies_last_scanned_at ON companies(last_scanned_at)",
        ],
    ),
    Migration(
        version=27,
        description="career-ops scoring metadata: jobs.expiry_status, .eval_blocks, .job_archetype",
        sql=[
            "ALTER TABLE jobs ADD COLUMN expiry_status TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN eval_blocks TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN job_archetype TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=28,
        description="interview_preps.reusable_stories_json for STAR-story reuse across applications",
        sql=[
            "ALTER TABLE interview_preps ADD COLUMN reusable_stories_json TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=29,
        description="company_research async-state table for on-demand company research",
        sql=[
            """CREATE TABLE IF NOT EXISTS company_research (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL REFERENCES companies(id),
                status TEXT NOT NULL DEFAULT 'pending',
                research_json TEXT DEFAULT NULL,
                error_msg TEXT DEFAULT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT DEFAULT NULL,
                cost_usd REAL DEFAULT 0.0
            )""",
            "CREATE INDEX IF NOT EXISTS idx_company_research_company_id ON company_research(company_id)",
        ],
    ),
    Migration(
        version=30,
        description="companies.careers_url cache for find_careers_url() result",
        sql=[
            "ALTER TABLE companies ADD COLUMN careers_url TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=31,
        description="companies.careers_crawl_last_at for freshness-based crawler rotation",
        sql=[
            "ALTER TABLE companies ADD COLUMN careers_crawl_last_at TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=32,
        description="jobs.legitimacy_note for ghost-job detection signals",
        sql=[
            "ALTER TABLE jobs ADD COLUMN legitimacy_note TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=33,
        description="liveness checker columns on jobs (later dropped by Mig 39)",
        sql=[
            "ALTER TABLE jobs ADD COLUMN liveness_checked_at TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN liveness_status TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN liveness_reason TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_jobs_liveness ON jobs(liveness_checked_at, pipeline_status)",
        ],
    ),
    Migration(
        version=34,
        description="rejection_pattern_reports table for zero-LLM-cost mechanical analysis",
        sql=[
            """CREATE TABLE IF NOT EXISTS rejection_pattern_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_json TEXT NOT NULL,
                period_days INTEGER NOT NULL,
                total_rejections INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )""",
        ],
    ),
    Migration(
        version=35,
        description="companies.careers_api_endpoint cache for direct HTTP crawls (skip Playwright)",
        sql=[
            "ALTER TABLE companies ADD COLUMN careers_api_endpoint TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=36,
        description="companies.careers_crawl_tier — last successful extraction tier (static/url_param/playwright/api_cached)",
        sql=[
            "ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=37,
        description="companies.careers_nav_recipe — Haiku-discovered navigation recipe for cached AI replays",
        sql=[
            "ALTER TABLE companies ADD COLUMN careers_nav_recipe TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=38,
        description="dashboard performance: indexes for scoring_costs, pipeline_events, pipeline_detections, company_scan_log, jobs.first_seen",
        sql=[
            "CREATE INDEX IF NOT EXISTS idx_scoring_costs_timestamp ON scoring_costs(timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pipeline_events_timestamp ON pipeline_events(timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pipeline_detections_created_at ON pipeline_detections(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_company_scan_log_scanned_at ON company_scan_log(scanned_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen DESC)",
        ],
    ),
    Migration(
        version=39,
        description="drop dead liveness_* columns from jobs (functionality merged into expiry_checker)",
        sql=[
            "DROP INDEX IF EXISTS idx_jobs_liveness",
            "ALTER TABLE jobs DROP COLUMN liveness_checked_at",
            "ALTER TABLE jobs DROP COLUMN liveness_status",
            "ALTER TABLE jobs DROP COLUMN liveness_reason",
        ],
    ),
    Migration(
        version=40,
        description="v3.0 ordinal rubric scoring: jobs.classification, .sub_scores_json, .scoring_model + index",
        sql=[
            "ALTER TABLE jobs ADD COLUMN classification TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN sub_scores_json TEXT DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN scoring_model TEXT DEFAULT NULL",
            "CREATE INDEX IF NOT EXISTS idx_jobs_classification ON jobs(classification)",
        ],
    ),
    Migration(
        version=41,
        description="drop legacy haiku_score/haiku_summary/sonnet_score after backup-recency gate",
        py=_migration_41_drop_legacy_scores,
    ),
    Migration(
        version=42,
        description="extend classification enum vocabulary to include 'low_signal' (no-op DDL — column has no CHECK)",
        sql=["SELECT 1"],
    ),
    Migration(
        version=43,
        description="gold-set labeling columns on jobs: gold_classification (CHECK), gold_sub_scores_json, gold_notes, gold_labeled_at",
        sql=[
            """ALTER TABLE jobs ADD COLUMN gold_classification TEXT
               CHECK (gold_classification IS NULL
                      OR gold_classification IN ('apply', 'consider', 'skip', 'reject', 'low_signal'))""",
            "ALTER TABLE jobs ADD COLUMN gold_sub_scores_json TEXT",
            "ALTER TABLE jobs ADD COLUMN gold_notes TEXT",
            "ALTER TABLE jobs ADD COLUMN gold_labeled_at TIMESTAMP",
        ],
    ),
    Migration(
        version=44,
        description="jobs.gold_no_signal_axes for per-axis 'no signal' tagging on gold labels",
        sql=["ALTER TABLE jobs ADD COLUMN gold_no_signal_axes TEXT"],
    ),
    Migration(
        version=45,
        description="eval_runs table for Phase 5 harness run history",
        sql=[
            """CREATE TABLE IF NOT EXISTS eval_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                variant_name TEXT NOT NULL,
                baseline_run_id TEXT,
                gold_set_version TEXT NOT NULL,
                n_runs INTEGER NOT NULL,
                config_json TEXT,
                metrics_json TEXT NOT NULL,
                per_job_json TEXT NOT NULL,
                report_path TEXT,
                notes TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_eval_runs_variant ON eval_runs(variant_name)",
            "CREATE INDEX IF NOT EXISTS idx_eval_runs_ts ON eval_runs(timestamp DESC)",
        ],
    ),
    Migration(
        version=46,
        description="heal Workday URL-template bug fallout: repair source_urls, reset enrichment, drop bogus scores",
        sql=[
            # 1. Fix the malformed source URLs so future fetches hit the right slot
            """UPDATE jobs
                  SET source_urls = REPLACE(source_urls, '/job//job/', '/job/')
                WHERE source_urls LIKE '%/job//job/%'""",
            # 2. Reset jd_full + enrichment_tier on Workday rows that captured "Workday"
            """UPDATE jobs
                  SET jd_full = NULL,
                      enrichment_tier = NULL
                WHERE TRIM(jd_full) = 'Workday'""",
            # 3. Drop classification + sub_scores derived from the corrupt jd_full so
            #    the next batch scoring run re-classifies these rows from scratch
            """UPDATE jobs
                  SET classification = NULL,
                      sub_scores_json = NULL,
                      fit_analysis = NULL,
                      scoring_provider = NULL,
                      scoring_model = NULL
                WHERE jd_full IS NULL
                  AND classification IS NOT NULL
                  AND sources LIKE '%Workday%'""",
        ],
    ),
    Migration(
        version=47,
        description="public-repo cleanup: drop resume_generations / resume_preferences_detected / resume_upload_reviews",
        sql=[
            "DROP INDEX IF EXISTS idx_resume_generations_job_id",
            "DROP INDEX IF EXISTS idx_resume_generations_status",
            "DROP INDEX IF EXISTS idx_prefs_detected_job_id",
            "DROP INDEX IF EXISTS idx_prefs_detected_accepted",
            "DROP TABLE IF EXISTS resume_generations",
            "DROP TABLE IF EXISTS resume_preferences_detected",
            "DROP TABLE IF EXISTS resume_upload_reviews",
        ],
    ),
    Migration(
        version=48,
        description="public-repo cleanup: drop interview_preps / rejection_reports / rejection_pattern_reports + jobs.rejection_reviewed",
        sql=[
            "DROP INDEX IF EXISTS idx_interview_preps_job_id",
            "DROP TABLE IF EXISTS interview_preps",
            "DROP TABLE IF EXISTS rejection_reports",
            "DROP TABLE IF EXISTS rejection_pattern_reports",
            "ALTER TABLE jobs DROP COLUMN rejection_reviewed",
        ],
    ),
]
# fmt: on


def run_migrations(db_path: str, user_data_root: str | None = None) -> None:
    """Run pending migrations against the given SQLite database.

    Idempotent — safe to call on every application startup. Uses
    `PRAGMA user_version` to track which migrations have been applied.

    After Migration 6 completes (or if it was already applied), runs the
    retroactive deduplication merge once. A sentinel row in `merge_log`
    (`merge_source='migration_complete'`) tracks that this has run so that
    subsequent startups skip the one-time operation.

    Args:
        db_path: Path to the SQLite database file.
        user_data_root: Directory where user-data backups live. Defaults to
            CWD. Used by Migration 41's backup-recency gate.
    """
    root = user_data_root if user_data_root is not None else os.getcwd()
    with standalone_connection(db_path) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=root)
        for migration in MIGRATIONS:
            if migration.version <= current_version:
                continue
            _apply_migration(ctx, migration)

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
        conn.execute(
            """
            INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at)
            VALUES ('__sentinel__', '__sentinel__', 'migration_complete', ?)
        """,
            (now_iso,),
        )
        conn.commit()

        if merged_count > 0:
            # Add activity feed entry so the user sees the merge count
            try:
                conn.execute(
                    """
                    INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                    VALUES (?, 'dedup_migration', ?, 0, 0)
                """,
                    (now_iso, merged_count),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log dedup migration run: %s", e)

            logger.info("Retroactive dedup: merged %d duplicate jobs.", merged_count)

            # Queue merged canonical rows for re-scoring by nulling the v3
            # scoring surface (classification/sub_scores_json) and the
            # rationale (fit_analysis). Plan 5 dropped haiku_score/sonnet_score;
            # the v3 scorer re-derives classification from sub_scores.
            try:
                canonical_keys = conn.execute(
                    "SELECT canonical_key FROM merge_log WHERE merge_source = 'migration'"
                ).fetchall()
                for row in canonical_keys:
                    conn.execute(
                        """
                        UPDATE jobs
                           SET classification = NULL,
                               sub_scores_json = NULL,
                               fit_analysis = NULL
                         WHERE dedup_key = ?
                    """,
                        (row[0],),
                    )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to queue merged rows for re-scoring: %s", e)
        else:
            logger.info("Retroactive dedup: no duplicates found.")

    except Exception as e:
        logger.warning("Retroactive dedup failed (non-fatal): %s", e)


def _apply_migration(ctx: MigrationContext, migration: Migration) -> None:
    """Apply a single migration and update PRAGMA user_version.

    Order: SQL statements first (in declared order), then the optional `py`
    helper. In practice no migration uses both — `py` is reserved for
    migrations that need filesystem or env state (Migration 41), and those
    perform their own DDL inside the helper.

    Per-statement idempotency:

    - `duplicate column name` errors from `ALTER TABLE ADD COLUMN` are caught
      and skipped, enabling re-runs of additive migrations on a populated
      schema.
    - `no such column` errors from `ALTER TABLE DROP COLUMN` are caught and
      skipped, enabling re-runs of destructive migrations after the column
      has already been removed.

    Any other `OperationalError` propagates and aborts the migration loop.

    Args:
        ctx: MigrationContext carrying the connection, DB path, and user-data
            root for any `py`-helpers that need them.
        migration: The Migration to apply.
    """
    for stmt in migration.sql:
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            ctx.conn.execute(stmt)
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "duplicate column name" in error_msg:
                # Column already exists — safe to skip for idempotent re-runs
                continue
            if "no such column" in error_msg:
                # Column already dropped (Mig 39+ re-run) — safe to skip
                continue
            raise

    if migration.py is not None:
        try:
            migration.py(ctx)
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if "no such column" not in error_msg:
                raise
            # Columns already dropped on a re-run — safe to skip

    # Commit once per migration (not per statement)
    ctx.conn.commit()

    # Update version counter after all statements succeed.
    # Migration.version is typed `int`; the isinstance check defends against
    # accidental shape drift before the f-string interpolation.
    if not isinstance(migration.version, int):
        raise TypeError(f"Migration version must be int, got {type(migration.version)}")
    ctx.conn.execute(f"PRAGMA user_version = {migration.version}")
    ctx.conn.commit()
    logger.info("Migration %d applied successfully.", migration.version)
