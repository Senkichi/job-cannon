"""Tests for wholesale_rescore.py — nullify-and-trigger script.

The script is destructive in production (nullifies scoring columns across the
entire jobs table). These tests run against a temp DB to verify:
  - --dry-run leaves data untouched
  - real run nullifies the five v3.0 scoring columns
  - gold_* columns (user-authored ground truth) are preserved
  - missing/wrong confirmation aborts with a non-zero exit
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest


def _make_jobs_table(conn: sqlite3.Connection) -> None:
    """Mirror the prod jobs schema for the columns wholesale_rescore touches.

    Includes the five columns the script nullifies AND gold_classification, so
    we can assert gold data is preserved.
    """
    conn.execute(
        """CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            classification TEXT,
            sub_scores_json TEXT,
            fit_analysis TEXT,
            scoring_provider TEXT,
            scoring_model TEXT,
            gold_classification TEXT
        )"""
    )


@pytest.fixture
def db_with_scored_rows(tmp_path):
    db = tmp_path / "test.db"
    with closing(sqlite3.connect(db)) as conn:
        _make_jobs_table(conn)
        rows = [
            ("a|1", "apply", '{"x":3}', '{"strengths":[]}', "ollama", "qwen2.5:14b", None),
            ("a|2", "consider", '{"x":3}', '{"strengths":[]}', "ollama", "qwen2.5:14b", None),
            ("a|3", "reject", '{"x":3}', '{"strengths":[]}', "ollama", "qwen2.5:14b", "skip"),
        ]
        conn.executemany(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return str(db)


def test_dry_run_does_not_modify(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications

    nullify_classifications(db_with_scored_rows, dry_run=True)

    with closing(sqlite3.connect(db_with_scored_rows)) as conn:
        cls_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
        ).fetchone()[0]
    assert cls_count == 3, "Dry-run must not change anything"


def test_real_run_nullifies_classifications(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications

    nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")

    with closing(sqlite3.connect(db_with_scored_rows)) as conn:
        n_class = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
        ).fetchone()[0]
        n_subs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE sub_scores_json IS NOT NULL"
        ).fetchone()[0]
        n_fit = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fit_analysis IS NOT NULL"
        ).fetchone()[0]
        n_provider = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE scoring_provider IS NOT NULL"
        ).fetchone()[0]
        n_model = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE scoring_model IS NOT NULL"
        ).fetchone()[0]

    assert n_class == 0
    assert n_subs == 0
    assert n_fit == 0
    assert n_provider == 0
    assert n_model == 0


def test_preserves_gold_columns(db_with_scored_rows):
    """gold_classification is user-authored ground truth — MUST survive a wholesale rescore."""
    from scripts.wholesale_rescore import nullify_classifications

    nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")

    with closing(sqlite3.connect(db_with_scored_rows)) as conn:
        n_gold = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE gold_classification IS NOT NULL"
        ).fetchone()[0]
    assert n_gold == 1


def test_aborts_without_confirmation(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications

    with pytest.raises(SystemExit):
        nullify_classifications(db_with_scored_rows, dry_run=False, confirm="no")

    with closing(sqlite3.connect(db_with_scored_rows)) as conn:
        cls_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
        ).fetchone()[0]
    assert cls_count == 3, "Aborted run must leave data untouched"


def test_returns_rowcount_on_real_run(db_with_scored_rows):
    """The function returns the number of rows it nullified — useful for the operator log."""
    from scripts.wholesale_rescore import nullify_classifications

    n = nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")
    assert n == 3


def test_idempotent_on_already_null_db(db_with_scored_rows):
    """Running twice in a row is a no-op the second time (rowcount=0)."""
    from scripts.wholesale_rescore import nullify_classifications

    nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")
    n2 = nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")
    assert n2 == 0
