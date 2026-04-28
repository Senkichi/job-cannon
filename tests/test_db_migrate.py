"""Tests for db_migrate.py — focused on per-migration schema/constraint behavior.

The full migration chain runs in conftest fixtures for almost every other
test file; this module exists to assert specific per-migration outcomes
that aren't covered indirectly elsewhere.
"""

import sqlite3
from contextlib import closing

import pytest

from job_finder.web.db_migrate import run_migrations


def test_migration_43_adds_gold_columns(tmp_db_path):
    """Migration 43 adds 4 nullable gold_* columns to jobs."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "gold_classification" in cols
    assert "gold_sub_scores_json" in cols
    assert "gold_notes" in cols
    assert "gold_labeled_at" in cols


def test_migration_44_adds_gold_no_signal_axes(tmp_db_path):
    """Migration 44 adds gold_no_signal_axes (nullable JSON) to jobs."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "gold_no_signal_axes" in cols


def test_migration_43_user_version_advances(tmp_db_path):
    """After running migrations on a fresh DB, user_version is at least 43."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 43


def test_migration_45_creates_eval_runs(tmp_db_path):
    """Migration 45 creates the eval_runs table with the documented columns."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_runs)").fetchall()}
    expected = {
        "run_id",
        "timestamp",
        "variant_name",
        "baseline_run_id",
        "gold_set_version",
        "n_runs",
        "config_json",
        "metrics_json",
        "per_job_json",
        "report_path",
        "notes",
    }
    assert expected <= cols


def test_migration_45_user_version_advances(tmp_db_path):
    """After running migrations on a fresh DB, user_version is at least 45."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 45


def test_migration_43_check_constraint_rejects_invalid_enum(tmp_db_path):
    """gold_classification CHECK constraint rejects values outside the 5-value enum.

    All NOT NULL columns on jobs are populated so the failure here is the
    CHECK constraint firing, not a NOT NULL violation on title/company/etc.
    """
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO jobs
                     (dedup_key, title, company, location,
                      first_seen, last_seen, gold_classification)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "a|b",
                    "T",
                    "C",
                    "Remote",
                    "2026-04-28T00:00:00",
                    "2026-04-28T00:00:00",
                    "invalid_enum_value",
                ),
            )


def test_migration_43_check_constraint_accepts_valid_enum(tmp_db_path):
    """gold_classification accepts each of the 5 valid enum values and NULL."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        for i, cls in enumerate((None, "apply", "consider", "skip", "reject", "low_signal")):
            conn.execute(
                """INSERT INTO jobs
                     (dedup_key, title, company, location,
                      first_seen, last_seen, gold_classification)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"k{i}|t",
                    "T",
                    "C",
                    "Remote",
                    "2026-04-28T00:00:00",
                    "2026-04-28T00:00:00",
                    cls,
                ),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM jobs WHERE dedup_key LIKE 'k%|t'").fetchone()[0]
    assert n == 6
