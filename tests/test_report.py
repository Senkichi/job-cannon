"""Snapshot-style tests for job_finder.eval.report.

The exact wording is intentionally not asserted (the report wording will
drift); the *structural* properties are: all 7 plan-spec'd sections must
be present, the confusion matrix renders, and per-job rows show flips.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from job_finder.eval.report import write_report

SAMPLE_GOLD_ROW = {
    "dedup_key": "abc",
    "gold_classification": "apply",
    "gold_sub_scores_json": json.dumps(
        {
            "title_fit": 4,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 4,
            "skills_match": 3,
        }
    ),
    "fit_analysis": json.dumps({"strengths": [], "gaps": []}),
    "enrichment_tier": "free",
    "jd_full": "X" * 2000,
    "legitimacy_note": None,
}


def _minimal_metrics():
    return {
        "per_axis": {
            "title_fit": {
                "mae": 0.5,
                "bias": 0.1,
                "icc": 0.7,
                "qw_kappa": 0.6,
                "n_used": 1,
            },
            "location_fit": {
                "mae": 0.0,
                "bias": 0.0,
                "icc": 1.0,
                "qw_kappa": 1.0,
                "n_used": 1,
            },
            "comp_fit": {
                "mae": 1.0,
                "bias": -0.5,
                "icc": 0.5,
                "qw_kappa": 0.4,
                "n_used": 1,
            },
            "domain_match": {
                "mae": 0.0,
                "bias": 0.0,
                "icc": 1.0,
                "qw_kappa": 1.0,
                "n_used": 1,
            },
            "seniority_match": {
                "mae": 0.0,
                "bias": 0.0,
                "icc": 1.0,
                "qw_kappa": 1.0,
                "n_used": 1,
            },
            "skills_match": {
                "mae": 0.0,
                "bias": 0.0,
                "icc": 1.0,
                "qw_kappa": 1.0,
                "n_used": 1,
            },
        },
        "classification": {
            "per_class": {
                "apply": {"precision": 1.0, "recall": 1.0, "f1": 1.0, "support": 1},
                "consider": {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 0},
                "skip": {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 0},
                "reject": {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 0},
                "low_signal": {
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1": 0.0,
                    "support": 0,
                },
                "macro_f1": 0.2,
            },
            "confusion_matrix": {
                c: dict.fromkeys(("apply", "consider", "skip", "reject", "low_signal"), 0)
                for c in ("apply", "consider", "skip", "reject", "low_signal")
            },
            "apply_false_positive_rate": 0.2,
        },
        "coherence": {"violations": [], "rate": 0.0},
        "run_level": {
            "total_calls": 6,
            "failed_calls": 0,
            "schema_adherence": 1.0,
        },
    }


def test_report_has_all_seven_required_sections(tmp_path):
    metrics_out = _minimal_metrics()
    metrics_out["classification"]["confusion_matrix"]["apply"]["apply"] = 1

    per_job_mean = {
        "abc": {
            "title_fit": 4,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 4,
            "skills_match": 3,
        }
    }
    path = write_report(
        run_id="abcdef0123456789",
        variant_name="baseline",
        baseline_run_id=None,
        gold_rows=[SAMPLE_GOLD_ROW],
        per_job_mean=per_job_mean,
        per_job_runs={"abc": [{"sub_scores": per_job_mean["abc"], "status": "ok"}]},
        metrics_out=metrics_out,
        report_dir=str(tmp_path),
        db_path=":memory:",
    )

    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    for section in (
        "Headline",
        "Aggregated Metric Tables",
        "Per-Axis",
        "Confusion Matrix",
        "Per-Job Diff",
        "Cost / Latency",
        "Coherence Violations",
    ):
        assert section in body, f"missing section: {section!r}"


def test_report_marks_classification_flips(tmp_path):
    """A gold=apply row with predicted means that derive to 'consider' must show YES flip."""
    metrics_out = _minimal_metrics()
    # Predicted means roll up to consider (one axis at 2 → all >= 2 but not all >= 3)
    per_job_mean = {
        "abc": {
            "title_fit": 2,
            "location_fit": 5,
            "comp_fit": 3,
            "domain_match": 3,
            "seniority_match": 4,
            "skills_match": 3,
        }
    }
    path = write_report(
        run_id="run00000",
        variant_name="baseline",
        baseline_run_id=None,
        gold_rows=[SAMPLE_GOLD_ROW],
        per_job_mean=per_job_mean,
        per_job_runs={"abc": [{"sub_scores": per_job_mean["abc"], "status": "ok"}]},
        metrics_out=metrics_out,
        report_dir=str(tmp_path),
        db_path=":memory:",
    )

    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "YES" in body
    # title_fit went 4 (gold) → 2 (pred) = -2 delta, must surface
    assert "title_fit-2" in body


def test_report_baseline_delta_renders_when_baseline_id_resolves(tmp_path, tmp_db_path):
    """A/B mode: report shows Δ vs baseline when the run_id resolves in eval_runs."""
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    baseline_metrics = _minimal_metrics()
    baseline_metrics["classification"]["apply_false_positive_rate"] = 0.5
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO eval_runs (run_id, timestamp, variant_name, "
            "gold_set_version, n_runs, metrics_json, per_job_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "deadbeef",
                "2026-04-28T00:00:00Z",
                "baseline",
                "v1",
                3,
                json.dumps(baseline_metrics),
                "{}",
            ),
        )
        conn.commit()

    candidate_metrics = _minimal_metrics()
    candidate_metrics["classification"]["apply_false_positive_rate"] = 0.2
    path = write_report(
        run_id="newrun01",
        variant_name="variant_x",
        baseline_run_id="deadbeef",
        gold_rows=[],
        per_job_mean={},
        per_job_runs={},
        metrics_out=candidate_metrics,
        report_dir=str(tmp_path),
        db_path=tmp_db_path,
    )

    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "BETTER" in body  # 0.2 < 0.5
    assert "Δ" in body
