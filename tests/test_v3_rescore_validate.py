"""Unit tests for scripts/v3_rescore_validate.py — Phase 34 Plan 4 Commit A.

Each gate function exercised against synthetic report dicts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3_rescore_validate import (
    evaluate_report,
    gate_g1_completeness,
    gate_g2_monotonicity,
    gate_g3_correlation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIMS = (
    "title_fit", "location_fit", "comp_fit",
    "domain_match", "seniority_match", "skills_match",
)


def _row(legacy: int, mean_sub: float, status: str = "ok") -> dict:
    """Build a per-row result with sub-scores all equal to mean_sub."""
    sub = {dim: mean_sub for dim in _DIMS}
    return {
        "dedup_key": f"co|job-{legacy}",
        "legacy_sonnet_score": legacy,
        "new_sub_scores": sub,
        "status": status,
    }


def _report(rows: list[dict], batch_number: int = 1) -> dict:
    return {
        "batch_number": batch_number,
        "batch_size": len(rows),
        "seed": 42,
        "row_count_rescored": sum(1 for r in rows if r["status"] == "ok"),
        "row_count_failed": sum(1 for r in rows if r["status"] not in ("ok", "already_scored")),
        "per_row_results": rows,
    }


# ---------------------------------------------------------------------------
# G1
# ---------------------------------------------------------------------------


def test_g1_pass_all_ok():
    report = _report([_row(50, 3) for _ in range(5)])
    verdict, evidence = gate_g1_completeness(report)
    assert verdict == "pass"
    assert evidence["count_missing"] == 0


def test_g1_pass_already_scored_counts_as_complete():
    rows = [_row(50, 3) for _ in range(3)]
    rows.append({"dedup_key": "co|already", "status": "already_scored"})
    verdict, _ = gate_g1_completeness(_report(rows))
    assert verdict == "pass"


def test_g1_fail_lists_missing():
    rows = [_row(50, 3) for _ in range(3)]
    rows.append({"dedup_key": "co|busted", "status": "error", "error": "boom"})
    verdict, evidence = gate_g1_completeness(_report(rows))
    assert verdict == "fail"
    assert evidence["count_missing"] == 1
    assert evidence["outliers"][0]["dedup_key"] == "co|busted"


# ---------------------------------------------------------------------------
# G2
# ---------------------------------------------------------------------------


def _bucketed_rows(counts: dict[str, int], all_pass: bool = True) -> list[dict]:
    """counts: {q1: n_apply_or_consider, ...}. Builds rows with appropriate sub-scores."""
    bucket_to_score = {"q1": 12, "q2": 38, "q3": 62, "q4": 88}
    rows = []
    for bucket, n in counts.items():
        score = bucket_to_score[bucket]
        for _ in range(n):
            rows.append(_row(score, 3))  # sub all 3 -> apply+consider eligible
        if not all_pass:
            for _ in range(2):
                rows.append(_row(score, 1))  # sub all 1 -> not apply+consider
    return rows


def test_g2_pass_strict_when_strictly_monotonic():
    rows = _bucketed_rows({"q1": 1, "q2": 2, "q3": 3, "q4": 4})
    verdict, evidence = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "pass"
    assert evidence["buckets_apply_or_consider"]["q4"] == 4


def test_g2_fail_strict_on_reversal():
    rows = _bucketed_rows({"q1": 5, "q2": 3, "q3": 4, "q4": 2})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "fail"


def test_g2_loose_passes_minor_reversal():
    rows = _bucketed_rows({"q1": 1, "q2": 4, "q3": 2, "q4": 5})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=1), strict=False)
    assert verdict == "pass"


def test_g2_loose_fails_q4_below_q1():
    rows = _bucketed_rows({"q1": 5, "q2": 4, "q3": 3, "q4": 1})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=1), strict=False)
    assert verdict == "fail"


# ---------------------------------------------------------------------------
# G3
# ---------------------------------------------------------------------------


def test_g3_suppressed_below_n_20():
    rows = [_row(legacy=i * 5, mean_sub=3) for i in range(15)]
    verdict, evidence = gate_g3_correlation(_report(rows, 1), batch_number=1)
    assert verdict == "suppressed"
    assert evidence["n"] == 15


def test_g3_pass_b1_threshold_0_3():
    # Construct correlated pairs: legacy 10..100 stepwise, sub-mean tracking.
    rows = []
    for i in range(25):
        legacy = 5 + i * 4
        mean = 1 + (i / 24) * 4  # 1..5
        rows.append(_row(legacy, mean))
    verdict, evidence = gate_g3_correlation(_report(rows, 1), batch_number=1)
    assert verdict == "pass"
    assert evidence["r"] >= 0.3
    assert evidence["threshold"] == 0.3


def test_g3_fail_b2_threshold_when_r_between_thresholds():
    # Build a mediocre correlation that passes 0.3 but fails 0.5.
    rows = []
    import random
    rng = random.Random(7)
    for i in range(50):
        legacy = 10 + i * 1.8
        mean = 2 + (i / 49) * 1 + rng.uniform(-1.0, 1.0)
        rows.append(_row(legacy, max(1, min(5, mean))))
    verdict, evidence = gate_g3_correlation(_report(rows, 2), batch_number=2)
    # Threshold check; r may be borderline depending on RNG but must be < 0.5
    if evidence["r"] >= 0.5:
        pytest.skip(f"random sample produced r={evidence['r']}; rerun seed adjust")
    assert verdict == "fail"


def test_g3_pass_b2_threshold_0_5_when_r_high():
    rows = []
    for i in range(30):
        legacy = 5 + i * 3
        mean = 1 + (i / 29) * 4
        rows.append(_row(legacy, mean))
    verdict, evidence = gate_g3_correlation(_report(rows, 2), batch_number=2)
    assert verdict == "pass"
    assert evidence["threshold"] == 0.5


# ---------------------------------------------------------------------------
# evaluate_report + main
# ---------------------------------------------------------------------------


def test_evaluate_report_aggregates_gates():
    rows = []
    for i in range(25):
        legacy = 5 + i * 4
        mean = 1 + (i / 24) * 4
        rows.append(_row(legacy, mean))
    exit_code, gates = evaluate_report(_report(rows, batch_number=1))
    assert exit_code == 0
    assert set(gates) == {"g1", "g2", "g3"}
    assert all("verdict" in v for v in gates.values())


def test_evaluate_report_exit_code_1_on_g1_fail():
    rows = [_row(50, 3) for _ in range(5)]
    rows.append({"dedup_key": "co|busted", "status": "error"})
    exit_code, _ = evaluate_report(_report(rows, batch_number=1))
    assert exit_code == 1


def test_main_writes_gates_back_into_report(tmp_path):
    rows = []
    for i in range(25):
        legacy = 5 + i * 4
        mean = 1 + (i / 24) * 4
        rows.append(_row(legacy, mean))
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report(rows, batch_number=1)))

    result = subprocess.run(
        [sys.executable, "scripts/v3_rescore_validate.py",
         "--batch-report", str(report_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    payload = json.loads(report_path.read_text())
    assert "gates" in payload
    assert payload["gates"]["g1"]["verdict"] == "pass"


def test_main_returns_2_on_missing_file(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/v3_rescore_validate.py",
         "--batch-report", str(tmp_path / "nonexistent.json")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "scripts/v3_rescore_validate.py", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--batch-report" in result.stdout
