"""Enrichment backfill script for job-finder.

Runs the 7-tier enrichment pipeline on all non-exhausted jobs in a convergence
loop. Estimates and confirms AI costs before any API calls. Queues newly-enriched
jobs for Sonnet evaluation. Re-scores borderline Haiku jobs whose enrichment tier
advanced.

Usage:
    python -m job_finder.web.backfill_enrichment

Design principles:
    - Uses its own sqlite3 connection (like stale_detector.py — thread-safe, no Flask g.db).
    - Calls enrich_job() directly with anthropic_client passed in — NOT run_enrichment_backfill()
      which silently skips AI tiers.
    - Convergence loop: runs passes until a pass enriches 0 jobs.
    - Cost confirmation before any AI tier executes.
    - High-value jobs first: ORDER BY COALESCE(haiku_score, 0) DESC.

Exports:
    main: CLI entry point.
    run_enrichment_pass: Single enrichment pass.
    run_passes_to_convergence: Convergence loop with cost gate.
    estimate_and_confirm: Estimate cost and prompt user for confirmation.
    run_sonnet_backfill: Queue jobs with jd_full but no sonnet_score.
    run_borderline_rescore: Re-score borderline jobs whose tier advanced.
"""

import json
import logging
import sqlite3
from typing import Any, Optional

from job_finder.config import DEFAULT_HAIKU_THRESHOLD, DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_SONNET
from job_finder.db import persist_haiku_score, persist_sonnet_score
from job_finder.web.claude_client import MODEL_PRICING
from job_finder.web.data_enricher import enrich_job
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_orchestrator import load_scoring_profile
from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
from job_finder.web.haiku_scorer import score_job_haiku

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token/cost estimation constants
# ---------------------------------------------------------------------------

# Approximate token counts per enrichment call
_HAIKU_INPUT_TOKENS = 600
_HAIKU_OUTPUT_TOKENS = 200
_SONNET_INPUT_TOKENS = 2000
_SONNET_OUTPUT_TOKENS = 500

# Tiers eligible for re-enrichment (not yet exhausted or at high paid tiers)
_ELIGIBLE_TIERS_QUERY = (
    "enrichment_tier IS NULL OR enrichment_tier NOT IN ('exhausted', 'serpapi', 'sonnet', 'agentic', 'agentic_exhausted')"
)

# Borderline score range for re-scoring after tier advancement
_BORDERLINE_MIN = 40
_BORDERLINE_MAX = 70

# Offline-only provider routing. Live pipeline config intentionally has no
# providers.haiku / providers.sonnet so scoring_runner stays on the Claude CLI
# (lower latency after cold-start flag tuning). Backfill wraps its config
# through _offline_config() to opt into Ollama with a CLI fallback, trading a
# few extra seconds per call for zero API cost on nightly/manual batches.
_OFFLINE_PROVIDERS: dict = {
    "haiku": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback": "anthropic",
    },
    "sonnet": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback_chain": [
            {"provider": "anthropic", "model": DEFAULT_MODEL_SONNET},
        ],
    },
}


def _offline_config(config: dict) -> dict:
    """Return a shallow-copied config with Ollama routing injected for scoring.

    Preserves every other field unchanged. Existing user-set providers.haiku
    or providers.sonnet entries win over the defaults (lets a caller override
    the routing on a per-run basis via CLI flags or env).
    """
    existing = config.get("providers", {}) or {}
    merged_providers = {**_OFFLINE_PROVIDERS, **existing}
    # Preserve cascade meta-keys if the caller set them
    for meta in ("daily_limits", "throttle_delays"):
        if meta in existing:
            merged_providers[meta] = existing[meta]
    return {**config, "providers": merged_providers}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_and_confirm(conn: sqlite3.Connection, config: dict) -> bool:
    """Count eligible jobs, estimate AI cost, and prompt user for confirmation.

    Counts jobs at each enrichment tier that would be processed. Computes
    estimated cost using MODEL_PRICING constants (rough per-job estimates).
    Prints a tier breakdown and total cost estimate, then prompts.

    Args:
        conn: Open SQLite connection.
        config: Application config dict.

    Returns:
        True if user enters 'y' or 'Y', False otherwise (including empty Enter).
    """
    # Count jobs at each eligible tier
    rows = conn.execute(
        f"""SELECT
            CASE WHEN enrichment_tier IS NULL THEN 'NULL' ELSE enrichment_tier END AS tier,
            COUNT(*) AS cnt
          FROM jobs
          WHERE {_ELIGIBLE_TIERS_QUERY}
          GROUP BY tier
          ORDER BY cnt DESC"""
    ).fetchall()

    tier_counts: dict[str, int] = {}
    total_eligible = 0
    for row in rows:
        tier = dict(row)["tier"]
        cnt = dict(row)["cnt"]
        tier_counts[tier] = cnt
        total_eligible += cnt

    print("\n" + "=" * 60)
    print("ENRICHMENT BACKFILL — COST ESTIMATE")
    print("=" * 60)
    print(f"\nEligible jobs to enrich: {total_eligible}")
    print("\nTier breakdown:")
    for tier, cnt in sorted(tier_counts.items(), key=lambda x: -x[1]):
        print(f"  {tier:12s}: {cnt:4d} jobs")

    # Estimate cost: each job will likely need Haiku tier at minimum.
    # Jobs at NULL/free/ddg tiers may escalate to Haiku for extraction.
    # Rough: ~80% of eligible jobs reach Haiku tier, ~20% reach Sonnet tier.
    haiku_jobs = int(total_eligible * 0.8)
    sonnet_jobs = int(total_eligible * 0.2)

    haiku_pricing = MODEL_PRICING.get(DEFAULT_MODEL_HAIKU, {"input": 1.0, "output": 5.0})
    sonnet_pricing = MODEL_PRICING.get(DEFAULT_MODEL_SONNET, {"input": 3.0, "output": 15.0})

    haiku_cost = (
        (haiku_jobs * _HAIKU_INPUT_TOKENS / 1_000_000) * haiku_pricing["input"]
        + (haiku_jobs * _HAIKU_OUTPUT_TOKENS / 1_000_000) * haiku_pricing["output"]
    )
    sonnet_cost = (
        (sonnet_jobs * _SONNET_INPUT_TOKENS / 1_000_000) * sonnet_pricing["input"]
        + (sonnet_jobs * _SONNET_OUTPUT_TOKENS / 1_000_000) * sonnet_pricing["output"]
    )
    total_estimate = haiku_cost + sonnet_cost

    print(f"\nEstimated AI cost:")
    print(f"  Haiku  (~{haiku_jobs} jobs): ${haiku_cost:.4f}")
    print(f"  Sonnet (~{sonnet_jobs} jobs): ${sonnet_cost:.4f}")
    print(f"  TOTAL ESTIMATE:   ${total_estimate:.4f}")
    print("\nNote: Actual cost depends on how many jobs need AI tiers.")
    print("=" * 60)

    response = input("\nProceed with enrichment backfill? [y/N] ").strip().lower()
    return response == "y"

def run_enrichment_pass(
    conn: sqlite3.Connection,
    serpapi_key: Optional[str],
    config: dict,
    limit: int = 100,
) -> tuple[int, set]:
    """Run a single enrichment pass over all eligible jobs.

    Queries jobs where enrichment_tier IS NULL or not yet at a high tier,
    ordered by COALESCE(haiku_score, 0) DESC so high-value jobs enrich first.
    Calls enrich_job() directly with anthropic_client — AI tiers will execute.

    Tracks which dedup_keys had their enrichment_tier advance during this pass.

    Args:
        conn: Open SQLite connection.
        serpapi_key: Optional SerpAPI API key.
        config: Application config dict.
        client: Anthropic client instance.
        limit: Max jobs to process per pass.

    Returns:
        Tuple of (enriched_count, tier_advanced_keys).
        enriched_count: Number of jobs that got non-empty enrichment results.
        tier_advanced_keys: Set of dedup_keys whose enrichment_tier changed.
    """
    rows = conn.execute(
        f"""SELECT * FROM jobs
           WHERE {_ELIGIBLE_TIERS_QUERY}
           ORDER BY COALESCE(haiku_score, 0) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    enriched_count = 0
    tier_advanced_keys: set = set()

    for row in rows:
        job_row = dict(row)
        dedup_key = job_row["dedup_key"]
        tier_before = job_row.get("enrichment_tier")

        result = enrich_job(
            job_row,
            serpapi_key=serpapi_key,
            conn=conn,
            config=config,
        )

        if result:
            enriched_count += 1

        # Check if tier advanced by re-reading the row
        updated_row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        tier_after = dict(updated_row)["enrichment_tier"] if updated_row else tier_before

        if tier_after != tier_before:
            tier_advanced_keys.add(dedup_key)

    return enriched_count, tier_advanced_keys

def run_passes_to_convergence(
    conn: sqlite3.Connection,
    serpapi_key: Optional[str],
    config: dict,
    limit: int = 100,
) -> tuple[int, set]:
    """Run enrichment passes until convergence (0 enriched in a pass).

    Calls estimate_and_confirm first; aborts if user declines.
    Loops calling run_enrichment_pass until a pass returns 0 enriched.
    Prints progress after each pass.

    Args:
        conn: Open SQLite connection.
        serpapi_key: Optional SerpAPI API key.
        config: Application config dict.
        client: Anthropic client instance.
        limit: Max jobs per pass.

    Returns:
        Tuple of (total_enriched, cumulative_tier_advanced_keys).
    """
    if not estimate_and_confirm(conn, config):
        print("Aborted by user.")
        return 0, set()

    total_enriched = 0
    cumulative_tier_advanced_keys: set = set()
    pass_num = 0

    while True:
        pass_num += 1
        enriched_count, tier_advanced_keys = run_enrichment_pass(
            conn, serpapi_key=serpapi_key, config=config, limit=limit
        )
        print(f"Pass {pass_num}: {enriched_count} jobs enriched")
        total_enriched += enriched_count
        cumulative_tier_advanced_keys.update(tier_advanced_keys)

        if enriched_count == 0:
            print(f"Convergence reached after {pass_num} pass(es).")
            break

    return total_enriched, cumulative_tier_advanced_keys

def run_sonnet_backfill(
    conn: sqlite3.Connection,
    config: dict,
) -> int:
    """Evaluate jobs that have jd_full but no sonnet_score using Sonnet.

    Queries jobs where jd_full IS NOT NULL AND sonnet_score IS NULL,
    ordered by COALESCE(haiku_score, 0) DESC to prioritize high-value jobs.
    Writes sonnet_score, sonnet_summary (as haiku_summary fallback), and
    fit_analysis JSON to the DB.

    Args:
        conn: Open SQLite connection.
        config: Application config dict.
        client: Anthropic client instance.

    Returns:
        Number of jobs evaluated.
    """
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE jd_full IS NOT NULL AND sonnet_score IS NULL
           ORDER BY COALESCE(haiku_score, 0) DESC"""
    ).fetchall()

    print(f"\nSonnet backfill: {len(rows)} jobs to evaluate")
    evaluated_count = 0

    # Load profile for Sonnet evaluation
    profile = load_scoring_profile(config)

    for i, row in enumerate(rows, start=1):
        job_row = dict(row)
        dedup_key = job_row["dedup_key"]

        scoring_result = evaluate_job_sonnet(job_row, profile, conn, _offline_config(config))

        if scoring_result is None or scoring_result.status != "success" or scoring_result.data is None:
            logger.debug("Sonnet eval returned %s for '%s'",
                         scoring_result.status if scoring_result else "None", dedup_key)
            continue

        result = scoring_result.data
        score = result.get("score")
        summary = result.get("summary")
        fit_analysis = result.get("fit_analysis")

        # Persist to DB
        fit_json = json.dumps(fit_analysis) if fit_analysis else None
        persist_sonnet_score(conn, dedup_key, score, fit_json)

        evaluated_count += 1
        if i % 10 == 0 or i == len(rows):
            print(f"  Sonnet: {i}/{len(rows)} evaluated ({evaluated_count} scored)")

    print(f"Sonnet backfill complete: {evaluated_count} jobs evaluated.")
    return evaluated_count

def run_borderline_rescore(
    conn: sqlite3.Connection,
    config: dict,
    tier_advanced_keys: set,
) -> int:
    """Re-score borderline Haiku jobs (40-70) whose enrichment tier advanced.

    Only processes jobs that are both:
    1. In the borderline score range (haiku_score BETWEEN 40 AND 70)
    2. In tier_advanced_keys (their enrichment_tier advanced this session)

    Calls score_job_haiku() with the latest job data (may now include jd_full).
    Updates haiku_score and haiku_summary in the DB.

    Args:
        conn: Open SQLite connection.
        config: Application config dict.
        client: Anthropic client instance.
        tier_advanced_keys: Set of dedup_keys whose enrichment_tier advanced.

    Returns:
        Number of jobs re-scored.
    """
    if not tier_advanced_keys:
        return 0

    # Build parameterized IN clause
    placeholders = ", ".join("?" * len(tier_advanced_keys))
    rows = conn.execute(
        f"""SELECT * FROM jobs
           WHERE haiku_score BETWEEN ? AND ?
             AND dedup_key IN ({placeholders})
           ORDER BY haiku_score DESC""",
        (_BORDERLINE_MIN, _BORDERLINE_MAX, *tier_advanced_keys),
    ).fetchall()

    print(f"\nBorderline re-score: {len(rows)} jobs to re-score")
    rescored_count = 0

    # Load profile for Haiku scoring
    profile = load_scoring_profile(config)

    haiku_threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)

    for row in rows:
        job_row = dict(row)
        dedup_key = job_row["dedup_key"]

        scoring_result = score_job_haiku(job_row, profile, conn, _offline_config(config))

        if scoring_result is None or scoring_result.status != "success" or scoring_result.data is None:
            logger.debug("Haiku re-score returned %s for '%s'",
                         scoring_result.status if scoring_result else "None", dedup_key)
            continue

        result = scoring_result.data
        new_score = result.get("score")
        new_summary = result.get("summary")

        # Update haiku_score and haiku_summary
        persist_haiku_score(conn, dedup_key, new_score, new_summary)

        rescored_count += 1

    print(f"Borderline re-score complete: {rescored_count} jobs re-scored.")
    return rescored_count

def main() -> None:
    """CLI entry point for enrichment backfill.

    Loads config, opens its own sqlite3 connection (WAL-safe, own connection
    like stale_detector.py), instantiates Anthropic client. Runs convergence
    passes, then Sonnet backfill, then borderline re-score.
    """

    from job_finder.config import load_config

    config = load_config()

    db_path = config.get("db", {}).get("path", "jobs.db")
    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key") or None

    if not serpapi_key:
        print("Warning: SerpAPI key not configured — SerpAPI tier will be skipped.")

    # Open own connection (thread-safe, not Flask g.db)
    with standalone_connection(db_path) as conn:

        print("\n=== Phase 1: Convergence Enrichment Passes ===")
        total_enriched, tier_advanced_keys = run_passes_to_convergence(
            conn, serpapi_key=serpapi_key, config=config
        )
        print(f"\nTotal enriched across all passes: {total_enriched}")
        print(f"Jobs with tier advancement: {len(tier_advanced_keys)}")

        print("\n=== Phase 2: Sonnet Backfill ===")
        sonnet_count = run_sonnet_backfill(conn, config=config)

        print("\n=== Phase 3: Borderline Re-score ===")
        rescore_count = run_borderline_rescore(
            conn, config=config, tier_advanced_keys=tier_advanced_keys
        )

        # Final tier distribution summary
        print("\n=== Final Tier Distribution ===")
        rows = conn.execute(
            """SELECT
                CASE WHEN enrichment_tier IS NULL THEN 'NULL' ELSE enrichment_tier END AS tier,
                COUNT(*) AS cnt
              FROM jobs
              GROUP BY tier
              ORDER BY cnt DESC"""
        ).fetchall()
        for row in rows:
            r = dict(row)
            print(f"  {r['tier']:12s}: {r['cnt']:4d} jobs")

        print(f"\nBackfill complete.")
        print(f"  Enriched: {total_enriched}")
        print(f"  Sonnet evaluated: {sonnet_count}")
        print(f"  Borderline re-scored: {rescore_count}")

if __name__ == "__main__":
    main()
