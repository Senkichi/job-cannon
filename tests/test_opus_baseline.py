"""Tests for opus_baseline.py — Opus baseline scoring pipeline."""

import json
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from opus_baseline import (
    stratified_sample_jobs,
    call_opus_cli,
    store_opus_score,
    parse_args,
    _parse_model_json,
    SCORE_BUCKETS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_db(jobs: list[dict]) -> sqlite3.Connection:
    """Create an in-memory DB with a jobs table and insert given rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs ("
        "  dedup_key TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  company TEXT NOT NULL,"
        "  location TEXT NOT NULL,"
        "  salary_min INTEGER,"
        "  salary_max INTEGER,"
        "  jd_full TEXT,"
        "  sonnet_score REAL,"
        "  fit_analysis TEXT,"
        "  haiku_score INTEGER,"
        "  opus_score REAL"
        ")"
    )
    for j in jobs:
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                j["dedup_key"], j.get("title", "T"), j.get("company", "C"),
                j.get("location", "L"), j.get("salary_min"), j.get("salary_max"),
                j.get("jd_full", "JD"), j.get("sonnet_score"),
                j.get("fit_analysis", "{}"), j.get("haiku_score"),
                j.get("opus_score"),
            ),
        )
    conn.commit()
    return conn


def _jobs_across_buckets(per_bucket: int = 5) -> list[dict]:
    """Generate jobs spread across all score buckets."""
    jobs = []
    for i, (lo, hi) in enumerate(SCORE_BUCKETS):
        for j in range(per_bucket):
            score = lo + j * ((hi - lo) / max(per_bucket - 1, 1))
            jobs.append({
                "dedup_key": f"k_{i}_{j}",
                "sonnet_score": score,
                "jd_full": f"Job description {i}_{j}",
            })
    return jobs


# ---------------------------------------------------------------------------
# TestStratifiedSampleJobs
# ---------------------------------------------------------------------------

class TestStratifiedSampleJobs:

    def test_equal_distribution(self):
        """Each bucket gets n // 5 jobs."""
        conn = _make_test_db(_jobs_across_buckets(per_bucket=10))
        results = stratified_sample_jobs(conn, 25)
        assert len(results) == 25
        conn.close()

    def test_skip_scored_filters_opus(self):
        """skip_scored=True excludes jobs that already have opus_score."""
        jobs = [
            {"dedup_key": "scored", "sonnet_score": 50.0, "jd_full": "JD", "opus_score": 72.0},
            {"dedup_key": "unscored", "sonnet_score": 55.0, "jd_full": "JD", "opus_score": None},
        ]
        conn = _make_test_db(jobs)
        results = stratified_sample_jobs(conn, 10, skip_scored=True)
        keys = [r["dedup_key"] for r in results]
        assert "scored" not in keys
        assert "unscored" in keys
        conn.close()

    def test_skip_scored_false_includes_all(self):
        """skip_scored=False includes jobs with opus_score."""
        jobs = [
            {"dedup_key": "scored", "sonnet_score": 50.0, "jd_full": "JD", "opus_score": 72.0},
            {"dedup_key": "unscored", "sonnet_score": 55.0, "jd_full": "JD", "opus_score": None},
        ]
        conn = _make_test_db(jobs)
        results = stratified_sample_jobs(conn, 10, skip_scored=False)
        keys = [r["dedup_key"] for r in results]
        assert "scored" in keys
        conn.close()

    def test_empty_database(self):
        """Returns empty list when no qualifying jobs exist."""
        conn = _make_test_db([])
        results = stratified_sample_jobs(conn, 10)
        assert results == []
        conn.close()

    def test_bucket_underflow_fills_from_pool(self):
        """When a bucket has fewer jobs than quota, shortfall is filled."""
        # Only put jobs in the 40-59 bucket
        jobs = [
            {"dedup_key": f"k_{i}", "sonnet_score": 45.0 + i, "jd_full": "JD"}
            for i in range(20)
        ]
        conn = _make_test_db(jobs)
        # Request 10 (2 per bucket), but only 40-59 has jobs
        results = stratified_sample_jobs(conn, 10)
        assert len(results) <= 20  # can't get more than available
        assert len(results) >= 10  # should fill from pool
        conn.close()


# ---------------------------------------------------------------------------
# TestParseModelJson
# ---------------------------------------------------------------------------

class TestParseModelJson:

    def test_direct_json(self):
        """Parses raw JSON string."""
        data, err = _parse_model_json('{"score": 75, "summary": "good"}')
        assert err is None
        assert data["score"] == 75

    def test_markdown_fenced_json(self):
        """Strips ```json fences before parsing."""
        raw = '```json\n{"score": 42, "summary": "ok"}\n```'
        data, err = _parse_model_json(raw)
        assert err is None
        assert data["score"] == 42

    def test_json_in_text(self):
        """Extracts JSON object embedded in text."""
        raw = 'Here is the result: {"score": 88, "summary": "great"} Hope this helps!'
        data, err = _parse_model_json(raw)
        assert err is None
        assert data["score"] == 88

    def test_no_json(self):
        """Returns error when no JSON found."""
        data, err = _parse_model_json("No JSON here at all")
        assert data is None
        assert err is not None


# ---------------------------------------------------------------------------
# TestCallOpusCli
# ---------------------------------------------------------------------------

class TestCallOpusCli:

    def test_successful_call(self):
        """Parses successful claude -p JSON envelope."""
        envelope = {
            "type": "result",
            "is_error": False,
            "result": '{"score": 82, "summary": "strong fit"}',
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(envelope)
        mock_result.stderr = ""

        with patch("opus_baseline.subprocess.run", return_value=mock_result):
            data, err = call_opus_cli("system", "user msg")

        assert err is None
        assert data["score"] == 82

    def test_markdown_fenced_response(self):
        """Handles markdown-fenced JSON in result field."""
        envelope = {
            "type": "result",
            "is_error": False,
            "result": '```json\n{"score": 65, "summary": "partial"}\n```',
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(envelope)

        with patch("opus_baseline.subprocess.run", return_value=mock_result):
            data, err = call_opus_cli("system", "user msg")

        assert err is None
        assert data["score"] == 65

    def test_timeout(self):
        """Returns error on subprocess timeout."""
        import subprocess
        with patch("opus_baseline.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            data, err = call_opus_cli("system", "user msg", timeout_seconds=120)

        assert data is None
        assert "Timeout" in err

    def test_nonzero_exit(self):
        """Returns error on non-zero exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"

        with patch("opus_baseline.subprocess.run", return_value=mock_result):
            data, err = call_opus_cli("system", "user msg")

        assert data is None
        assert "Exit code 1" in err

    def test_claude_error_flag(self):
        """Returns error when envelope has is_error=True."""
        envelope = {
            "type": "result",
            "is_error": True,
            "result": "Not logged in",
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(envelope)

        with patch("opus_baseline.subprocess.run", return_value=mock_result):
            data, err = call_opus_cli("system", "user msg")

        assert data is None
        assert "Claude error" in err


# ---------------------------------------------------------------------------
# TestStoreOpusScore
# ---------------------------------------------------------------------------

class TestStoreOpusScore:

    def test_stores_score(self):
        """Stores opus_score in the correct row."""
        conn = _make_test_db([{"dedup_key": "k1", "sonnet_score": 50.0, "jd_full": "JD"}])
        store_opus_score(conn, "k1", 72.0)
        row = conn.execute("SELECT opus_score FROM jobs WHERE dedup_key = 'k1'").fetchone()
        assert row["opus_score"] == 72.0
        conn.close()

    def test_overwrites_existing(self):
        """Overwrites existing opus_score (idempotent)."""
        conn = _make_test_db([
            {"dedup_key": "k1", "sonnet_score": 50.0, "jd_full": "JD", "opus_score": 60.0},
        ])
        store_opus_score(conn, "k1", 85.0)
        row = conn.execute("SELECT opus_score FROM jobs WHERE dedup_key = 'k1'").fetchone()
        assert row["opus_score"] == 85.0
        conn.close()


# ---------------------------------------------------------------------------
# TestParseArgs
# ---------------------------------------------------------------------------

class TestParseArgs:

    def test_defaults(self):
        args = parse_args([])
        assert args.sample_size == 50
        assert args.batch_size == 10
        assert args.timeout == 120
        assert args.resume is False
        assert args.yes is False
        assert args.no_pause is False

    def test_custom_values(self):
        args = parse_args([
            "--sample-size", "100",
            "--batch-size", "25",
            "--timeout", "60",
            "--resume", "--yes", "--no-pause",
        ])
        assert args.sample_size == 100
        assert args.batch_size == 25
        assert args.timeout == 60
        assert args.resume is True
        assert args.yes is True
        assert args.no_pause is True
