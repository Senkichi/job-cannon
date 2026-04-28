"""Integration tests for schema migration correctness.

Tests run against temporary SQLite databases.
All tests should FAIL initially (ImportError) until db_migrate.py is implemented.
"""

import sqlite3

import pytest

from job_finder.web.db_migrate import MIGRATIONS, run_migrations


class TestMigrationOnEmptyDB:
    """Tests for migration on a fresh empty database."""

    def test_creates_jobs_table_with_original_columns(self, tmp_db_path):
        """Migration creates jobs table with all original columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()

        original_columns = {
            "dedup_key",
            "title",
            "company",
            "location",
            "sources",
            "source_urls",
            "source_id",
            "salary_min",
            "salary_max",
            "description",
            "first_seen",
            "last_seen",
            "score",
            "score_breakdown",
            "user_interest",
        }
        assert original_columns.issubset(cols), (
            f"Missing original columns: {original_columns - cols}"
        )

    def test_creates_jobs_table_with_new_columns(self, tmp_db_path):
        """Migration adds new columns: pipeline_status, posted_date, notes."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()

        new_columns = {"pipeline_status", "posted_date", "notes"}
        assert new_columns.issubset(cols), f"Missing new columns: {new_columns - cols}"

    def test_creates_supporting_tables(self, tmp_db_path):
        """Migration creates all four supporting tables."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()

        expected_tables = {
            "pipeline_events",
            "email_parse_log",
            "resume_generations",
            "scoring_costs",
        }
        assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"

    def test_wal_mode_enabled(self, tmp_db_path):
        """WAL mode is enabled after migration."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal", f"Expected WAL mode, got: {mode}"

    def test_indexes_exist(self, tmp_db_path):
        """Indexes exist on required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()

        expected_indexes = {
            "idx_jobs_pipeline_status",
            "idx_pipeline_events_job_id",
            "idx_email_parse_log_message_id",
        }
        assert expected_indexes.issubset(indexes), f"Missing indexes: {expected_indexes - indexes}"

    def test_score_and_last_seen_indexes_exist(self, tmp_db_path):
        """Indexes on score and last_seen exist (may be from original schema or migration)."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        # At minimum these query-critical indexes must exist
        assert "idx_jobs_score" in indexes, "Missing idx_jobs_score"
        assert "idx_jobs_last_seen" in indexes, "Missing idx_jobs_last_seen"

    def test_new_column_defaults(self, tmp_db_path):
        """New columns have correct defaults (pipeline_status='discovered', notes='')."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        # Insert a minimal row to test defaults
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen)
            VALUES ('test|job|loc', 'Test Job', 'Test Co', 'Test Loc',
                    '2026-03-01', '2026-03-01')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT pipeline_status, notes FROM jobs WHERE dedup_key = 'test|job|loc'"
        ).fetchone()
        conn.close()
        assert row[0] == "discovered", f"Expected 'discovered', got: {row[0]}"
        assert row[1] == "", f"Expected '', got: {row[1]}"

    def test_pragma_user_version_increments(self, tmp_db_path):
        """PRAGMA user_version is updated after migration."""
        conn = sqlite3.connect(tmp_db_path)
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        assert version_before == 0, f"Expected version 0 before migration, got: {version_before}"
        # Version equals the total number of migrations applied
        assert version_after == len(MIGRATIONS), (
            f"Expected version {len(MIGRATIONS)} after migration, got: {version_after}"
        )


class TestMigrationPreservesData:
    """Tests for migration on a DB with existing job rows."""

    def test_preserves_all_rows(self, sample_db_with_jobs):
        """Migration on a DB with existing jobs preserves all rows."""
        # Count rows before migration
        conn = sqlite3.connect(sample_db_with_jobs)
        count_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()

        run_migrations(sample_db_with_jobs)

        conn = sqlite3.connect(sample_db_with_jobs)
        count_after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()

        assert count_before == 3, f"Expected 3 rows before migration, got: {count_before}"
        assert count_after == 3, f"Expected 3 rows after migration, got: {count_after}"

    def test_preserves_original_column_values(self, sample_db_with_jobs):
        """Migration preserves all original column values in existing rows.

        After retroactive dedup runs, dedup_keys are normalized to company|title
        format (location excluded). The test looks up the row by the new normalized
        key to verify column values are preserved.
        """
        run_migrations(sample_db_with_jobs)
        conn = sqlite3.connect(sample_db_with_jobs)
        conn.row_factory = sqlite3.Row

        # After retroactive dedup, the dedup_key is updated to normalized format:
        # 'thumbtack|senior data scientist' (no location suffix)
        row = conn.execute(
            "SELECT * FROM jobs WHERE dedup_key = 'thumbtack|senior data scientist'"
        ).fetchone()
        conn.close()

        assert row is not None, (
            "Sample job row not found after migration. "
            "Note: retroactive dedup renames dedup_keys to normalized company|title format."
        )
        assert row["title"] == "Senior Data Scientist"
        assert row["company"] == "Thumbtack"
        assert row["location"] == "United States"
        assert row["salary_min"] == 180000
        assert row["salary_max"] == 240000
        assert row["score"] == 8.5
        assert row["user_interest"] == "reviewing"

    def test_new_columns_have_defaults_on_existing_rows(self, sample_db_with_jobs):
        """Existing rows get default values for new columns after migration."""
        run_migrations(sample_db_with_jobs)
        conn = sqlite3.connect(sample_db_with_jobs)

        rows = conn.execute("SELECT pipeline_status, notes FROM jobs").fetchall()
        conn.close()

        for pipeline_status, notes in rows:
            assert pipeline_status == "discovered", (
                f"Expected 'discovered', got: {pipeline_status}"
            )
            assert notes == "", f"Expected '', got: {notes}"


class TestMigration5:
    """Tests for Migration 5 (Phase 5 Intelligence tables)."""

    def test_migrations_count_includes_migration5(self):
        """MIGRATIONS list includes at least 5 entries (Phase 5 added the 5th)."""
        assert len(MIGRATIONS) >= 5, f"Expected at least 5 migrations, got {len(MIGRATIONS)}"

    def test_migration5_creates_phase5_tables(self, tmp_db_path):
        """Migration 5 creates all Phase 5 tables: interview_preps, resume_preferences_detected, rejection_reports."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()

        expected = {"interview_preps", "resume_preferences_detected", "rejection_reports"}
        assert expected.issubset(tables), f"Missing Phase 5 tables: {expected - tables}"

    def test_migration5_interview_preps_columns(self, tmp_db_path):
        """Migration 5 interview_preps table has all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(interview_preps)").fetchall()}
        conn.close()

        expected = {
            "id",
            "job_id",
            "status",
            "company_brief",
            "predicted_questions",
            "gap_mitigation",
            "questions_to_ask",
            "error_msg",
            "generated_at",
            "cost_usd",
        }
        assert expected.issubset(cols), f"Missing interview_preps columns: {expected - cols}"

    def test_migration5_adds_rejection_reviewed_column(self, tmp_db_path):
        """Migration 5 adds rejection_reviewed column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "rejection_reviewed" in cols, "rejection_reviewed column missing from jobs"

    def test_migration5_adds_last_drive_polled_at_column(self, tmp_db_path):
        """Migration 5 adds last_drive_polled_at column to resume_generations table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()
        assert "last_drive_polled_at" in cols, (
            "last_drive_polled_at missing from resume_generations"
        )


class TestMigrationIdempotency:
    """Tests for idempotent migration behavior."""

    def test_migration_is_idempotent_on_empty_db(self, tmp_db_path):
        """Running migration twice on empty DB produces no errors."""
        run_migrations(tmp_db_path)
        # Second run must not raise
        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == len(MIGRATIONS)

    def test_migration_is_idempotent_on_existing_data(self, sample_db_with_jobs):
        """Running migration twice on DB with existing data produces no errors and no data loss."""
        run_migrations(sample_db_with_jobs)
        run_migrations(sample_db_with_jobs)

        conn = sqlite3.connect(sample_db_with_jobs)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 3, f"Expected 3 rows after double migration, got: {count}"


class TestMigration6:
    """Tests for Migration 6 (Phase 6 Data Quality schema additions)."""

    def test_migrations_count_includes_migration6(self):
        """MIGRATIONS list has at least 6 entries after Phase 6."""
        assert len(MIGRATIONS) >= 6, f"Expected at least 6 migrations, got {len(MIGRATIONS)}"

    def test_migration6_creates_batch_score_sessions_table(self, tmp_db_path):
        """Migration 6 creates batch_score_sessions table with all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(batch_score_sessions)").fetchall()
        }
        conn.close()

        expected = {
            "id",
            "session_type",
            "status",
            "total",
            "scored",
            "skipped",
            "started_at",
            "finished_at",
            "error_msg",
        }
        assert expected.issubset(cols), f"Missing batch_score_sessions columns: {expected - cols}"

    def test_migration6_creates_merge_log_table(self, tmp_db_path):
        """Migration 6 creates merge_log table with all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(merge_log)").fetchall()}
        conn.close()

        expected = {"id", "canonical_key", "merged_key", "merge_source", "merged_at"}
        assert expected.issubset(cols), f"Missing merge_log columns: {expected - cols}"

    def test_migration6_adds_locations_raw_column(self, tmp_db_path):
        """Migration 6 adds locations_raw column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "locations_raw" in cols, "locations_raw column missing from jobs"

    def test_migration6_adds_description_reformatted_column(self, tmp_db_path):
        """Migration 6 adds description_reformatted column (INTEGER DEFAULT 0) to jobs."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)

        # Check column exists
        col_info = {row[1]: row for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()

        assert "description_reformatted" in col_info, (
            "description_reformatted column missing from jobs"
        )

    def test_migration6_description_reformatted_default_zero(self, tmp_db_path):
        """description_reformatted defaults to 0 for new rows."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen)
            VALUES ('test|mig6|loc', 'Test Job', 'Test Co', 'Test Loc',
                    '2026-03-01', '2026-03-01')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT description_reformatted FROM jobs WHERE dedup_key = 'test|mig6|loc'"
        ).fetchone()
        conn.close()
        assert row[0] == 0, f"Expected description_reformatted=0, got: {row[0]}"

    def test_migration6_creates_merge_log_index(self, tmp_db_path):
        """Migration 6 creates idx_merge_log_canonical index on merge_log."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_merge_log_canonical" in indexes, "idx_merge_log_canonical index missing"

    def test_migration6_creates_batch_score_sessions_index(self, tmp_db_path):
        """Migration 6 creates idx_batch_score_sessions_status index."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_batch_score_sessions_status" in indexes, (
            "idx_batch_score_sessions_status index missing"
        )


def test_migration_count_is_thirteen():
    """v1.1 adds 4 migrations (9-12), Phase 19 cleanup adds Migration 13.

    NOTE: Migration 14 (Phase 30 infrastructure) and Migration 15 (Phase 40 data
    quality) were added after this test was written. Migration 16 adds company
    enrichment columns. Migration 17 adds homepage_probe_attempted_at column.
    Migration 18 (Phase 24) adds provider column to scoring_costs.
    Migration 19 adds opus_score column for Opus baseline evaluation.
    Migration 20 adds scoring_provider column for provider attribution (ATTR-01).
    Migration 23 recalibrates jobs_found_total and adds jobs_matched column.
    Migration 24 adds index on email_parse_log.processed_at for dedup query.
    Migration 25 cleans up Eightfold/PCS SPA shell garbage from jd_full.
    Migration 26 adds enrichment retry columns to companies table.
    Migration 27 adds career-ops scoring metadata columns (expiry_status, eval_blocks,
    job_archetype) to jobs table.
    Migration 28 adds reusable_stories_json to interview_preps.
    Migration 29 adds company_research table.
    Migration 30 adds careers_url to companies.
    Migration 31 adds careers_crawl_last_at to companies.
    Migration 40 adds v3.0 classification/sub_scores_json/scoring_model.
    Migration 41 (Plan 5) drops legacy haiku_score/haiku_summary/sonnet_score.
    Migration 42 (Phase 2d sub-fix 1/4) extends classification enum vocabulary
    to include 'low_signal' (no-op DDL — column has no CHECK constraint; bumps
    user_version and documents the new allowed value).
    Kept for historical reference; updated to reflect current count.
    """
    from job_finder.web.db_migrate import MIGRATIONS

    assert len(MIGRATIONS) == 42


class TestMigration27:
    """Tests for Migration 27 (career-ops scoring metadata columns)."""

    def test_migration27_adds_expiry_status(self, tmp_db_path):
        """Migration 27 adds expiry_status column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "expiry_status" in cols, "expiry_status column missing from jobs"

    def test_migration27_adds_eval_blocks(self, tmp_db_path):
        """Migration 27 adds eval_blocks column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "eval_blocks" in cols, "eval_blocks column missing from jobs"

    def test_migration27_adds_job_archetype(self, tmp_db_path):
        """Migration 27 adds job_archetype column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "job_archetype" in cols, "job_archetype column missing from jobs"

    def test_migration28_adds_reusable_stories_json(self, tmp_db_path):
        """Migration 28 adds reusable_stories_json column to interview_preps."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(interview_preps)").fetchall()}
        conn.close()
        assert "reusable_stories_json" in cols, (
            "reusable_stories_json column missing from interview_preps"
        )

    def test_migration27_expiry_status_default_null(self, tmp_db_path):
        """expiry_status defaults to NULL for existing rows."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen)
            VALUES ('test|mig27', 'Test Job', 'Test Co', 'Remote',
                    '2026-04-01', '2026-04-01')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT expiry_status, eval_blocks, job_archetype FROM jobs WHERE dedup_key = 'test|mig27'"
        ).fetchone()
        conn.close()
        assert row[0] is None, f"Expected expiry_status=NULL, got: {row[0]}"
        assert row[1] is None, f"Expected eval_blocks=NULL, got: {row[1]}"
        assert row[2] is None, f"Expected job_archetype=NULL, got: {row[2]}"


class TestMigration13:
    """Tests for Migration 13 (drop dead ATS retry columns from jobs table).

    Uses migrated_db_class (class-scoped) fixture — all tests are pure schema reads
    (PRAGMA table_info, PRAGMA user_version) so shared DB is safe across the class.
    """

    def test_migration13_removes_ats_retry_count(self, migrated_db_class):
        """Migration 13 removes ats_retry_count column from jobs table."""
        path, conn = migrated_db_class
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "ats_retry_count" not in cols, (
            "ats_retry_count column should have been dropped from jobs by Migration 13"
        )

    def test_migration13_removes_ats_last_error(self, migrated_db_class):
        """Migration 13 removes ats_last_error column from jobs table."""
        path, conn = migrated_db_class
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "ats_last_error" not in cols, (
            "ats_last_error column should have been dropped from jobs by Migration 13"
        )

    def test_migration13_removes_ats_retry_after(self, migrated_db_class):
        """Migration 13 removes ats_retry_after column from jobs table."""
        path, conn = migrated_db_class
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "ats_retry_after" not in cols, (
            "ats_retry_after column should have been dropped from jobs by Migration 13"
        )

    def test_migration13_user_version_is_thirteen(self, migrated_db_class):
        """Migration 13 was applied (user_version >= 13 after all migrations).

        NOTE: migrated_db_class runs ALL migrations, so user_version reflects
        the latest migration applied (currently 14 after Phase 30 infrastructure).
        This test verifies Migration 13 was applied by checking version >= 13.
        """
        path, conn = migrated_db_class
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 13, f"Expected user_version >= 13 (Migration 13 applied), got: {version}"


class TestMigration12:
    """Tests for Migration 12 (ATS retry columns on companies table)."""

    def test_migration12_adds_retry_count_to_companies(self, tmp_db_path):
        """Migration 12 adds retry_count column to companies table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        conn.close()
        assert "retry_count" in cols, (
            "retry_count column missing from companies after Migration 12"
        )


class TestMigration14:
    """Tests for Migration 14 (expiry_checked_at on jobs, validation_report on resume_generations).

    Uses migrated_db_class (class-scoped) fixture — all tests are pure schema reads
    (PRAGMA table_info, sqlite_master, PRAGMA user_version) so shared DB is safe across the class.
    """

    def test_jobs_has_expiry_checked_at_column(self, migrated_db_class):
        """Migration 14 adds expiry_checked_at column (TEXT, nullable) to jobs table."""
        path, conn = migrated_db_class
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "expiry_checked_at" in cols, (
            "expiry_checked_at column missing from jobs after Migration 14"
        )

    def test_resume_generations_has_validation_report_column(self, migrated_db_class):
        """Migration 14 adds validation_report column (TEXT, nullable) to resume_generations table."""
        path, conn = migrated_db_class
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        assert "validation_report" in cols, (
            "validation_report column missing from resume_generations after Migration 14"
        )

    def test_expiry_checked_at_index_exists(self, migrated_db_class):
        """Migration 14 creates idx_jobs_expiry_checked_at index on jobs table."""
        path, conn = migrated_db_class
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_jobs_expiry_checked_at'"
        ).fetchone()
        assert index is not None, "idx_jobs_expiry_checked_at index missing after Migration 14"

    def test_migration_count_is_14(self):
        """MIGRATIONS list has at least 14 entries (Migration 15 added in Phase 40)."""
        assert len(MIGRATIONS) >= 14, f"Expected at least 14 migrations, got: {len(MIGRATIONS)}"

    def test_user_version_is_14(self, migrated_db_class):
        """user_version is at least 14 after all migrations including Migration 14."""
        path, conn = migrated_db_class
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 14, f"Expected user_version>=14, got: {version}"

    def test_migration12_adds_retry_after_to_companies(self, tmp_db_path):
        """Migration 12 adds retry_after column to companies table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        conn.close()
        assert "retry_after" in cols, (
            "retry_after column missing from companies after Migration 12"
        )

    def test_migration12_adds_miss_reason_to_companies(self, tmp_db_path):
        """Migration 12 adds miss_reason column to companies table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        conn.close()
        assert "miss_reason" in cols, (
            "miss_reason column missing from companies after Migration 12"
        )

    def test_migration12_retry_count_defaults_to_zero(self, tmp_db_path):
        """Migration 12 retry_count defaults to 0 for new company rows."""
        run_migrations(tmp_db_path)
        from datetime import datetime

        conn = sqlite3.connect(tmp_db_path)
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies (name, name_raw, created_at, updated_at)
               VALUES ('retryco', 'RetryCo', ?, ?)""",
            (now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT retry_count, retry_after, miss_reason FROM companies WHERE name = 'retryco'"
        ).fetchone()
        conn.close()
        assert row[0] == 0, f"Expected retry_count=0, got: {row[0]}"
        assert row[1] is None, f"Expected retry_after=None, got: {row[1]}"
        assert row[2] is None, f"Expected miss_reason=None, got: {row[2]}"


class TestMigration7:
    """Tests for Migration 7 (Phase 7 Company Tracking schema additions)."""

    def test_migrations_count_includes_migration7(self):
        """MIGRATIONS list has at least 7 entries after Phase 7."""
        assert len(MIGRATIONS) >= 7, f"Expected at least 7 migrations, got {len(MIGRATIONS)}"

    def test_migration7_creates_companies_table(self, tmp_db_path):
        """Migration 7 creates companies table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        assert "companies" in tables, "companies table missing after Migration 7"

    def test_migration7_companies_table_has_all_columns(self, tmp_db_path):
        """Migration 7 companies table has all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        conn.close()

        expected = {
            "id",
            "name",
            "name_raw",
            "homepage_url",
            "ats_platform",
            "ats_slug",
            "ats_probe_status",
            "ats_probe_attempted_at",
            "scan_enabled",
            "last_scanned_at",
            "jobs_found_total",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols), f"Missing companies columns: {expected - cols}"

    def test_migration7_creates_company_scan_log_table(self, tmp_db_path):
        """Migration 7 creates company_scan_log table with FK to companies."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        assert "company_scan_log" in tables, "company_scan_log table missing after Migration 7"

    def test_migration7_company_scan_log_has_all_columns(self, tmp_db_path):
        """Migration 7 company_scan_log table has all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(company_scan_log)").fetchall()}
        conn.close()

        expected = {"id", "company_id", "scanned_at", "jobs_found", "error"}
        assert expected.issubset(cols), f"Missing company_scan_log columns: {expected - cols}"

    def test_migration7_adds_company_id_to_jobs_table(self, tmp_db_path):
        """Migration 7 adds company_id column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "company_id" in cols, "company_id column missing from jobs after Migration 7"

    def test_migration7_adds_comp_data_json_to_jobs_table(self, tmp_db_path):
        """Migration 7 adds comp_data_json column to jobs table for ATS compensation data."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "comp_data_json" in cols, (
            "comp_data_json column missing from jobs after Migration 7"
        )

    def test_migration7_fixup_adds_comp_data_json_on_rerun(self, tmp_db_path):
        """Re-running migrations on DB where user_version=7 but comp_data_json missing still adds column."""
        # First run to get to version 7
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        # Verify column exists (it will from the migration itself on fresh DB)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "comp_data_json" in cols
        # Re-run should not fail (fixup handles already-existing column gracefully)
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "comp_data_json" in cols

    def test_migration7_creates_companies_indexes(self, tmp_db_path):
        """Migration 7 creates all required indexes on companies table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()

        expected_indexes = {
            "idx_companies_name",
            "idx_companies_ats_platform",
            "idx_companies_ats_probe_status",
            "idx_companies_scan_enabled",
            "idx_company_scan_log_company_id",
        }
        assert expected_indexes.issubset(indexes), (
            f"Missing Migration 7 indexes: {expected_indexes - indexes}"
        )

    def test_migration7_companies_probe_status_defaults_to_pending(self, tmp_db_path):
        """companies.ats_probe_status defaults to 'pending' for new rows."""
        run_migrations(tmp_db_path)
        from datetime import datetime

        conn = sqlite3.connect(tmp_db_path)
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies (name, name_raw, created_at, updated_at)
               VALUES ('testco', 'TestCo', ?, ?)""",
            (now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ats_probe_status, scan_enabled FROM companies WHERE name = 'testco'"
        ).fetchone()
        conn.close()
        assert row[0] == "pending", f"Expected 'pending', got: {row[0]}"
        assert row[1] == 1, f"Expected scan_enabled=1, got: {row[1]}"


# ---------------------------------------------------------------------------
# Consolidated migration tests (relocated from domain test files, Phase 24)
# ---------------------------------------------------------------------------


class TestMigration2:
    """Verify Migration 2 added AI-scoring scaffolding that *persists* through
    the full migration chain.

    Plan 5 (Migration 41) dropped the transient haiku_score / haiku_summary /
    sonnet_score columns and idx_jobs_haiku_score index. The fit_analysis,
    jd_full, is_stale columns Migration 2 added remain in the final schema and
    are the load-bearing survivors — they're asserted here.

    Class-scope safe: all migrated_db tests are pure PRAGMA reads (schema
    verification only). Confirmed by audit (Plan 20-01). One test uses
    tmp_db_path independently.
    """

    def test_migration2_legacy_score_columns_dropped_by_mig41(self, migrated_db_class):
        """Post-Mig-41: the transient legacy score columns are gone."""
        path, conn = migrated_db_class
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "haiku_score" not in cols
        assert "haiku_summary" not in cols
        assert "sonnet_score" not in cols

    def test_migration2_adds_fit_analysis_column(self, migrated_db_class):
        path, conn = migrated_db_class
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "fit_analysis" in cols

    def test_migration2_adds_jd_full_column(self, migrated_db_class):
        path, conn = migrated_db_class
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "jd_full" in cols

    def test_migration2_adds_is_stale_column(self, migrated_db_class):
        path, conn = migrated_db_class
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "is_stale" in cols

    def test_migration2_legacy_haiku_score_index_dropped_by_mig41(self, migrated_db_class):
        """Post-Mig-41: idx_jobs_haiku_score is gone (dropped before the column)."""
        path, conn = migrated_db_class
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_jobs_haiku_score" not in indexes

    def test_migration2_adds_is_stale_index(self, migrated_db_class):
        path, conn = migrated_db_class
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_jobs_is_stale" in indexes

    def test_migration2_is_idempotent(self, tmp_db_path):
        """Running migrations twice on same DB must not raise."""
        run_migrations(tmp_db_path)
        run_migrations(tmp_db_path)  # should not raise
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        # fit_analysis survives the full chain; haiku_score is dropped by Mig 41.
        assert "fit_analysis" in cols
        assert "haiku_score" not in cols

    def test_migration2_user_version_is_current(self, migrated_db_class):
        from job_finder.web.db_migrate import MIGRATIONS

        path, conn = migrated_db_class
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(MIGRATIONS)


class TestMigration3:
    """Integration tests for Migration 3 (pipeline_detections table).

    Class-scope safe: schema reads and one unique-constraint test (msg_001 insert
    does not affect other tests which are pure schema/index reads). Confirmed by
    audit (Plan 20-01).
    """

    def test_migration3_creates_pipeline_detections_table(self, migrated_db_class):
        path, conn = migrated_db_class
        # Verify table exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_detections'"
        ).fetchone()
        assert row is not None, "pipeline_detections table should exist after Migration 3"

    def test_pipeline_detections_has_correct_columns(self, migrated_db_class):
        path, conn = migrated_db_class
        cols_rows = conn.execute("PRAGMA table_info(pipeline_detections)").fetchall()
        col_names = {row[1] for row in cols_rows}
        expected = {
            "id",
            "gmail_message_id",
            "detection_type",
            "job_id",
            "confidence_score",
            "matched_signals",
            "snippet",
            "email_subject",
            "email_from",
            "email_date",
            "status",
            "created_at",
            "resolved_at",
        }
        assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"

    def test_pipeline_detections_gmail_message_id_unique(self, migrated_db_class):
        from datetime import datetime

        path, conn = migrated_db_class
        now = datetime.now().isoformat()
        # Insert with unique key for this class (msg_mig3_unique)
        conn.execute(
            """INSERT OR IGNORE INTO pipeline_detections
               (gmail_message_id, detection_type, confidence_score, email_date, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_mig3_unique", "rejection", 2, now, "pending", now),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO pipeline_detections
                   (gmail_message_id, detection_type, confidence_score, email_date, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("msg_mig3_unique", "interview", 1, now, "pending", now),
            )
            conn.commit()

    def test_migration3_indexes_exist(self, migrated_db_class):
        path, conn = migrated_db_class
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pipeline_detections'"
        ).fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_pipeline_detections_status" in index_names
        assert "idx_pipeline_detections_job_id" in index_names
        assert "idx_pipeline_detections_message_id" in index_names


class TestMigration4:
    """Migration 4 adds status tracking columns to resume_generations."""

    def test_migration_4_adds_generation_type_column(self, tmp_db_path):
        """Migration 4 adds generation_type column to resume_generations."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()

        assert "generation_type" in cols, "Missing generation_type column"

    def test_migration_4_adds_status_column(self, tmp_db_path):
        """Migration 4 adds status column to resume_generations."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()

        assert "status" in cols, "Missing status column"

    def test_migration_4_adds_strategy_column(self, tmp_db_path):
        """Migration 4 adds strategy column to resume_generations."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()

        assert "strategy" in cols, "Missing strategy column"

    def test_migration_4_adds_error_msg_column(self, tmp_db_path):
        """Migration 4 adds error_msg column to resume_generations."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()

        assert "error_msg" in cols, "Missing error_msg column"

    def test_migration_4_creates_job_id_index(self, tmp_db_path):
        """Migration 4 creates idx_resume_generations_job_id index."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()

        assert "idx_resume_generations_job_id" in indexes, (
            "Missing idx_resume_generations_job_id index"
        )

    def test_migration_4_creates_status_index(self, tmp_db_path):
        """Migration 4 creates idx_resume_generations_status index."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()

        assert "idx_resume_generations_status" in indexes, (
            "Missing idx_resume_generations_status index"
        )

    def test_migration_4_status_default_is_done(self, tmp_db_path):
        """New rows in resume_generations get status='done' by default."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO resume_generations (job_id, generated_at, model)
               VALUES ('test-job', '2026-03-11T00:00:00', 'claude-3-haiku')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM resume_generations WHERE job_id = 'test-job'"
        ).fetchone()
        conn.close()

        assert row[0] == "done", f"Expected status='done', got: {row[0]}"

    def test_migration_4_generation_type_default_is_single(self, tmp_db_path):
        """New rows in resume_generations get generation_type='single' by default."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO resume_generations (job_id, generated_at, model)
               VALUES ('test-job-2', '2026-03-11T00:00:00', 'claude-3-haiku')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT generation_type FROM resume_generations WHERE job_id = 'test-job-2'"
        ).fetchone()
        conn.close()

        assert row[0] == "single", f"Expected generation_type='single', got: {row[0]}"

    def test_migration_4_is_idempotent(self, tmp_db_path):
        """Running migrations twice does not raise for Migration 4 columns."""
        run_migrations(tmp_db_path)
        # Second run must not raise
        run_migrations(tmp_db_path)


class TestMigration5InterviewPrep:
    """Verify Migration 5 creates all Phase 5 tables and columns (from test_interview_prep.py)."""

    def test_migration_count_includes_migration5(self):
        """MIGRATIONS list has at least 5 entries (Phase 5 added the 5th)."""
        assert len(MIGRATIONS) >= 5, (
            f"Expected at least 5 migrations, got {len(MIGRATIONS)}. "
            "Did you add Migration 5 to db_migrate.py?"
        )

    def test_migration5_creates_interview_preps_table(self, tmp_db_path):
        """Migration 5 creates interview_preps table with all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(interview_preps)").fetchall()}
        conn.close()

        expected = {
            "id",
            "job_id",
            "status",
            "company_brief",
            "predicted_questions",
            "gap_mitigation",
            "questions_to_ask",
            "error_msg",
            "generated_at",
            "cost_usd",
        }
        assert expected.issubset(cols), f"Missing columns in interview_preps: {expected - cols}"

    def test_migration5_creates_resume_preferences_detected_table(self, tmp_db_path):
        """Migration 5 creates resume_preferences_detected table with all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(resume_preferences_detected)").fetchall()
        }
        conn.close()

        expected = {
            "id",
            "job_id",
            "preference_type",
            "preference_text",
            "example_before",
            "example_after",
            "accepted",
            "detected_at",
            "applied_at",
        }
        assert expected.issubset(cols), (
            f"Missing columns in resume_preferences_detected: {expected - cols}"
        )

    def test_migration5_creates_rejection_reports_table(self, tmp_db_path):
        """Migration 5 creates rejection_reports table with all required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(rejection_reports)").fetchall()}
        conn.close()

        expected = {"id", "report_text", "rejections_analyzed", "generated_at", "cost_usd"}
        assert expected.issubset(cols), f"Missing columns in rejection_reports: {expected - cols}"

    def test_migration5_adds_rejection_reviewed_to_jobs(self, tmp_db_path):
        """Migration 5 adds rejection_reviewed column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "rejection_reviewed" in cols, "rejection_reviewed column missing from jobs table"

    def test_migration5_adds_last_drive_polled_at_to_resume_generations(self, tmp_db_path):
        """Migration 5 adds last_drive_polled_at column to resume_generations table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()}
        conn.close()
        assert "last_drive_polled_at" in cols, (
            "last_drive_polled_at column missing from resume_generations table"
        )

    def test_migration5_interview_preps_index_exists(self, tmp_db_path):
        """Migration 5 creates index on interview_preps.job_id."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_interview_preps_job_id" in indexes, "Missing idx_interview_preps_job_id"


class TestMigration5Reporting:
    """Verify Migration 5 creates required tables and columns for Phase 5 (from test_rejection_analyzer.py)."""

    def test_rejection_reports_table_exists(self, tmp_db_path):
        """Migration creates rejection_reports table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        assert "rejection_reports" in tables, "rejection_reports table missing after Migration 5"

    def test_rejection_reports_columns(self, tmp_db_path):
        """rejection_reports table has required columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(rejection_reports)").fetchall()}
        conn.close()

        expected = {"id", "report_text", "rejections_analyzed", "generated_at", "cost_usd"}
        assert expected.issubset(cols), f"Missing columns in rejection_reports: {expected - cols}"

    def test_jobs_has_rejection_reviewed_column(self, tmp_db_path):
        """Migration 5 adds rejection_reviewed column to jobs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "rejection_reviewed" in cols, "rejection_reviewed column missing from jobs"

    def test_rejection_reviewed_default_zero(self, tmp_db_path):
        """rejection_reviewed defaults to 0 for new rows."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen)
            VALUES ('test|job|loc', 'Test Job', 'Test Co', 'Remote',
                    '2026-03-01', '2026-03-10')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT rejection_reviewed FROM jobs WHERE dedup_key='test|job|loc'"
        ).fetchone()
        conn.close()
        assert row[0] == 0, f"Expected rejection_reviewed=0, got {row[0]}"


class TestMigration18:
    """Tests for migration 18: provider column on scoring_costs."""

    def test_migration_18_adds_provider_column(self, tmp_db_path):
        """Migration 18 adds provider column to scoring_costs table."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(scoring_costs)").fetchall()}
        conn.close()
        assert "provider" in columns

    def test_migration_18_provider_default_is_anthropic(self, tmp_db_path):
        """Provider column defaults to 'anthropic' for new rows."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES ('test-job', 'haiku_score', 'claude-haiku-4-5', 100, 50, 0.01, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT provider FROM scoring_costs WHERE job_id = 'test-job'"
        ).fetchone()
        conn.close()
        assert row[0] == "anthropic"

    def test_migration_18_existing_record_cost_insert_still_works(self, tmp_db_path):
        """The exact INSERT from record_cost() (no provider column) succeeds post-migration."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        # This is the exact INSERT statement from claude_client.py record_cost()
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "test-job-2",
                "sonnet_eval",
                "claude-sonnet-4-6",
                500,
                200,
                0.05,
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT provider FROM scoring_costs WHERE job_id = 'test-job-2'"
        ).fetchone()
        conn.close()
        assert row[0] == "anthropic"

    def test_migrations_count_is_19(self):
        """MIGRATIONS list has exactly 42 entries (Migration 42: Phase 2d low_signal enum bump)."""
        assert len(MIGRATIONS) == 42


class TestMigration40:
    """Tests for Migration 40 (v3.0 ordinal rubric scoring — additive schema).

    Adds classification, sub_scores_json, scoring_model columns + idx_jobs_classification.
    No data loss; rollback is `git revert` + drop new columns.
    """

    def test_migration_40_additive_schema(self, tmp_db_path):
        """Migration 40 adds classification, sub_scores_json, scoring_model columns to jobs."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        col_info = {row[1]: row for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()

        # All three new columns exist with TEXT type and default NULL
        for col in ("classification", "sub_scores_json", "scoring_model"):
            assert col in col_info, f"Migration 40: missing column {col}"
            # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
            assert col_info[col][2].upper() == "TEXT", (
                f"Migration 40: {col} has type {col_info[col][2]}, expected TEXT"
            )
            assert col_info[col][4] is None or str(col_info[col][4]).upper() == "NULL", (
                f"Migration 40: {col} default is {col_info[col][4]}, expected NULL"
            )

    def test_migration_40_creates_classification_index(self, tmp_db_path):
        """Migration 40 creates idx_jobs_classification on jobs(classification)."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_jobs_classification'"
        ).fetchone()
        conn.close()
        assert row is not None, "Migration 40: idx_jobs_classification index missing"

    def test_migration_40_user_version_increments(self, tmp_db_path):
        """Migration 40 increments user_version to 40 (matches len(MIGRATIONS))."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == len(MIGRATIONS), (
            f"Migration 40: user_version={version}, expected {len(MIGRATIONS)}"
        )
        # Post-Plan-5 the final user_version is 41 (Mig 41 ran on top of Mig 40).
        assert version >= 40, f"Migration 40: expected user_version>=40, got {version}"

    def test_migration_40_defaults_null_on_new_row(self, tmp_db_path):
        """New jobs rows have classification/sub_scores_json/scoring_model = NULL by default."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen)
            VALUES ('test|mig40|scorer', 'Test Job', 'Test Co', 'Remote',
                    '2026-04-21', '2026-04-21')"""
        )
        conn.commit()
        row = conn.execute(
            "SELECT classification, sub_scores_json, scoring_model "
            "FROM jobs WHERE dedup_key = 'test|mig40|scorer'"
        ).fetchone()
        conn.close()
        assert row[0] is None, f"Expected classification=NULL, got {row[0]!r}"
        assert row[1] is None, f"Expected sub_scores_json=NULL, got {row[1]!r}"
        assert row[2] is None, f"Expected scoring_model=NULL, got {row[2]!r}"

    def test_migration_40_idempotent(self, tmp_db_path):
        """Running migrations twice (incl. Migration 40) does not raise — columns still exist."""
        run_migrations(tmp_db_path)
        # Second run must not raise duplicate-column errors
        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        for col in ("classification", "sub_scores_json", "scoring_model"):
            assert col in cols, f"Migration 40 idempotency: {col} missing after double-run"
        # After Plan 5 lands, user_version advances to 41 (Mig 41 follows Mig 40).
        assert version >= 40, f"Migration 40 idempotency: user_version={version}, expected >=40"

    def test_migration_40_fit_analysis_preserved_by_mig41(self, tmp_db_path):
        """Migration 41 preserves fit_analysis (holds the v3.0 rationale payload).

        The transient legacy scoring columns (haiku_score / haiku_summary /
        sonnet_score) are dropped by Mig 41; fit_analysis remains because it
        now carries the rationale JSON (strengths / gaps / talking_points /
        resume_priority_skills).
        """
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "fit_analysis" in cols
        for dropped in ("haiku_score", "haiku_summary", "sonnet_score"):
            assert dropped not in cols, (
                f"Migration 41 should have dropped {dropped}, but it is still present"
            )


class TestMigration41DestructiveShape:
    """Shape tests for Migration 41 -- the destructive legacy-score column drop.

    Plan 5 asserts: after run_migrations on a fresh DB, haiku_score,
    haiku_summary, and sonnet_score columns are absent; idx_jobs_haiku_score
    index is absent; everything else Plan 5 intended to preserve is present.
    """

    def test_mig41_drops_haiku_score_column(self, tmp_db_path):
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "haiku_score" not in cols

    def test_mig41_drops_haiku_summary_column(self, tmp_db_path):
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "haiku_summary" not in cols

    def test_mig41_drops_sonnet_score_column(self, tmp_db_path):
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        assert "sonnet_score" not in cols

    def test_mig41_drops_idx_jobs_haiku_score_index(self, tmp_db_path):
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        conn.close()
        assert "idx_jobs_haiku_score" not in indexes

    def test_mig41_preserves_v3_columns(self, tmp_db_path):
        """Migration 41 preserves the v3 scoring surface and all untouched columns."""
        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()
        for preserved in (
            "fit_analysis",
            "scoring_provider",
            "scoring_model",
            "eval_blocks",
            "opus_score",
            "score",
            "job_archetype",
            "legitimacy_note",
            "classification",
            "sub_scores_json",
        ):
            assert preserved in cols, f"Mig 41 should preserve {preserved}"

    def test_mig41_preserves_existing_data(self, tmp_db_path):
        """Rows populated with v3 columns before Mig 41 retain their values after."""
        import json
        import os
        import tempfile

        from job_finder.web.db_migrate import (
            MIGRATIONS,
            _apply_migration,
            _migration_41_drop_legacy_scores,
        )

        # Run all migrations EXCEPT Migration 41 so we can populate legacy
        # columns with a known payload before the destructive migration lands.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            for i, m in enumerate(MIGRATIONS, start=1):
                if m is _migration_41_drop_legacy_scores:
                    break
                _apply_migration(conn, i, m)

            # Seed a row with both legacy columns AND v3 columns populated.
            sub_scores = {
                "title_fit": 4,
                "location_fit": 4,
                "comp_fit": 3,
                "domain_match": 4,
                "seniority_match": 4,
                "skills_match": 3,
            }
            conn.execute(
                """INSERT INTO jobs
                    (dedup_key, title, company, location, first_seen, last_seen,
                     haiku_score, sonnet_score, haiku_summary,
                     classification, sub_scores_json, fit_analysis,
                     scoring_provider, scoring_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "preserve-me",
                    "Engineer",
                    "Acme",
                    "Remote",
                    "2026-04-23",
                    "2026-04-23",
                    65.0,
                    78.0,
                    "summary-text",
                    "apply",
                    json.dumps(sub_scores),
                    json.dumps({"strengths": ["ML"]}),
                    "ollama",
                    "qwen2.5:14b",
                ),
            )
            conn.commit()

            # Now apply Mig 41
            _apply_migration(
                conn,
                MIGRATIONS.index(_migration_41_drop_legacy_scores) + 1,
                _migration_41_drop_legacy_scores,
            )

            row = conn.execute(
                "SELECT classification, sub_scores_json, fit_analysis, "
                "scoring_provider, scoring_model "
                "FROM jobs WHERE dedup_key = 'preserve-me'"
            ).fetchone()
            conn.close()
            assert row["classification"] == "apply"
            assert json.loads(row["sub_scores_json"]) == sub_scores
            assert json.loads(row["fit_analysis"]) == {"strengths": ["ML"]}
            assert row["scoring_provider"] == "ollama"
            assert row["scoring_model"] == "qwen2.5:14b"
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except PermissionError:
                    pass

    def test_mig41_is_idempotent(self, tmp_db_path):
        """Running the full migration chain twice does not raise on Mig 41 re-run."""
        run_migrations(tmp_db_path)
        run_migrations(tmp_db_path)  # must not raise
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "haiku_score" not in cols
        assert version == len(MIGRATIONS)


class TestMigration41BackupGate:
    """Preflight tests for Migration 41's backup-recency gate.

    The gate reads GSD_BACKUP_CONFIRMED env var and globs for
    backup_userdata_*.tar.gz in cwd. Raises MigrationBlockedError when the
    combination indicates no recent backup is available. conftest.py sets
    GSD_BACKUP_CONFIRMED=1 session-wide, so these tests monkeypatch it away
    to exercise the real gate logic.
    """

    def test_gate_raises_with_no_backup_and_no_override(self, monkeypatch, tmp_path):
        from job_finder.web.db_migrate import (
            MigrationBlockedError,
            _check_backup_recent,
        )

        monkeypatch.delenv("GSD_BACKUP_CONFIRMED", raising=False)
        monkeypatch.chdir(tmp_path)  # empty directory -- no backup tarballs
        with pytest.raises(MigrationBlockedError, match=r"no backup_userdata_\*\.tar\.gz"):
            _check_backup_recent()

    def test_gate_raises_when_backup_older_than_24h(self, monkeypatch, tmp_path):
        import os
        import time

        from job_finder.web.db_migrate import (
            MigrationBlockedError,
            _check_backup_recent,
        )

        monkeypatch.delenv("GSD_BACKUP_CONFIRMED", raising=False)
        monkeypatch.chdir(tmp_path)
        # Create a backup tarball with mtime 48h in the past
        stale = tmp_path / "backup_userdata_20260101_000000.tar.gz"
        stale.write_bytes(b"")
        old_mtime = time.time() - (48 * 3600)
        os.utime(stale, (old_mtime, old_mtime))

        with pytest.raises(MigrationBlockedError, match=r"h old \(>24h\)"):
            _check_backup_recent()

    def test_gate_allows_fresh_backup(self, monkeypatch, tmp_path):
        from job_finder.web.db_migrate import _check_backup_recent

        monkeypatch.delenv("GSD_BACKUP_CONFIRMED", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "backup_userdata_fresh.tar.gz").write_bytes(b"")
        # Fresh mtime (now) -- should not raise
        _check_backup_recent()

    def test_gate_allows_env_override(self, monkeypatch, tmp_path):
        from job_finder.web.db_migrate import _check_backup_recent

        # No backups + explicit override -- must not raise
        monkeypatch.setenv("GSD_BACKUP_CONFIRMED", "1")
        monkeypatch.chdir(tmp_path)
        _check_backup_recent()

    def test_gate_env_override_any_other_value_does_not_bypass(self, monkeypatch, tmp_path):
        """GSD_BACKUP_CONFIRMED only bypasses the gate when literally '1'."""
        from job_finder.web.db_migrate import (
            MigrationBlockedError,
            _check_backup_recent,
        )

        monkeypatch.setenv("GSD_BACKUP_CONFIRMED", "yes")  # not '1'
        monkeypatch.chdir(tmp_path)
        with pytest.raises(MigrationBlockedError):
            _check_backup_recent()
