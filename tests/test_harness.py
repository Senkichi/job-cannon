"""End-to-end test for the eval harness on a synthetic gold set.

The harness exercises real DB I/O, real metric computation, and real report
emission — only the scoring boundary is mocked (via _score_one). A run with
3 controlled gold rows runs in <1s and proves:

    - eval_runs row is persisted with the right shape
    - metrics_json contains per-axis / classification / coherence / run_level
    - the report file is created in the requested directory
    - report_path on the eval_runs row is populated post-write
    - no-signal axes are honored (one axis is tagged on one row, the others
      remain comparable)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from job_finder.web.db_migrate import run_migrations

GOLD_SUB_SCORES = {
    "title_fit": 4,
    "location_fit": 5,
    "comp_fit": 3,
    "domain_match": 3,
    "seniority_match": 4,
    "skills_match": 3,
}


def _insert_gold_row(
    conn: sqlite3.Connection,
    *,
    dedup_key: str,
    gold_classification: str,
    gold_sub_scores: dict,
    no_signal_axes: list[str] | None = None,
    enrichment_tier: str = "free",
    jd_full: str | None = None,
) -> None:
    """Insert a single labeled gold row using the migrated schema."""
    conn.execute(
        """
        INSERT INTO jobs
          (dedup_key, title, company, location, sources, source_urls, source_id,
           jd_full, first_seen, last_seen,
           classification, sub_scores_json, fit_analysis,
           gold_classification, gold_sub_scores_json, gold_notes,
           gold_no_signal_axes, gold_labeled_at, enrichment_tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            f"Title for {dedup_key}",
            f"Company {dedup_key}",
            "Remote",
            json.dumps(["test"]),
            json.dumps([]),
            "",
            jd_full or ("X" * 2000),  # >low_signal_threshold (1500)
            "2026-04-28T00:00:00Z",
            "2026-04-28T00:00:00Z",
            None,
            None,
            json.dumps(
                {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                }
            ),
            gold_classification,
            json.dumps(gold_sub_scores),
            "",
            json.dumps(no_signal_axes) if no_signal_axes is not None else None,
            "2026-04-28T00:00:00Z",
            enrichment_tier,
        ),
    )


@pytest.fixture
def gold_db(tmp_db_path):
    """Migrated DB with 3 labeled gold rows."""
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        _insert_gold_row(
            conn,
            dedup_key="job-a",
            gold_classification="apply",
            gold_sub_scores=GOLD_SUB_SCORES,
        )
        _insert_gold_row(
            conn,
            dedup_key="job-b",
            gold_classification="consider",
            gold_sub_scores={**GOLD_SUB_SCORES, "title_fit": 3},
            no_signal_axes=["comp_fit"],
        )
        _insert_gold_row(
            conn,
            dedup_key="job-c",
            gold_classification="skip",
            gold_sub_scores={**GOLD_SUB_SCORES, "title_fit": 2, "skills_match": 2},
        )
        conn.commit()
    return tmp_db_path


def _make_fake_score(sub_scores: dict, status: str = "ok"):
    """Build a SimpleNamespace shaped like ScoringResult."""
    return SimpleNamespace(
        status=status,
        data=SimpleNamespace(
            sub_scores=sub_scores,
            classification="",
            rationale={
                "strengths": ["a strength"],
                "gaps": ["nothing notable"],
                "talking_points": [],
                "resume_priority_skills": [],
            },
            provider="ollama",
        ),
        provider="ollama",
        error=None,
    )


def test_harness_diagnose_mode_writes_report_and_persists_run(gold_db, tmp_path, monkeypatch):
    from job_finder.eval import harness

    def fake_score_one(row, conn, config, candidate_context):
        # Match gold for job-a, off-by-one for the others
        if row["dedup_key"] == "job-a":
            return _make_fake_score(dict(GOLD_SUB_SCORES))
        return _make_fake_score({k: max(1, v - 1) for k, v in GOLD_SUB_SCORES.items()})

    monkeypatch.setattr(harness, "_score_one", fake_score_one)

    report_path = harness.run(
        db_path=gold_db,
        variant_name="baseline",
        n_runs=2,
        report_dir=str(tmp_path),
        config={},
    )

    # Report file written
    assert report_path
    from pathlib import Path

    assert Path(report_path).exists()

    # eval_runs row exists with the expected shape
    with closing(sqlite3.connect(gold_db)) as conn:
        rows = conn.execute(
            "SELECT run_id, variant_name, n_runs, gold_set_version, "
            "metrics_json, per_job_json, report_path "
            "FROM eval_runs"
        ).fetchall()
    assert len(rows) == 1
    run_id, variant_name, n_runs, gsv, metrics_json, per_job_json, persisted_report = rows[0]
    assert variant_name == "baseline"
    assert n_runs == 2
    assert gsv  # any non-empty version sentinel
    metrics_out = json.loads(metrics_json)
    assert "per_axis" in metrics_out
    assert "classification" in metrics_out
    assert "coherence" in metrics_out
    assert "run_level" in metrics_out
    # comp_fit had one no-signal row dropped → n_used should be 2 (rows a, c)
    assert metrics_out["per_axis"]["comp_fit"]["n_used"] == 2
    # Other axes saw all 3 rows
    assert metrics_out["per_axis"]["title_fit"]["n_used"] == 3
    # Run-level: 3 rows × 2 runs = 6 calls, all ok
    assert metrics_out["run_level"]["total_calls"] == 6
    assert metrics_out["run_level"]["failed_calls"] == 0
    # Per-job runs JSON is keyed by dedup_key with n_runs entries each
    per_job = json.loads(per_job_json)
    assert set(per_job.keys()) == {"job-a", "job-b", "job-c"}
    assert all(len(v) == 2 for v in per_job.values())
    # report_path was updated post-write
    assert persisted_report == report_path


def test_harness_records_failed_scorer_calls(gold_db, tmp_path, monkeypatch):
    """Scorer errors must be counted in run_level.failed_calls and not abort the run."""
    from job_finder.eval import harness

    def fake_score_one(row, conn, config, candidate_context):
        if row["dedup_key"] == "job-b":
            return SimpleNamespace(status="error", data=None, provider=None, error="boom")
        return _make_fake_score(dict(GOLD_SUB_SCORES))

    monkeypatch.setattr(harness, "_score_one", fake_score_one)

    harness.run(
        db_path=gold_db,
        variant_name="baseline",
        n_runs=1,
        report_dir=str(tmp_path),
        config={},
    )

    with closing(sqlite3.connect(gold_db)) as conn:
        metrics_json = conn.execute("SELECT metrics_json FROM eval_runs").fetchone()[0]
    metrics_out = json.loads(metrics_json)
    # 3 rows × 1 run = 3 calls; one failed
    assert metrics_out["run_level"]["total_calls"] == 3
    assert metrics_out["run_level"]["failed_calls"] == 1


def test_harness_handles_scorer_exception(gold_db, tmp_path, monkeypatch):
    """A raised exception in the scorer is captured as an error entry, not a crash."""
    from job_finder.eval import harness

    def fake_score_one(row, conn, config, candidate_context):
        if row["dedup_key"] == "job-c":
            raise RuntimeError("simulated dispatcher crash")
        return _make_fake_score(dict(GOLD_SUB_SCORES))

    monkeypatch.setattr(harness, "_score_one", fake_score_one)

    report_path = harness.run(
        db_path=gold_db,
        variant_name="baseline",
        n_runs=1,
        report_dir=str(tmp_path),
        config={},
    )
    assert report_path

    with closing(sqlite3.connect(gold_db)) as conn:
        metrics_json = conn.execute("SELECT metrics_json FROM eval_runs").fetchone()[0]
    metrics_out = json.loads(metrics_json)
    assert metrics_out["run_level"]["failed_calls"] == 1
