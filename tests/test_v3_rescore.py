"""Unit tests for scripts/v3_rescore.py — Phase 34 Plan 4 Commit A.

Covers:
    - select_batch_rows stratified sampling, seeded determinism, distinct-seed
      distinct-rows behavior.
    - run_batch row iteration: invokes score_job per row, persists via
      persist_job_assessment, writes per-row report, skips already-scored
      rows, handles error / skipped status.
    - Report JSON shape on disk.
    - CLI argparse smoke (--help exits 0).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from job_finder.db import JobAssessment
from job_finder.web.job_scorer import ScoringResult
from scripts.v3_rescore import run_batch, select_batch_rows

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_jobs_db(num_rows: int = 40) -> sqlite3.Connection:
    """In-memory jobs table seeded with stratified sonnet_score values.

    Spreads sonnet_score evenly 1..100 so NTILE(4) yields 4 quartiles of
    equal size when num_rows is a multiple of 4.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            sonnet_score REAL,
            jd_full TEXT,
            classification TEXT,
            sub_scores_json TEXT,
            fit_analysis TEXT,
            scoring_provider TEXT,
            scoring_model TEXT,
            legitimacy_note TEXT,
            enrichment_tier TEXT
        )
        """
    )
    long_jd = "x" * 500
    for i in range(num_rows):
        score = (i + 1) * (100 / num_rows)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, sonnet_score, jd_full) VALUES (?, ?, ?, ?)",
            (f"co|job-{i:03d}", f"Job {i}", score, long_jd),
        )
    conn.commit()
    return conn


def _good_assessment() -> JobAssessment:
    return JobAssessment(
        sub_scores={
            "title_fit": 4,
            "location_fit": 3,
            "comp_fit": 4,
            "domain_match": 5,
            "seniority_match": 4,
            "skills_match": 3,
        },
        classification="",
        rationale={
            "strengths": [],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        },
        provider="ollama",
    )


# ---------------------------------------------------------------------------
# select_batch_rows
# ---------------------------------------------------------------------------


def test_select_batch_rows_returns_requested_count():
    conn = _make_jobs_db(num_rows=40)
    keys = select_batch_rows(conn, batch_size=12, seed=1)
    assert len(keys) == 12


def test_select_batch_rows_stratified_across_quartiles():
    conn = _make_jobs_db(num_rows=40)
    keys = select_batch_rows(conn, batch_size=12, seed=1)
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT dedup_key, sonnet_score FROM jobs WHERE dedup_key IN ({placeholders})",
        keys,
    ).fetchall()
    buckets = {"q1": 0, "q2": 0, "q3": 0, "q4": 0}
    for r in rows:
        s = r["sonnet_score"]
        b = "q1" if s <= 25 else "q2" if s <= 50 else "q3" if s <= 75 else "q4"
        buckets[b] += 1
    assert min(buckets.values()) >= 1, f"empty quartile in {buckets}"


def test_select_batch_rows_deterministic_for_same_seed():
    conn = _make_jobs_db(num_rows=40)
    keys_a = select_batch_rows(conn, batch_size=12, seed=42)
    keys_b = select_batch_rows(conn, batch_size=12, seed=42)
    assert keys_a == keys_b


def test_select_batch_rows_distinct_seeds_yield_different_rows():
    conn = _make_jobs_db(num_rows=40)
    keys_a = set(select_batch_rows(conn, batch_size=12, seed=1))
    keys_b = set(select_batch_rows(conn, batch_size=12, seed=999))
    overlap = keys_a & keys_b
    # Some overlap is fine; not an exact match.
    assert keys_a != keys_b, "distinct seeds produced identical row sets"
    assert len(overlap) < len(keys_a), "all rows overlapped — seed had no effect"


def test_select_batch_rows_excludes_already_rescored():
    conn = _make_jobs_db(num_rows=40)
    conn.execute("UPDATE jobs SET classification = 'apply' WHERE rowid <= 10")
    conn.commit()
    keys = select_batch_rows(conn, batch_size=12, seed=1)
    placeholders = ",".join("?" * len(keys))
    classifications = [
        r["classification"]
        for r in conn.execute(
            f"SELECT classification FROM jobs WHERE dedup_key IN ({placeholders})",
            keys,
        ).fetchall()
    ]
    assert all(c is None for c in classifications)


def test_select_batch_rows_force_returns_already_scored_when_no_excluded():
    conn = _make_jobs_db(num_rows=40)
    conn.execute("UPDATE jobs SET classification = 'apply' WHERE rowid <= 10")
    conn.commit()
    keys = select_batch_rows(conn, batch_size=12, seed=1, exclude_rescored=False)
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT classification FROM jobs WHERE dedup_key IN ({placeholders})",
        keys,
    ).fetchall()
    classified = sum(1 for r in rows if r["classification"] is not None)
    assert classified > 0


def test_select_batch_rows_excludes_no_sonnet_by_default():
    """sonnet_score IS NULL rows are excluded from default stratified pool."""
    conn = _make_jobs_db(num_rows=40)
    # Set 5 rows' sonnet_score to NULL.
    conn.execute("UPDATE jobs SET sonnet_score = NULL WHERE rowid <= 5")
    conn.commit()
    keys = select_batch_rows(conn, batch_size=20, seed=1)
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT sonnet_score FROM jobs WHERE dedup_key IN ({placeholders})",
        keys,
    ).fetchall()
    assert all(r["sonnet_score"] is not None for r in rows)


def test_select_batch_rows_include_no_sonnet_picks_them_up():
    """include_no_sonnet=True surfaces sonnet-NULL rows in a synthetic q0."""
    conn = _make_jobs_db(num_rows=20)
    # Insert 8 rows with NULL sonnet_score (simulating leftover pool).
    long_jd = "x" * 500
    for i in range(8):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, jd_full) VALUES (?, ?, ?)",
            (f"co|leftover-{i:03d}", f"Leftover {i}", long_jd),
        )
    conn.commit()
    keys = select_batch_rows(conn, batch_size=20, seed=1, include_no_sonnet=True)
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT sonnet_score FROM jobs WHERE dedup_key IN ({placeholders})",
        keys,
    ).fetchall()
    null_count = sum(1 for r in rows if r["sonnet_score"] is None)
    assert null_count > 0


# ---------------------------------------------------------------------------
# run_batch
# ---------------------------------------------------------------------------


def _ok_result() -> ScoringResult:
    return ScoringResult(status="ok", data=_good_assessment(), provider="ollama")


def test_run_batch_persists_each_row(tmp_path):
    conn = _make_jobs_db(num_rows=8)
    keys = [f"co|job-{i:03d}" for i in range(4)]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    with patch("scripts.v3_rescore.score_job", return_value=_ok_result()) as mock_score:
        report = run_batch(
            conn,
            config,
            keys,
            report_path,
            batch_number=1,
            seed=42,
        )

    assert mock_score.call_count == 4
    assert report["row_count_rescored"] == 4
    assert report["row_count_failed"] == 0
    rows = conn.execute(
        "SELECT classification FROM jobs WHERE dedup_key = ?", (keys[0],)
    ).fetchone()
    assert rows[0] in {"apply", "consider", "skip", "reject"}


def test_run_batch_writes_report_with_per_row_results(tmp_path):
    conn = _make_jobs_db(num_rows=8)
    keys = [f"co|job-{i:03d}" for i in range(3)]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    with patch("scripts.v3_rescore.score_job", return_value=_ok_result()):
        run_batch(conn, config, keys, report_path, batch_number=1, seed=42)

    payload = json.loads(report_path.read_text())
    assert payload["batch_number"] == 1
    assert payload["seed"] == 42
    assert len(payload["per_row_results"]) == 3
    first = payload["per_row_results"][0]
    assert {
        "dedup_key",
        "legacy_sonnet_score",
        "new_sub_scores",
        "provider",
        "model",
        "status",
    } <= set(first)


def test_run_batch_skips_already_classified_rows(tmp_path):
    conn = _make_jobs_db(num_rows=8)
    conn.execute("UPDATE jobs SET classification = 'apply' WHERE dedup_key = 'co|job-001'")
    conn.commit()
    keys = ["co|job-000", "co|job-001", "co|job-002"]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    with patch("scripts.v3_rescore.score_job", return_value=_ok_result()) as mock_score:
        report = run_batch(conn, config, keys, report_path, batch_number=1, seed=42)

    assert mock_score.call_count == 2  # job-001 was skipped
    statuses = [r["status"] for r in report["per_row_results"]]
    assert "already_scored" in statuses


def test_run_batch_records_error_status(tmp_path):
    conn = _make_jobs_db(num_rows=4)
    keys = ["co|job-000"]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    err_result = ScoringResult(status="error", data=None, provider="ollama", error="boom")
    with patch("scripts.v3_rescore.score_job", return_value=err_result):
        report = run_batch(conn, config, keys, report_path, batch_number=1, seed=42)

    assert report["row_count_rescored"] == 0
    assert report["row_count_failed"] == 1
    assert report["per_row_results"][0]["status"] == "error"
    assert report["per_row_results"][0]["error"] == "boom"


def test_run_batch_skipped_status_no_persist(tmp_path):
    conn = _make_jobs_db(num_rows=4)
    keys = ["co|job-000"]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    skipped = ScoringResult(status="skipped", data=None)
    with patch("scripts.v3_rescore.score_job", return_value=skipped):
        report = run_batch(conn, config, keys, report_path, batch_number=1, seed=42)

    assert report["per_row_results"][0]["status"] == "skipped"
    classification = conn.execute(
        "SELECT classification FROM jobs WHERE dedup_key = ?", (keys[0],)
    ).fetchone()[0]
    assert classification is None


def test_run_batch_force_rescore_overwrites(tmp_path):
    conn = _make_jobs_db(num_rows=4)
    conn.execute("UPDATE jobs SET classification = 'apply' WHERE dedup_key = 'co|job-000'")
    conn.commit()
    keys = ["co|job-000"]
    config = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
    report_path = tmp_path / "report.json"

    with patch("scripts.v3_rescore.score_job", return_value=_ok_result()) as mock_score:
        report = run_batch(
            conn,
            config,
            keys,
            report_path,
            batch_number=1,
            seed=42,
            force=True,
        )
    assert mock_score.call_count == 1
    assert report["per_row_results"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# CLI argparse smoke
# ---------------------------------------------------------------------------


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "scripts/v3_rescore.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--batch-size" in result.stdout
    assert "--seed" in result.stdout
    assert "--report-path" in result.stdout
    assert "--batch-number" in result.stdout
