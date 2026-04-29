"""Tests for scripts/run_wholesale_rescore.py.

The driver is the CLI counterpart to _run_batch_bg. Verifies:
  - selects only rows where classification IS NULL and pipeline_status not in dismissed/archived
  - --limit caps the row set
  - score_fn is invoked once per scorable row
  - excluded rows are auto-dismissed and not scored
  - per-row exceptions are caught and counted as errored

We inject a fake score_fn so no LLM calls happen. The exclusion filter,
candidate context, and DB iteration are exercised end-to-end against a
fully migrated temp DB.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def db_with_unscored_rows(migrated_db):
    """Yield (path, conn) with three discovered+unscored rows of varying titles."""
    path, conn = migrated_db

    rows = [
        # (dedup_key, title, company, pipeline_status, classification)
        ("k|1", "Senior ML Engineer", "Acme", "discovered", None),
        ("k|2", "Frontend Developer", "Beta", "discovered", None),
        ("k|3", "Already Classified", "Gamma", "discovered", "apply"),  # filtered out
        ("k|4", "Dismissed Job", "Delta", "dismissed", None),  # filtered out by predicate
        ("k|5", "Data Scientist", "Epsilon", "discovered", None),
    ]
    for k, title, company, ps, cls in rows:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, pipeline_status, classification, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            (k, title, company, "Remote", ps, cls),
        )
    conn.commit()
    yield path, conn


def _fake_score_fn_factory():
    """Returns (fn, calls) where calls is a list mutated on each invocation."""
    calls: list[dict] = []

    def fn(job_row, conn, config, *, candidate_context=None):
        calls.append({"dedup_key": job_row["dedup_key"], "title": job_row.get("title")})
        # Mimic the persistence side-effect so a re-query for IS NULL skips this row.
        conn.execute(
            "UPDATE jobs SET classification = 'consider' WHERE dedup_key = ?",
            (job_row["dedup_key"],),
        )
        return object()  # non-None means scored

    return fn, calls


def test_only_unscored_non_dismissed_rows_are_processed(db_with_unscored_rows):
    from scripts.run_wholesale_rescore import run_rescore

    path, _ = db_with_unscored_rows
    score_fn, calls = _fake_score_fn_factory()

    summary = run_rescore(path, config={}, score_fn=score_fn)

    # k|1, k|2, k|5 are eligible; k|3 (already classified) and k|4 (dismissed) are not.
    assert summary["total"] == 3
    assert summary["scored"] == 3
    assert summary["errored"] == 0
    assert {c["dedup_key"] for c in calls} == {"k|1", "k|2", "k|5"}


def test_limit_caps_the_row_set(db_with_unscored_rows):
    from scripts.run_wholesale_rescore import run_rescore

    path, _ = db_with_unscored_rows
    score_fn, calls = _fake_score_fn_factory()

    summary = run_rescore(path, config={}, limit=1, score_fn=score_fn)

    assert summary["total"] == 1
    assert len(calls) == 1


def test_per_row_errors_are_counted_not_fatal(db_with_unscored_rows):
    from scripts.run_wholesale_rescore import run_rescore

    path, _ = db_with_unscored_rows

    def explode_on_first(job_row, conn, config, *, candidate_context=None):
        if job_row["dedup_key"] == "k|1":
            raise RuntimeError("simulated provider failure")
        conn.execute(
            "UPDATE jobs SET classification = 'consider' WHERE dedup_key = ?",
            (job_row["dedup_key"],),
        )
        return object()

    summary = run_rescore(path, config={}, score_fn=explode_on_first)

    assert summary["total"] == 3
    assert summary["scored"] == 2
    assert summary["errored"] == 1


def test_excluded_rows_are_auto_dismissed_not_scored(db_with_unscored_rows):
    from scripts.run_wholesale_rescore import run_rescore

    path, _ = db_with_unscored_rows
    score_fn, calls = _fake_score_fn_factory()

    # Exclude any row whose title contains "Frontend" — should hit k|2.
    config = {"profile": {"exclusions": {"title_keywords": ["Frontend"]}}}
    summary = run_rescore(path, config=config, score_fn=score_fn)

    assert summary["scored"] == 2
    assert summary["excluded"] == 1
    assert "k|2" not in {c["dedup_key"] for c in calls}

    # Verify k|2 was auto-dismissed (matches batch worker behavior).
    conn = sqlite3.connect(path)
    status = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key = 'k|2'").fetchone()[0]
    conn.close()
    assert status == "dismissed"
