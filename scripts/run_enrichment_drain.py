#!/usr/bin/env python3
"""Drive the enrichment backfill loop without Flask.

Wraps job_finder.web.data_enricher.run_enrichment_backfill in a loop that
keeps calling it until a batch yields 0 enriched rows (drain complete) or
a safety cap is reached.

Used to drive the post-Workday-fix re-enrichment of the ~108 rows reset by
Migration 46 (and any other unenriched accumulation) through the corrected
ats_platforms.py URL template + enrichment_tiers.py SPA-shell guard.

Each batch processes up to ``--limit`` rows. The cron job uses limit=200;
the plan calls for limit=500 to drain faster. The query inside
run_enrichment_backfill orders by first_seen DESC and skips rows already in
terminal tiers, so re-invocation between batches walks the backlog forward
without revisiting completed rows.

Usage:
    uv run python scripts/run_enrichment_drain.py [--db jobs.db] [--config config.yaml]
        [--limit N] [--safety-cap N] [--log path] [--once]
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing

import yaml

# Ensure project root is on sys.path so job_finder imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.web.data_enricher import run_enrichment_backfill


def _setup_logging(log_path: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _count_remaining(db_path: str) -> int:
    """Count rows that match run_enrichment_backfill's eligibility predicate.

    Mirrors the WHERE clause from data_enricher.run_enrichment_backfill so
    the remaining-count can be reasoned about against the same population
    the loop is draining.
    """
    sql = (
        "SELECT COUNT(*) FROM jobs "
        "WHERE (enrichment_tier IS NULL "
        "       OR enrichment_tier NOT IN ('exhausted', 'serpapi', 'agentic', 'sonnet')) "
        "  AND (jd_full IS NULL OR jd_full = '' OR salary_min IS NULL)"
    )
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql).fetchone()[0]


def run_drain(
    db_path: str,
    config: dict,
    *,
    limit: int = 500,
    safety_cap: int = 5000,
    once: bool = False,
) -> dict:
    """Drive the enrichment drain loop.

    Args:
        db_path: SQLite DB path.
        config: Loaded config dict (yaml.safe_load output).
        limit: Per-batch ceiling passed to run_enrichment_backfill.
        safety_cap: Stop after this many rows have been enriched in total
            across all batches (defensive ceiling).
        once: If True, run a single batch and exit (smoke-test mode).

    Returns:
        ``{'batches': N, 'enriched_total': N, 'remaining_before': N,
           'remaining_after': N, 'elapsed_s': float, 'stopped_reason': str}``
    """
    log = logging.getLogger("enrich_drain")

    serpapi_key = (config.get("sources") or {}).get("serpapi", {}).get("api_key")
    log.info(
        "Drain starting: db=%s limit=%d safety_cap=%d once=%s serpapi_configured=%s",
        db_path,
        limit,
        safety_cap,
        once,
        bool(serpapi_key),
    )

    remaining_before = _count_remaining(db_path)
    log.info("Eligible rows before drain: %d", remaining_before)

    batches = 0
    enriched_total = 0
    started = time.time()
    stopped_reason = "unknown"

    while True:
        batch_started = time.time()
        try:
            enriched = run_enrichment_backfill(
                db_path,
                serpapi_key=serpapi_key,
                config=config,
                limit=limit,
            )
        except Exception as e:
            log.exception("Batch %d crashed: %s", batches + 1, e)
            stopped_reason = f"exception: {e!r}"
            break

        batches += 1
        enriched_total += enriched
        batch_elapsed = time.time() - batch_started
        remaining_now = _count_remaining(db_path)

        log.info(
            "batch=%d enriched=%d total=%d remaining=%d batch_elapsed=%.1fs",
            batches,
            enriched,
            enriched_total,
            remaining_now,
            batch_elapsed,
        )

        if once:
            stopped_reason = "once_flag"
            break

        if enriched == 0:
            stopped_reason = "batch_returned_zero"
            break

        if enriched_total >= safety_cap:
            stopped_reason = f"safety_cap_hit ({enriched_total} >= {safety_cap})"
            break

    elapsed = time.time() - started
    remaining_after = _count_remaining(db_path)

    log.info(
        "DONE: batches=%d enriched_total=%d remaining_before=%d remaining_after=%d "
        "elapsed=%.1fmin reason=%s",
        batches,
        enriched_total,
        remaining_before,
        remaining_after,
        elapsed / 60,
        stopped_reason,
    )

    return {
        "batches": batches,
        "enriched_total": enriched_total,
        "remaining_before": remaining_before,
        "remaining_after": remaining_after,
        "elapsed_s": elapsed,
        "stopped_reason": stopped_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the enrichment backfill loop until exhaustion (CLI).",
    )
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=500, help="Per-batch row ceiling.")
    parser.add_argument(
        "--safety-cap",
        type=int,
        default=5000,
        help="Hard ceiling on total enriched-count to prevent runaways.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single batch and exit. For smoke tests.",
    )
    parser.add_argument("--log", default=None, help="Tee logs to this file in addition to stdout.")
    args = parser.parse_args()

    _setup_logging(args.log)
    config = _load_config(args.config)
    run_drain(
        args.db,
        config,
        limit=args.limit,
        safety_cap=args.safety_cap,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
