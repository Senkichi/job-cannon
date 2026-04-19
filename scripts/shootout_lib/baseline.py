"""Anthropic-filtered, stratified baseline sampling for Phase 33 Plan 2.

Per Phase 33 CONTEXT §D-06/D-08/D-09/D-10:
  - Pool filter: jobs.scoring_provider='anthropic' AND scoring_costs.provider='anthropic'
  - Stratified across 4 score quartiles [0,25), [25,50), [50,75), [75,100]
  - n=100 total (80 dev + 20 holdout)
  - Aborts on insufficient pool with three-option remediation message
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable


class ShootoutInsufficientBaselineError(Exception):
    """Raised when the filtered Anthropic baseline pool cannot fill n
    stratified samples. The message enumerates the three remediation options
    per D-10 (no silent weakening)."""


@dataclass(frozen=True)
class BaselineSample:
    """Immutable container for a stratified baseline sample plus metadata.

    dev and holdout are tuples of dicts (each row carries dedup_key, title,
    company, jd_full, location, salary, sonnet_score, haiku_score,
    scoring_provider, legitimacy_note, job_archetype).

    quartile_counts records how many rows were drawn from each quartile —
    recorded in the matrix methodology section for audit.

    total_eligible_pool is the full size of the filtered pool BEFORE
    stratified sampling (used to detect quartile imbalance in the matrix).
    """

    dev: tuple[dict, ...]
    holdout: tuple[dict, ...]
    quartile_counts: dict[str, int] = field(default_factory=dict)
    total_eligible_pool: int = 0


_BASELINE_POOL_SQL: str = """
    SELECT j.dedup_key, j.title, j.company, j.jd_full, j.location,
           j.salary_min, j.salary_max,
           j.sonnet_score, j.haiku_score, j.scoring_provider,
           j.legitimacy_note, j.job_archetype
    FROM jobs j
    WHERE j.scoring_provider = 'anthropic'
      AND j.sonnet_score IS NOT NULL
      AND j.jd_full IS NOT NULL
      AND LENGTH(TRIM(j.jd_full)) >= 200
      AND EXISTS (
          SELECT 1 FROM scoring_costs sc
          WHERE sc.job_id = j.dedup_key
            AND sc.provider = 'anthropic'
            AND sc.purpose IN ('sonnet_eval', 'haiku_score')
      )
"""


def _bucket(score: float) -> str:
    """Assign score to one of four quartile buckets: q1/q2/q3/q4.

    Bucket boundaries: [0,25) → q1, [25,50) → q2, [50,75) → q3, [75,100] → q4.
    """
    if score < 25:
        return "q1"
    if score < 50:
        return "q2"
    if score < 75:
        return "q3"
    return "q4"


def _three_option_message(total: int, n: int, detail: str = "") -> str:
    """The canonical abort message per D-10 — all three remediation options
    must be named in the exception string."""
    base = (
        f"Filtered Anthropic baseline pool has only {total} rows, need {n}. "
        "Options: (1) relax filter (risk contamination), (2) reduce n, "
        "(3) rescore more jobs through Anthropic."
    )
    return base if not detail else f"{base} {detail}"


def build_baseline_sample(
    conn: sqlite3.Connection,
    n: int = 100,
    holdout_fraction: float = 0.2,
    random_state: int = 42,
) -> BaselineSample:
    """Build a stratified Anthropic-filtered baseline sample.

    Args:
        conn: Open sqlite3 connection (read-only usage).
        n: Target total sample size (default 100 per D-06).
        holdout_fraction: Fraction of n set aside for holdout (default 0.2 →
            80 dev + 20 holdout for n=100).
        random_state: RNG seed for reproducible stratified sampling.

    Returns:
        BaselineSample with .dev, .holdout, .quartile_counts, .total_eligible_pool.

    Raises:
        ShootoutInsufficientBaselineError: If the filtered pool cannot fill
            the stratified sample (either total < n OR any quartile bucket
            has fewer than n/4 rows). The exception message names all three
            remediation options per D-10.
    """
    # Normalize row access — baseline.py does not assume a particular factory
    rows = conn.execute(_BASELINE_POOL_SQL).fetchall()
    pool: list[dict] = []
    for r in rows:
        # Support both sqlite3.Row and plain tuple/dict access
        if isinstance(r, sqlite3.Row):
            pool.append(dict(r))
        elif isinstance(r, dict):
            pool.append(r)
        else:
            # Positional tuple — map via column description
            cols = [c[0] for c in conn.execute(_BASELINE_POOL_SQL).description]
            pool.append(dict(zip(cols, r)))
            break  # one reshape is enough; but normal case is sqlite3.Row

    total = len(pool)
    if total < n:
        raise ShootoutInsufficientBaselineError(_three_option_message(total, n))

    # Bucket the pool by quartile
    buckets: dict[str, list[dict]] = {"q1": [], "q2": [], "q3": [], "q4": []}
    for row in pool:
        score = float(row.get("sonnet_score") or 0.0)
        buckets[_bucket(score)].append(row)

    per_bucket = n // 4
    rng = random.Random(random_state)

    # Verify every bucket has enough rows BEFORE sampling
    for q in ("q1", "q2", "q3", "q4"):
        if len(buckets[q]) < per_bucket:
            raise ShootoutInsufficientBaselineError(
                _three_option_message(
                    total, n,
                    detail=f"Quartile {q} has only {len(buckets[q])} rows (need {per_bucket}).",
                )
            )

    # Stratified sample: exactly n/4 per bucket
    sampled: list[dict] = []
    for q in ("q1", "q2", "q3", "q4"):
        pick = rng.sample(buckets[q], per_bucket)
        sampled.extend(pick)

    # Deterministic shuffle of the full sample so dev/holdout split is
    # reproducible AND doesn't cluster quartiles
    rng.shuffle(sampled)

    dev_n = int(n * (1.0 - holdout_fraction))
    dev = tuple(sampled[:dev_n])
    holdout = tuple(sampled[dev_n:])

    counts = {q: per_bucket for q in ("q1", "q2", "q3", "q4")}

    return BaselineSample(
        dev=dev,
        holdout=holdout,
        quartile_counts=counts,
        total_eligible_pool=total,
    )
