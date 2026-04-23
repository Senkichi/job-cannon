#!/usr/bin/env python
"""v3.0 rescore gate validator — Phase 34 Plan 4 (CONTEXT D-20).

Reads a rescore-batch-N-report.json and evaluates G1/G2/G3.

Exit codes:
    0  all gates pass (G3 may be 'suppressed' for n<20)
    1  one or more G1/G2/G3 gates failed
    2  validator-internal error (malformed input, etc.)

Gates:
    G1 completeness — every batch row produced an ok or already_scored result.
    G2 monotonicity — higher legacy-sonnet-score quartiles produce >= apply+
                      consider counts than lower quartiles. Loose on B1
                      (n=150) — only a strict reversal from q4 < q1 fails;
                      strict on B2/B3.
    G3 correlation  — Pearson r between legacy sonnet_score and the mean of
                      the new sub_scores ≥ 0.3 (B1) / 0.5 (B2/B3).
                      Suppressed when n<20 per Phase 33 convention.
    G4 production-path refit is enforced separately by
        tests/test_v3_production_path_refit.py — not run from this script.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------


def gate_g1_completeness(report: dict) -> tuple[str, dict]:
    """All batch rows produced an ok or already_scored result."""
    results = report.get("per_row_results", [])
    completed = {"ok", "already_scored"}
    missing = [r for r in results if r.get("status") not in completed]
    if missing:
        return "fail", {
            "count_missing": len(missing),
            "outliers": [
                {
                    "dedup_key": r.get("dedup_key"),
                    "status": r.get("status"),
                    "error": r.get("error"),
                }
                for r in missing[:10]
            ],
        }
    return "pass", {"count_missing": 0, "count_total": len(results)}


def _quartile_for_score(score: float | int | None) -> str:
    s = score or 0
    if s <= 25:
        return "q1"
    if s <= 50:
        return "q2"
    if s <= 75:
        return "q3"
    return "q4"


def gate_g2_monotonicity(
    report: dict, strict: bool, min_bucket_n: int = 5,
) -> tuple[str, dict]:
    """Higher legacy-score quartile -> >= apply+consider RATE.

    Compares rates (not raw counts) because the underlying jobs.sonnet_score
    distribution is heavily skewed -- the upper quartile (76-100) is rare in
    the unclassified pool, so a count comparison would always fail there
    despite the rates being monotonic.

    Buckets with fewer than ``min_bucket_n`` rows are suppressed (n too small
    for a stable rate); the monotonicity check then runs over the remaining
    buckets in ascending-quartile order.
    """
    buckets = {"q1": 0, "q2": 0, "q3": 0, "q4": 0}
    totals = {"q1": 0, "q2": 0, "q3": 0, "q4": 0}
    for r in report.get("per_row_results", []):
        if r.get("status") != "ok":
            continue
        bucket = _quartile_for_score(r.get("legacy_sonnet_score"))
        totals[bucket] += 1
        sub = r.get("new_sub_scores") or {}
        if sub and all(v >= 2 for v in sub.values()):
            buckets[bucket] += 1

    rates: dict[str, float | None] = {}
    valid: list[tuple[str, float]] = []
    for q in ("q1", "q2", "q3", "q4"):
        if totals[q] >= min_bucket_n:
            rate = buckets[q] / totals[q]
            rates[q] = round(rate, 3)
            valid.append((q, rate))
        else:
            rates[q] = None

    evidence_base: dict = {
        "rates_apply_or_consider": rates,
        "buckets_apply_or_consider": buckets,
        "totals": totals,
        "strict": strict,
        "min_bucket_n": min_bucket_n,
    }

    if len(valid) < 2:
        return "suppressed", {
            **evidence_base,
            "reason": f"fewer than 2 buckets with n>={min_bucket_n}",
        }

    rates_seq = [r for _, r in valid]
    if strict:
        monotonic = all(rates_seq[i] <= rates_seq[i + 1] for i in range(len(rates_seq) - 1))
    else:
        # Loose mode: only flag a strict reversal (highest valid bucket rate <
        # lowest valid bucket rate).
        monotonic = rates_seq[-1] >= rates_seq[0]
    return ("pass" if monotonic else "fail"), evidence_base


def gate_g3_correlation(report: dict, batch_number: int) -> tuple[str, dict]:
    """Pearson r between legacy sonnet_score and mean(new sub_scores)."""
    pairs = []
    for r in report.get("per_row_results", []):
        if r.get("status") != "ok":
            continue
        legacy = r.get("legacy_sonnet_score")
        sub = r.get("new_sub_scores") or {}
        if legacy is None or not sub:
            continue
        pairs.append((float(legacy), statistics.mean(sub.values())))
    n = len(pairs)
    if n < 20:
        return "suppressed", {"n": n, "reason": "n<20 per Phase 33 convention"}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    den_y = (sum((y - my) ** 2 for y in ys)) ** 0.5
    r = num / (den_x * den_y) if den_x and den_y else 0.0
    threshold = 0.3 if batch_number == 1 else 0.5
    verdict = "pass" if r >= threshold else "fail"
    return verdict, {"r": round(r, 3), "n": n, "threshold": threshold}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def evaluate_report(report: dict) -> tuple[int, dict]:
    """Run all gates against a parsed report. Returns (exit_code, gates_dict)."""
    batch_number = report.get("batch_number", 1)
    strict = batch_number >= 2

    g1_verdict, g1_evidence = gate_g1_completeness(report)
    g2_verdict, g2_evidence = gate_g2_monotonicity(report, strict)
    g3_verdict, g3_evidence = gate_g3_correlation(report, batch_number)

    exit_code = 0
    if g1_verdict == "fail" or g2_verdict == "fail" or g3_verdict == "fail":
        exit_code = 1

    gates = {
        "g1": {"verdict": g1_verdict, **g1_evidence},
        "g2": {"verdict": g2_verdict, **g2_evidence},
        "g3": {"verdict": g3_verdict, **g3_evidence},
    }
    return exit_code, gates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="v3.0 rescore gate validator (Phase 34 Plan 4)"
    )
    parser.add_argument("--batch-report", required=True)
    args = parser.parse_args(argv)

    path = Path(args.batch_report)
    if not path.exists():
        print(f"ERROR: report not found: {path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: malformed JSON: {exc}", file=sys.stderr)
        return 2

    exit_code, gates = evaluate_report(report)
    n = len(report.get("per_row_results", []))
    print(f"Validating rescore batch {report.get('batch_number')} (n={n})")
    for name in ("g1", "g2", "g3"):
        g = gates[name]
        print(f"  {name.upper()}: {g['verdict'].upper()}  {json.dumps({k: v for k, v in g.items() if k != 'verdict'})}")

    report["gates"] = gates
    path.write_text(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
