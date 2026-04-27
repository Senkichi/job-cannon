"""Statistical metrics for Phase 33 Plan 2 — paired MAE, BCa bootstrap,
retry-rate gate, tiebreaker comparator.

Per Phase 33 CONTEXT §D-15/D-16/D-17/D-20/D-23:
  - Paired per-dimension MAE (every candidate scores same baseline jobs)
  - BCa bootstrap 10k resamples, random_state=42, 95% CI
  - Retry-rate gate: rate > 0.20 → WARN; suppressed when n<20
  - Tiebreaker precedence: uniformity → retry → latency → VRAM
"""

from __future__ import annotations

import statistics
from collections.abc import Callable

import numpy as np
from scipy.stats import bootstrap

_DIMENSIONS: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def paired_mae(
    candidate_results: dict[str, dict],
    gold_results: dict[str, dict],
    dimension: str,
) -> dict:
    """Paired per-dimension MAE of candidate vs gold on the same dedup_keys.

    Args:
        candidate_results: {dedup_key: assessment_dict} from the candidate.
        gold_results: {dedup_key: assessment_dict} from Opus gold.
        dimension: One of the six ordinal fields (title_fit, location_fit,
            comp_fit, domain_match, seniority_match, skills_match).

    Returns:
        {"mae": float or None, "n": int, "deltas": list[float]} — n is the
        count of paired, valid, non-error rows. deltas are (candidate - gold)
        per row; mae = mean(abs(deltas)). Returns None MAE on n=0.
    """
    deltas: list[float] = []
    for k, c in candidate_results.items():
        g = gold_results.get(k)
        if g is None:
            continue
        if not isinstance(c, dict) or not isinstance(g, dict):
            continue
        if "_error" in c or "_error" in g:
            continue
        if dimension not in c or dimension not in g:
            continue
        cv = c[dimension]
        gv = g[dimension]
        if cv is None or gv is None:
            continue
        try:
            deltas.append(float(cv) - float(gv))
        except (TypeError, ValueError):
            continue

    if not deltas:
        return {"mae": None, "n": 0, "deltas": []}

    return {
        "mae": statistics.mean(abs(d) for d in deltas),
        "n": len(deltas),
        "deltas": deltas,
    }


def bca_bootstrap_ci(
    deltas: list[float],
    statistic: Callable | None = None,
) -> tuple[float, float]:
    """95% bias-corrected accelerated bootstrap CI on `deltas`.

    Per D-17: method='BCa', n_resamples=10_000, random_state=42, CI=0.95.

    Returns (nan, nan) for degenerate input (n<2 or all-identical values —
    BCa acceleration is undefined when deltas are constant).
    """
    if statistic is None:
        statistic = np.mean
    arr = np.asarray(list(deltas), dtype=float)
    if len(arr) < 2:
        return (float("nan"), float("nan"))
    # BCa is undefined when all values are identical (zero variance).
    if np.all(arr == arr[0]):
        v = float(statistic(arr))
        return (v, v)
    try:
        res = bootstrap(
            (arr,),
            statistic=statistic,
            method="BCa",
            n_resamples=10_000,
            confidence_level=0.95,
            random_state=42,
        )
    except Exception:
        return (float("nan"), float("nan"))
    lo = float(res.confidence_interval.low)
    hi = float(res.confidence_interval.high)
    return (lo, hi)


def retry_rate_gate(
    retries: int,
    n: int,
    threshold: float = 0.20,
) -> tuple[str, float]:
    """Schema-retry-rate gate per D-20.

    Returns:
        ("SUPPRESSED", rate) when n < 20 (not enough samples).
        ("WARN", rate) when n >= 20 and rate > threshold.
        ("PASS", rate) when n >= 20 and rate <= threshold.
    """
    if n <= 0:
        return ("SUPPRESSED", 0.0)
    rate = retries / n
    if n < 20:
        return ("SUPPRESSED", rate)
    if rate > threshold:
        return ("WARN", rate)
    return ("PASS", rate)


def tiebreaker_key(candidate_result: dict) -> tuple:
    """Tiebreaker comparator for candidate ranking per D-23.

    Precedence (ascending — lower is better):
      1. uniformity: stddev across 6 per-dim MAEs (lopsided → higher stddev)
      2. retry_rate: lower is better
      3. -tokens_per_sec: higher tok/s is better → negate for sort
      4. vram_mb: lower is better
    """
    per_dim = candidate_result.get("per_dim_mae", {})
    maes = []
    for d in _DIMENSIONS:
        v = per_dim.get(d)
        if v is None:
            continue
        try:
            maes.append(float(v))
        except (TypeError, ValueError):
            continue
    uniformity = statistics.stdev(maes) if len(maes) >= 2 else 0.0
    retry_rate = float(candidate_result.get("retry_rate", 0.0) or 0.0)
    tok_per_sec = float(candidate_result.get("tokens_per_sec", 1.0) or 1.0)
    vram_mb = int(candidate_result.get("vram_mb", 0) or 0)
    return (uniformity, retry_rate, -tok_per_sec, vram_mb)
