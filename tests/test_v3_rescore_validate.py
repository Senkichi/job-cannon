"""Unit tests for scripts/v3_rescore_validate.py — Phase 34 Plan 4 Commit A.

Each gate function exercised against synthetic report dicts.
"""

from __future__ import annotations

import json
import subprocess
import sys

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
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def _row(legacy: int, mean_sub: float, status: str = "ok") -> dict:
    """Build a per-row result with sub-scores all equal to mean_sub."""
    sub = dict.fromkeys(_DIMS, mean_sub)
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


def _rated_rows(rates: dict[str, tuple[int, int]]) -> list[dict]:
    """rates: {q1: (n_apply_or_consider, n_total), ...}. Builds rows with the right mix."""
    bucket_to_score = {"q1": 12, "q2": 38, "q3": 62, "q4": 88}
    rows = []
    for bucket, (apply_n, total_n) in rates.items():
        score = bucket_to_score[bucket]
        for _ in range(apply_n):
            rows.append(_row(score, 3))  # all sub-scores 3 -> apply-eligible
        for _ in range(total_n - apply_n):
            rows.append(_row(score, 1))  # any sub=1 -> not apply-eligible
    return rows


def test_g2_pass_strict_when_rates_monotonic():
    # Rates: q1=2/10=20%, q2=4/10=40%, q3=6/10=60%, q4=8/10=80% -- strictly monotonic
    rows = _rated_rows({"q1": (2, 10), "q2": (4, 10), "q3": (6, 10), "q4": (8, 10)})
    verdict, evidence = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "pass"
    assert evidence["rates_apply_or_consider"]["q4"] == 0.8


def test_g2_fail_strict_on_rate_reversal():
    # Rates: q1=8/10=80%, q2=3/10=30%, q3=4/10=40%, q4=2/10=20% -- reversed
    rows = _rated_rows({"q1": (8, 10), "q2": (3, 10), "q3": (4, 10), "q4": (2, 10)})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "fail"


def test_g2_loose_passes_minor_rate_dip():
    # q1=20%, q2=40%, q3=30%, q4=50% -- non-strict but q4 > q1
    rows = _rated_rows({"q1": (2, 10), "q2": (4, 10), "q3": (3, 10), "q4": (5, 10)})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=1), strict=False)
    assert verdict == "pass"


def test_g2_loose_fails_q4_below_q1():
    # q1=80%, q4=10% -- loose mode flags this reversal
    rows = _rated_rows({"q1": (8, 10), "q2": (5, 10), "q3": (3, 10), "q4": (1, 10)})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=1), strict=False)
    assert verdict == "fail"


def test_g2_suppresses_buckets_below_min_n():
    # q4 has only 3 ok rows; gate should ignore it. q1..q3 are strictly monotonic by rate.
    rows = _rated_rows({"q1": (2, 10), "q2": (4, 10), "q3": (6, 10), "q4": (3, 3)})
    verdict, evidence = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "pass"
    assert evidence["rates_apply_or_consider"]["q4"] is None
    assert evidence["totals"]["q4"] == 3


def test_g2_suppressed_when_too_few_buckets_meet_min_n():
    # Only q1 has n>=5; everything else suppressed. Need 2+ to evaluate.
    rows = _rated_rows({"q1": (2, 10), "q2": (1, 2), "q3": (1, 1), "q4": (0, 1)})
    verdict, evidence = gate_g2_monotonicity(_report(rows, batch_number=2), strict=True)
    assert verdict == "suppressed"
    assert "fewer than 2 buckets" in evidence["reason"]


def test_g2_strict_tolerates_small_adjacent_inversion():
    # B3 saw q3=92.7% / q4=91.9% -- a 0.8pp inversion within sampling noise
    # for n~37 (SE ~ 4.5pp). Default tolerance is 2pp.
    # Approximate: q3=46/50=92%, q4=37/40=92.5% (no inversion) vs
    # q3=46/50=92%, q4=36/40=90% (2pp inversion -- exactly at boundary).
    rows = _rated_rows({"q1": (40, 50), "q2": (45, 50), "q3": (46, 50), "q4": (36, 40)})
    verdict, evidence = gate_g2_monotonicity(_report(rows, batch_number=3), strict=True)
    # q3 = 92%, q4 = 90% -> 2pp inversion (within default 2pp tolerance) -> pass
    assert verdict == "pass"
    assert evidence["rates_apply_or_consider"]["q3"] == 0.92
    assert evidence["rates_apply_or_consider"]["q4"] == 0.9


def test_g2_strict_fails_inversion_beyond_tolerance():
    # q3 80%, q4 50% -- 30pp inversion, well beyond noise tolerance.
    rows = _rated_rows({"q1": (10, 50), "q2": (20, 50), "q3": (40, 50), "q4": (25, 50)})
    verdict, _ = gate_g2_monotonicity(_report(rows, batch_number=3), strict=True)
    assert verdict == "fail"


def test_g2_skips_rows_with_null_legacy_score():
    # Rows with legacy_sonnet_score=None are dropped (don't pollute q1 bucket).
    rows = _rated_rows({"q1": (4, 5), "q2": (5, 5), "q3": (5, 5), "q4": (5, 5)})
    # Add 20 rows with None legacy score and apply-eligible sub-scores -- if not
    # filtered they would all collide into q1 (since 0 -> q1) and skew totals.
    null_legacy_rows = [
        {
            "dedup_key": f"co|nolegacy-{i}",
            "legacy_sonnet_score": None,
            "new_sub_scores": dict.fromkeys(_DIMS, 4),
            "status": "ok",
        }
        for i in range(20)
    ]
    verdict, evidence = gate_g2_monotonicity(
        _report(rows + null_legacy_rows, batch_number=3),
        strict=True,
    )
    assert verdict == "pass"
    # q1 totals reflect ONLY rows with legacy_sonnet_score != None.
    assert evidence["totals"]["q1"] == 5


# ---------------------------------------------------------------------------
# G3
# ---------------------------------------------------------------------------


def test_g3_suppressed_below_n_20():
    rows = [_row(legacy=i * 5, mean_sub=3) for i in range(15)]
    verdict, evidence = gate_g3_correlation(_report(rows, 1), batch_number=1)
    assert verdict == "suppressed"
    assert evidence["n"] == 15


def test_g3_pass_when_r_above_noise_floor():
    # Construct correlated pairs: legacy 10..100 stepwise, sub-mean tracking.
    rows = []
    for i in range(25):
        legacy = 5 + i * 4
        mean = 1 + (i / 24) * 4  # 1..5
        rows.append(_row(legacy, mean))
    verdict, evidence = gate_g3_correlation(_report(rows, 1), batch_number=1)
    assert verdict == "pass"
    assert evidence["r"] >= 0.20
    assert evidence["threshold"] == 0.20


def test_g3_threshold_uniform_across_batches():
    # B1, B2, B3 all use the noise-floor threshold (0.20). G3 was
    # downgraded from a "matches legacy" test to a "v3 isn't returning
    # noise" test after B2 confirmed cross-paradigm r ~ 0.33 (Phase 33
    # locked decision: v3 ordinal is intentionally a different paradigm
    # than legacy continuous Sonnet).
    rows = []
    for i in range(25):
        rows.append(_row(legacy=5 + i * 4, mean_sub=1 + (i / 24) * 4))
    rep = _report(rows, batch_number=1)
    for batch in (1, 2, 3):
        _, ev = gate_g3_correlation(rep, batch_number=batch)
        assert ev["threshold"] == 0.20


def test_g3_fail_when_r_below_noise_floor():
    # Build an essentially uncorrelated sequence (deterministic) to fail the floor.
    # Reconciliation R2.1 (F-C1.7): seed=11 used to produce r=0.269 ~30% of the
    # time, causing a silent skip. seed=3 deterministically produces r ~= -0.012
    # (verified by sweeping seeds 0-49 with the same uniform sampling), which is
    # comfortably below the 0.20 noise-floor threshold.
    import random

    rng = random.Random(3)
    rows = []
    for _i in range(50):
        legacy = int(rng.uniform(0, 100))  # _row's legacy param is int per its signature
        mean = rng.uniform(1, 5)
        rows.append(_row(legacy, mean))
    verdict, evidence = gate_g3_correlation(_report(rows, 2), batch_number=2)
    assert evidence["r"] < 0.20, (
        f"seed drift: r={evidence['r']} unexpectedly >= 0.20 noise floor; "
        "re-sweep seeds (R2.1 picked 3 from a sweep of 0-49)."
    )
    assert verdict == "fail"


def test_g3_pass_b2_when_r_high():
    rows = []
    for i in range(30):
        legacy = 5 + i * 3
        mean = 1 + (i / 29) * 4
        rows.append(_row(legacy, mean))
    verdict, evidence = gate_g3_correlation(_report(rows, 2), batch_number=2)
    assert verdict == "pass"
    assert evidence["threshold"] == 0.20


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
        [sys.executable, "scripts/v3_rescore_validate.py", "--batch-report", str(report_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    payload = json.loads(report_path.read_text())
    assert "gates" in payload
    assert payload["gates"]["g1"]["verdict"] == "pass"


def test_main_returns_2_on_missing_file(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/v3_rescore_validate.py",
            "--batch-report",
            str(tmp_path / "nonexistent.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "scripts/v3_rescore_validate.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--batch-report" in result.stdout
