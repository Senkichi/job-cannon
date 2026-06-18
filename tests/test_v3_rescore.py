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

import pytest

from job_finder.db import JobAssessment
from job_finder.web.job_scorer import ScoringResult
from scripts import v3_rescore
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


# ---------------------------------------------------------------------------
# Scheduler-backfill pause/restore (issue #455)
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal urlopen() context-manager stand-in for /admin/jobs."""

    def __init__(self, body: str):
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _admin_jobs_json(*, include_ingestion: bool = False, **paused: bool) -> str:
    jobs = [{"id": job_id, "paused": paused_state} for job_id, paused_state in paused.items()]
    if include_ingestion:
        # ingestion_poll must always be present-but-ignored.
        jobs.append({"id": "ingestion_poll", "paused": False})
    return json.dumps({"jobs": jobs})


def _make_main_argv(tmp_path) -> list[str]:
    """Build a valid argv pointing main() at a throwaway config + sqlite DB."""
    db_path = tmp_path / "jobs.db"
    sqlite3.connect(str(db_path)).close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db:\n  path: {db_path.as_posix()}\n")
    report = tmp_path / "report.json"
    return [
        "--batch-size",
        "5",
        "--seed",
        "1",
        "--batch-number",
        "1",
        "--report-path",
        str(report),
        "--config-path",
        str(cfg),
    ]


def _stub_batch(monkeypatch, *, run_batch_side_effect=None):
    """Replace the real scoring path so main() does no DB/LLM work."""
    monkeypatch.setattr(v3_rescore, "select_batch_rows", lambda *a, **k: [])

    def _fake_run_batch(*a, **k):
        if run_batch_side_effect is not None:
            raise run_batch_side_effect
        return {"row_count_rescored": 0, "row_count_failed": 0, "wall_clock_seconds": 0.0}

    monkeypatch.setattr(v3_rescore, "run_batch", _fake_run_batch)


def _record_pause_resume(monkeypatch):
    """Capture every _pause / _resume call as (action, job_id) tuples."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(v3_rescore, "_pause", lambda base, jid: calls.append(("pause", jid)))
    monkeypatch.setattr(v3_rescore, "_resume", lambda base, jid: calls.append(("resume", jid)))
    return calls


def test_pause_resume_records_and_restores_prior_state(tmp_path, monkeypatch):
    """Both targets paused; the previously-running one resumed, the other left paused."""
    monkeypatch.setattr(
        v3_rescore,
        "_read_paused_state",
        lambda base: {"enrichment_backfill": False, "agentic_backfill": True},
    )
    calls = _record_pause_resume(monkeypatch)
    _stub_batch(monkeypatch)

    rc = v3_rescore.main(_make_main_argv(tmp_path))

    assert rc == 0
    paused = [jid for action, jid in calls if action == "pause"]
    resumed = [jid for action, jid in calls if action == "resume"]
    assert set(paused) == {"enrichment_backfill", "agentic_backfill"}
    # Only the previously-running job is resumed; the manually-paused one stays paused.
    assert resumed == ["enrichment_backfill"]
    assert "agentic_backfill" not in resumed


def test_crash_mid_rescore_restores_state(tmp_path, monkeypatch):
    """A crash mid-run still restores prior state and re-raises the original error."""
    monkeypatch.setattr(
        v3_rescore,
        "_read_paused_state",
        lambda base: {"enrichment_backfill": False, "agentic_backfill": False},
    )
    calls = _record_pause_resume(monkeypatch)
    boom = RuntimeError("scoring exploded")
    _stub_batch(monkeypatch, run_batch_side_effect=boom)

    with pytest.raises(RuntimeError, match="scoring exploded"):
        v3_rescore.main(_make_main_argv(tmp_path))

    resumed = [jid for action, jid in calls if action == "resume"]
    # Both were running before the crash → both must be resumed despite the failure.
    assert set(resumed) == {"enrichment_backfill", "agentic_backfill"}


def test_ingestion_poll_never_touched(tmp_path, monkeypatch):
    """ingestion_poll is filtered out of prior state and never paused/resumed."""
    body = _admin_jobs_json(
        include_ingestion=True,
        enrichment_backfill=False,
        agentic_backfill=False,
    )
    monkeypatch.setattr(
        v3_rescore.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(body),
    )
    calls = _record_pause_resume(monkeypatch)
    _stub_batch(monkeypatch)

    rc = v3_rescore.main(_make_main_argv(tmp_path))

    assert rc == 0
    touched = {jid for _action, jid in calls}
    assert "ingestion_poll" not in touched
    assert touched == {"enrichment_backfill", "agentic_backfill"}


def test_scheduler_unreachable_proceeds(tmp_path, monkeypatch):
    """A down scheduler yields {} prior state; rescore proceeds and exits 0, no pause/resume."""

    def _boom(*a, **k):
        raise v3_rescore.urllib.error.URLError("connection refused")

    monkeypatch.setattr(v3_rescore.urllib.request, "urlopen", _boom)
    calls = _record_pause_resume(monkeypatch)
    _stub_batch(monkeypatch)

    rc = v3_rescore.main(_make_main_argv(tmp_path))

    assert rc == 0
    assert calls == []
