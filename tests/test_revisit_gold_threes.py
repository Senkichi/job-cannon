"""Tests for revisit_gold_threes.py — per-axis no-signal tagging."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest


@pytest.fixture
def db_with_labeled_jobs(tmp_db_path):
    """Migrated DB with 3 labeled rows that have varying mixes of 3-valued axes."""
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    try:
        for dedup_key, gold_sub in [
            (
                "k1|t",
                # Two axes scored 3
                {
                    "title_fit": 4,
                    "location_fit": 3,
                    "comp_fit": 3,
                    "domain_match": 4,
                    "seniority_match": 5,
                    "skills_match": 4,
                },
            ),
            (
                "k2|t",
                # No axes scored 3 — should auto-skip and write []
                {
                    "title_fit": 4,
                    "location_fit": 5,
                    "comp_fit": 4,
                    "domain_match": 5,
                    "seniority_match": 5,
                    "skills_match": 4,
                },
            ),
            (
                "k3|t",
                # All six axes scored 3
                {
                    "title_fit": 3,
                    "location_fit": 3,
                    "comp_fit": 3,
                    "domain_match": 3,
                    "seniority_match": 3,
                    "skills_match": 3,
                },
            ),
        ]:
            conn.execute(
                """INSERT INTO jobs
                     (dedup_key, title, company, location, sources, jd_full,
                      classification, sub_scores_json,
                      gold_classification, gold_sub_scores_json,
                      first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedup_key,
                    "T",
                    "C",
                    "Remote",
                    '["linkedin"]',
                    "X" * 2000,
                    "consider",
                    json.dumps(gold_sub),
                    "consider",
                    json.dumps(gold_sub),
                    "2026-04-01T00:00:00",
                    "2026-04-01T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return tmp_db_path


def _patch_inputs(values):
    it = iter(values)
    return patch("builtins.input", lambda *_: next(it))


def test_revisit_one_writes_no_signal_axes_when_some_marked_n(db_with_labeled_jobs):
    from job_finder.scripts.revisit_gold_threes import revisit_one

    # Row k1 has 3s on location_fit and comp_fit. Mark location as midpoint, comp as no-signal.
    inputs = ["m", "n"]
    with _patch_inputs(inputs):
        assert revisit_one(db_with_labeled_jobs, "k1|t") is True

    with closing(sqlite3.connect(db_with_labeled_jobs)) as conn:
        raw = conn.execute(
            "SELECT gold_no_signal_axes FROM jobs WHERE dedup_key='k1|t'"
        ).fetchone()[0]
    assert json.loads(raw) == ["comp_fit"]


def test_revisit_one_writes_empty_list_when_no_3s(db_with_labeled_jobs):
    """Row with zero 3s gets [] written without prompting (auto-skip)."""
    from job_finder.scripts.revisit_gold_threes import revisit_one

    # No prompts because no axis is 3.
    assert revisit_one(db_with_labeled_jobs, "k2|t") is False

    with closing(sqlite3.connect(db_with_labeled_jobs)) as conn:
        raw = conn.execute(
            "SELECT gold_no_signal_axes FROM jobs WHERE dedup_key='k2|t'"
        ).fetchone()[0]
    assert json.loads(raw) == []


def test_revisit_one_all_six_axes_no_signal(db_with_labeled_jobs):
    """Row with all 6 axes at 3, every one marked no-signal."""
    from job_finder.scripts.revisit_gold_threes import revisit_one

    inputs = ["n", "n", "n", "n", "n", "n"]
    with _patch_inputs(inputs):
        revisit_one(db_with_labeled_jobs, "k3|t")

    with closing(sqlite3.connect(db_with_labeled_jobs)) as conn:
        raw = conn.execute(
            "SELECT gold_no_signal_axes FROM jobs WHERE dedup_key='k3|t'"
        ).fetchone()[0]
    saved = set(json.loads(raw))
    expected = {
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
    }
    assert saved == expected


def test_revisit_one_rejects_invalid_response(db_with_labeled_jobs):
    """Bad input re-prompts until 'm' or 'n' is given."""
    from job_finder.scripts.revisit_gold_threes import revisit_one

    # k1 has 2 axes at 3. First answer is bad, then m+n.
    inputs = ["x", "m", "n"]
    with _patch_inputs(inputs):
        revisit_one(db_with_labeled_jobs, "k1|t")

    with closing(sqlite3.connect(db_with_labeled_jobs)) as conn:
        raw = conn.execute(
            "SELECT gold_no_signal_axes FROM jobs WHERE dedup_key='k1|t'"
        ).fetchone()[0]
    assert json.loads(raw) == ["comp_fit"]


def test_revisit_one_missing_row_returns_false(db_with_labeled_jobs):
    from job_finder.scripts.revisit_gold_threes import revisit_one

    assert revisit_one(db_with_labeled_jobs, "does|not|exist") is False


def test_candidate_keys_excludes_already_revisited(db_with_labeled_jobs):
    """Rows with gold_no_signal_axes IS NOT NULL are excluded."""
    from job_finder.scripts.revisit_gold_threes import _candidate_keys

    with closing(sqlite3.connect(db_with_labeled_jobs)) as conn:
        conn.execute("UPDATE jobs SET gold_no_signal_axes = '[]' WHERE dedup_key = 'k2|t'")
        conn.commit()

    keys = _candidate_keys(db_with_labeled_jobs)
    assert "k2|t" not in keys
    assert {"k1|t", "k3|t"} == set(keys)


def test_candidate_keys_excludes_unlabeled_rows(db_with_labeled_jobs):
    """Rows where gold_classification IS NULL are excluded."""
    from job_finder.scripts.revisit_gold_threes import _candidate_keys

    # Insert an unlabeled row
    conn = sqlite3.connect(db_with_labeled_jobs)
    try:
        conn.execute(
            """INSERT INTO jobs
                 (dedup_key, title, company, location,
                  first_seen, last_seen)
               VALUES ('k4|t', 'T', 'C', 'Remote',
                       '2026-04-01', '2026-04-01')""",
        )
        conn.commit()
    finally:
        conn.close()

    keys = _candidate_keys(db_with_labeled_jobs)
    assert "k4|t" not in keys
