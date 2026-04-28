"""Tests for the labeling CLI's per-job step (input/output isolation).

The interactive walker (`main()`) is not exercised end-to-end; only the
single-row labeler `label_one()` is tested, with `input()` patched to
return a scripted iterator.
"""

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest


@pytest.fixture
def db_with_one_job(tmp_db_path):
    """Run the real migrations and seed one row matching the labeler's reads."""
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            """INSERT INTO jobs
                 (dedup_key, title, company, location, sources, jd_full,
                  classification, sub_scores_json,
                  first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "k|t",
                "Test Engineer",
                "Acme",
                "Remote",
                '["linkedin"]',
                "A long JD " + "X" * 2000,
                "apply",
                json.dumps(
                    {
                        "title_fit": 4,
                        "location_fit": 5,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 4,
                        "skills_match": 3,
                    }
                ),
                "2026-04-01T00:00:00",
                "2026-04-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_db_path


def _patch_inputs(values):
    """Return a context manager that feeds successive input() calls from values."""
    it = iter(values)
    return patch("builtins.input", lambda *_: next(it))


def test_label_one_writes_gold_columns(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one

    inputs = [
        "consider",  # gold_classification
        "3",
        "5",
        "3",
        "3",
        "4",
        "3",  # 6 sub-scores
        "Title is close but seniority off",
    ]
    with _patch_inputs(inputs):
        label_one(db_with_one_job, "k|t")

    with closing(sqlite3.connect(db_with_one_job)) as conn:
        row = conn.execute(
            "SELECT gold_classification, gold_sub_scores_json, gold_notes, "
            "       gold_labeled_at "
            "FROM jobs WHERE dedup_key='k|t'"
        ).fetchone()
    assert row[0] == "consider"
    sub = json.loads(row[1])
    assert sub == {
        "title_fit": 3,
        "location_fit": 5,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 4,
        "skills_match": 3,
    }
    assert "seniority off" in row[2]
    assert row[3] is not None  # gold_labeled_at populated


def test_label_one_empty_note_stored_as_null(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one

    inputs = ["apply", "5", "5", "5", "5", "5", "5", ""]
    with _patch_inputs(inputs):
        label_one(db_with_one_job, "k|t")
    with closing(sqlite3.connect(db_with_one_job)) as conn:
        note = conn.execute("SELECT gold_notes FROM jobs WHERE dedup_key='k|t'").fetchone()[0]
    assert note is None


def test_label_one_rejects_invalid_classification(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one

    inputs = [
        "invalid",  # bad — should re-prompt
        "consider",
        "3",
        "3",
        "3",
        "3",
        "3",
        "3",
        "",
    ]
    with _patch_inputs(inputs):
        label_one(db_with_one_job, "k|t")
    with closing(sqlite3.connect(db_with_one_job)) as conn:
        cls = conn.execute(
            "SELECT gold_classification FROM jobs WHERE dedup_key='k|t'"
        ).fetchone()[0]
    assert cls == "consider"


def test_label_one_rejects_out_of_range_subscore(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one

    inputs = [
        "apply",
        "6",  # out of 1-5 — should re-prompt
        "5",
        "5",
        "5",
        "5",
        "5",
        "5",
        "",
    ]
    with _patch_inputs(inputs):
        label_one(db_with_one_job, "k|t")
    with closing(sqlite3.connect(db_with_one_job)) as conn:
        sub = json.loads(
            conn.execute("SELECT gold_sub_scores_json FROM jobs WHERE dedup_key='k|t'").fetchone()[
                0
            ]
        )
    assert sub["title_fit"] == 5


def test_label_one_rejects_non_integer_subscore(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one

    inputs = [
        "apply",
        "abc",  # non-integer — should re-prompt
        "4",
        "4",
        "4",
        "4",
        "4",
        "4",
        "",
    ]
    with _patch_inputs(inputs):
        label_one(db_with_one_job, "k|t")
    with closing(sqlite3.connect(db_with_one_job)) as conn:
        sub = json.loads(
            conn.execute("SELECT gold_sub_scores_json FROM jobs WHERE dedup_key='k|t'").fetchone()[
                0
            ]
        )
    assert sub["title_fit"] == 4


def test_label_one_missing_row_returns_without_writing(db_with_one_job):
    """Calling label_one with a dedup_key that doesn't exist is a no-op."""
    from job_finder.scripts.label_gold_set import label_one

    # No prompts because the function bails on the missing row.
    label_one(db_with_one_job, "does|not|exist")

    with closing(sqlite3.connect(db_with_one_job)) as conn:
        # Original row is untouched
        row = conn.execute("SELECT gold_classification FROM jobs WHERE dedup_key='k|t'").fetchone()
    assert row[0] is None
