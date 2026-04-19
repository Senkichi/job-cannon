"""Re-fit Ollama cascade calibration tables against Anthropic baselines.

Addresses the gap captured in .planning/CALIBRATION_REFIT_PLAN.md:
  * The shipped Sonnet table was fit against `fewshot-comparative` on
    2026-03-29. Production now runs `fewshot`, so the table undershoots.
  * The Haiku tier has no calibration table at all — the wiring no-ops
    until `calibration_ollama_haiku.json` is dropped alongside the Sonnet one.

This script refits both tiers by sampling jobs with `scoring_provider =
'anthropic'` (so the baseline pool is NOT contaminated by post-cascade-flip
Ollama writes), calling the production scorer with Ollama forced, then
fitting a monotone calibration curve via Pool Adjacent Violators.

Isotonic regression is implemented inline — adding sklearn as a runtime
dep just for offline calibration fitting is not warranted.

Usage:
    uv run --active python scripts/calibration_refit.py --tier sonnet --n 30
    uv run --active python scripts/calibration_refit.py --tier haiku  --n 30
    uv run --active python scripts/calibration_refit.py --tier both   --n 30
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from job_finder.config import load_config
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_orchestrator import load_scoring_profile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider forcing
# ---------------------------------------------------------------------------


def force_ollama(config: dict, tier: str, model: str = "qwen2.5:14b") -> dict:
    """Return a config copy routing `tier` through Ollama only (no fallback).

    Identical shape to scripts/quality_cascade_validator.force_ollama — kept
    inline here so this script stays self-contained.
    """
    out = deepcopy(config)
    out.setdefault("providers", {})[tier] = {
        "provider": "ollama",
        "model": model,
        "fallback_chain": [],
    }
    return out


# ---------------------------------------------------------------------------
# Sampling: anthropic-only baselines
# ---------------------------------------------------------------------------

_BASELINE_COL = {"sonnet": "sonnet_score", "haiku": "haiku_score"}


def sample_anthropic_baseline(conn, tier: str, n: int) -> list[dict]:
    """Sample `n` random jobs with anthropic-origin baseline for `tier`.

    Scopes to `scoring_provider = 'anthropic'` because a significant fraction
    of existing `sonnet_score`/`haiku_score` rows were written by the Ollama
    cascade post-flip. Fitting against mixed-origin baselines would make the
    calibration converge to "Ollama agrees with itself" — a vacuous fit.
    """
    col = _BASELINE_COL[tier]
    rows = conn.execute(
        f"SELECT * FROM jobs "
        f"WHERE {col} IS NOT NULL "
        f"  AND jd_full IS NOT NULL "
        f"  AND scoring_provider = 'anthropic' "
        f"ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Production scorer routing
# ---------------------------------------------------------------------------


def call_production_scorer(job: dict, profile: dict, conn, cfg: dict, tier: str):
    """Route through the production scorer so calibration matches prod prompts."""
    if tier == "sonnet":
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        return evaluate_job_sonnet(job, profile, conn, cfg)
    from job_finder.web.haiku_scorer import score_job_haiku
    return score_job_haiku(job, profile, conn, cfg)


# ---------------------------------------------------------------------------
# Isotonic regression via Pool Adjacent Violators (PAV)
# ---------------------------------------------------------------------------


def pav_isotonic(
    pairs: Iterable[tuple[float, float]],
    y_min: float = 0.0,
    y_max: float = 100.0,
) -> list[list[float]]:
    """Fit a monotone non-decreasing step function via PAV.

    Returns breakpoints as `[[x, y_fitted], ...]` sorted by x. Each unique x
    in the input emits one breakpoint at its PAV-pooled y, clipped to
    `[y_min, y_max]`. Downstream consumers (score_calibration._interpolate)
    linearly interpolate between adjacent breakpoints and clamp to endpoints
    for out-of-range queries.
    """
    by_x: dict[float, list[float]] = {}
    for x, y in pairs:
        by_x.setdefault(float(x), []).append(float(y))
    if not by_x:
        return []

    xs = sorted(by_x.keys())
    ys = [statistics.fmean(by_x[x]) for x in xs]
    ws = [len(by_x[x]) for x in xs]

    # Groups: [start_idx, end_idx, pooled_y, total_weight]. Merge while a
    # violation (group[i].y > group[i+1].y) exists.
    groups: list[list[float]] = [[float(i), float(i), ys[i], float(ws[i])] for i in range(len(xs))]
    i = 0
    while i < len(groups) - 1:
        if groups[i][2] > groups[i + 1][2]:
            a, b = groups[i], groups[i + 1]
            merged_y = (a[2] * a[3] + b[2] * b[3]) / (a[3] + b[3])
            merged_w = a[3] + b[3]
            groups[i:i + 2] = [[a[0], b[1], merged_y, merged_w]]
            if i > 0:
                i -= 1
        else:
            i += 1

    fitted = [0.0] * len(xs)
    for start, end, yval, _w in groups:
        clipped = max(y_min, min(y_max, yval))
        for k in range(int(start), int(end) + 1):
            fitted[k] = clipped
    return [[xs[i], round(fitted[i], 4)] for i in range(len(xs))]


def predict(score: float, breakpoints: list[list[float]]) -> float:
    """Linear interpolation between breakpoints; clamp to endpoints.

    Mirrors score_calibration._interpolate so the pre/post metrics reported
    by this script match what production will compute at runtime.
    """
    if score <= breakpoints[0][0]:
        return breakpoints[0][1]
    if score >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= score <= x1:
            t = (score - x0) / (x1 - x0) if x1 != x0 else 0.0
            return y0 + t * (y1 - y0)
    return float(score)


# ---------------------------------------------------------------------------
# Refit orchestrator
# ---------------------------------------------------------------------------


def refit_tier(
    tier: str,
    n: int,
    config: dict,
    profile: dict,
    conn,
    model: str,
    prompt_variant: str,
) -> dict | None:
    """Run an end-to-end refit for a single tier. Returns the written table."""
    print(f"\n=== {tier.upper()} refit ===", flush=True)
    jobs = sample_anthropic_baseline(conn, tier, n)
    if len(jobs) < 10:
        print(f"ABORT: only {len(jobs)} anthropic-baseline jobs available.")
        return None
    print(f"Sampled {len(jobs)} jobs (scoring_provider='anthropic').")

    ollama_cfg = force_ollama(config, tier, model=model)
    pairs: list[tuple[float, float]] = []
    t0 = time.perf_counter()

    baseline_col = _BASELINE_COL[tier]

    for i, job in enumerate(jobs, start=1):
        call_start = time.perf_counter()
        try:
            result = call_production_scorer(job, profile, conn, ollama_cfg, tier)
        except Exception as exc:
            print(f"  [{i}/{len(jobs)}] {job.get('title', '?')[:50]!r}: ERROR {exc!r}")
            continue
        latency = time.perf_counter() - call_start

        raw = result.data.get("score") if result.data else None
        baseline = job.get(baseline_col)
        if raw is None or baseline is None:
            print(
                f"  [{i}/{len(jobs)}] {job.get('title', '?')[:50]!r}: "
                f"SKIP raw={raw} base={baseline}"
            )
            continue
        pairs.append((float(raw), float(baseline)))
        print(
            f"  [{i}/{len(jobs)}] raw={raw:>5.1f} base={baseline:>5.1f} "
            f"delta={float(raw) - float(baseline):+5.1f} ({latency:.1f}s) "
            f"{job.get('title', '?')[:40]}"
        )

    elapsed = time.perf_counter() - t0
    print(f"\nCollected {len(pairs)} pairs in {elapsed:.0f}s.")
    if len(pairs) < 10:
        print("ABORT: too few valid pairs for a stable fit.")
        return None

    breakpoints = pav_isotonic(pairs)
    X = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    calibrated = [predict(x, breakpoints) for x in X]

    mae_before = statistics.fmean(abs(x - yi) for x, yi in zip(X, y))
    bias_before = statistics.fmean(x - yi for x, yi in zip(X, y))
    mae_after = statistics.fmean(abs(c - yi) for c, yi in zip(calibrated, y))
    bias_after = statistics.fmean(c - yi for c, yi in zip(calibrated, y))

    # Audit trail: write raw pairs so a future refit or review can recompute.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    pairs_path = Path(f"eval_results/refit_{tier}_{ts}.json")
    pairs_path.parent.mkdir(parents=True, exist_ok=True)
    pairs_path.write_text(
        json.dumps(
            {
                "tier": tier,
                "provider": "ollama",
                "model": model,
                "prompt_variant": prompt_variant,
                "baseline_provider": "anthropic",
                "sampled": len(jobs),
                "collected": len(pairs),
                "elapsed_seconds": round(elapsed, 1),
                "pairs": pairs,
                "job_ids": [j["dedup_key"] for j in jobs],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    table = {
        "provider": "ollama",
        "tier": tier,
        "model": model,
        "prompt_variant": prompt_variant,
        "baseline_provider": "anthropic",
        "eval_source": str(pairs_path),
        "n_pairs": len(pairs),
        "breakpoints": breakpoints,
        "mae_before": round(mae_before, 3),
        "mae_after": round(mae_after, 3),
        "bias_before": round(bias_before, 3),
        "bias_after": round(bias_after, 3),
        "fit_timestamp": ts,
    }

    out_path = Path(f"job_finder/web/calibration_ollama_{tier}.json")
    out_path.write_text(json.dumps(table, indent=2), encoding="utf-8")

    print(
        f"\n{tier.upper()} fit — n={len(pairs)}\n"
        f"  bias   : {bias_before:+7.2f} -> {bias_after:+7.2f}\n"
        f"  MAE    : {mae_before:7.2f} -> {mae_after:7.2f}\n"
        f"  points : {len(breakpoints)} breakpoints\n"
        f"  wrote  : {out_path}\n"
        f"  pairs  : {pairs_path}"
    )

    # Target gates (per plan)
    tier_targets = {
        "sonnet": {"bias_abs_max": 5.0, "mae_max": 12.0},
        "haiku": {"bias_abs_max": 5.0, "mae_max": 15.0},
    }
    gates = tier_targets[tier]
    failures = []
    if abs(bias_after) > gates["bias_abs_max"]:
        failures.append(f"|bias_after|={abs(bias_after):.2f} > {gates['bias_abs_max']}")
    if mae_after > gates["mae_max"]:
        failures.append(f"mae_after={mae_after:.2f} > {gates['mae_max']}")
    if failures:
        print(f"  WARNING: target gates not met: {'; '.join(failures)}")
    else:
        print("  gates  : PASS")

    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("sonnet", "haiku", "both"),
        default="both",
    )
    parser.add_argument("--n", type=int, default=30, help="Sample size per tier.")
    parser.add_argument("--model", default="qwen2.5:14b")
    parser.add_argument(
        "--prompt-variant",
        default="fewshot",
        help="Recorded in the written table for provenance only (the production "
        "scorer selects its own prompt based on tier + job archetype).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    config = load_config()
    profile = load_scoring_profile(config)

    tiers = ("sonnet", "haiku") if args.tier == "both" else (args.tier,)
    with standalone_connection(config["db"]["path"]) as conn:
        for tier in tiers:
            refit_tier(
                tier=tier,
                n=args.n,
                config=config,
                profile=profile,
                conn=conn,
                model=args.model,
                prompt_variant=args.prompt_variant,
            )

    # Reload runtime tables so any process that imports score_calibration
    # after this script runs picks up the new files. No-op for this process
    # (it's ending), but harmless.
    try:
        from job_finder.web.score_calibration import reload_tables
        reload_tables()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
