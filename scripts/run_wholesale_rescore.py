#!/usr/bin/env python3
"""Drive the wholesale rescore loop without Flask.

Mirrors job_finder.web.blueprints.batch_scoring._run_batch_bg's contract:
  - load profile + build candidate_context once at start
  - select rows: classification IS NULL AND pipeline_status NOT IN ('dismissed','archived')
  - per row: exclusion filter (auto-dismiss matches) → score_and_persist_job
  - per-row errors are logged, counted, and skipped — they do not abort the loop

Differences from the web worker:
  - No batch_score_sessions row is created (CLI has no UI to poll).
  - Output goes to stdout/stderr (and an optional --log file). The launch
    wrapper captures these for overnight monitoring.
  - --limit is supported for staged or smoke-test runs.
  - Resume-on-restart is implicit: the predicate ``classification IS NULL``
    self-heals across runs, so re-invoking after a crash picks up exactly
    where the previous attempt left off.

Usage:
    uv run python scripts/run_wholesale_rescore.py [--db jobs.db] [--config config.yaml]
                                                   [--limit N] [--log path] [--progress-every 25]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable

import yaml

# Ensure project root is on sys.path so job_finder imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.db import JOBS_ALL_COLUMNS, update_pipeline_status
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.scoring_orchestrator import (
    build_candidate_context,
    load_scoring_profile,
    score_and_persist_job,
)


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


def run_rescore(
    db_path: str,
    config: dict,
    *,
    limit: int | None = None,
    progress_every: int = 25,
    score_fn: Callable | None = None,
) -> dict:
    """Drive the rescore loop.

    Args:
        db_path: SQLite DB path.
        config: Already-loaded config dict.
        limit: Optional ceiling on rows to process (for smoke tests).
        progress_every: Log a progress line every N rows.
        score_fn: Injection point for tests. Defaults to the prod
            ``score_and_persist_job``.

    Returns:
        ``{'total': N, 'scored': N, 'skipped': N, 'excluded': N, 'errored': N,
           'elapsed_s': float}``
    """
    log = logging.getLogger("rescore")
    if score_fn is None:
        score_fn = score_and_persist_job

    profile = load_scoring_profile(config)
    candidate_context = build_candidate_context(config, profile)
    log.info(
        "Loaded profile (%d positions) + built candidate context (%d chars)",
        len(profile.get("positions", [])),
        len(candidate_context),
    )

    exclusions = config.get("profile", {}).get("exclusions", {})
    profile_min_salary = config.get("profile", {}).get("min_salary")

    scored = skipped = excluded = errored = 0
    started = time.time()

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE classification IS NULL "
            "AND pipeline_status NOT IN ('dismissed', 'archived') "
            "ORDER BY score DESC NULLS LAST"
        ).fetchall()

        if limit is not None:
            rows = rows[:limit]

        total = len(rows)
        log.info("Starting rescore: %d rows (limit=%s)", total, limit)

        for i, row in enumerate(rows, 1):
            job_row = dict(row)

            is_excluded, reason = should_exclude(
                job_row, exclusions, profile_min_salary, config=config
            )
            if is_excluded:
                dedup_key = job_row.get("dedup_key")
                if dedup_key and job_row.get("pipeline_status") == "discovered":
                    update_pipeline_status(
                        conn,
                        dedup_key,
                        "dismissed",
                        source="exclusion_filter",
                        evidence=reason,
                    )
                excluded += 1
                continue

            try:
                result = score_fn(job_row, conn, config, candidate_context=candidate_context)
                if result is not None:
                    scored += 1
                else:
                    skipped += 1
            except Exception as e:
                errored += 1
                log.warning("error scoring %s: %s", job_row.get("dedup_key"), e)

            if i % progress_every == 0:
                conn.commit()
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                remaining_min = (total - i) / rate / 60 if rate > 0 else 0
                log.info(
                    "progress=%d/%d scored=%d skipped=%d excluded=%d errored=%d "
                    "rate=%.2f/s eta=%.1fmin",
                    i,
                    total,
                    scored,
                    skipped,
                    excluded,
                    errored,
                    rate,
                    remaining_min,
                )

        conn.commit()

    elapsed = time.time() - started
    log.info(
        "DONE: total=%d scored=%d skipped=%d excluded=%d errored=%d elapsed=%.1fmin",
        total,
        scored,
        skipped,
        excluded,
        errored,
        elapsed / 60,
    )
    return {
        "total": total,
        "scored": scored,
        "skipped": skipped,
        "excluded": excluded,
        "errored": errored,
        "elapsed_s": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the wholesale rescore (CLI version of the batch worker)."
    )
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--log", default=None, help="Tee logs to this file in addition to stdout.")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    _setup_logging(args.log)
    config = _load_config(args.config)
    run_rescore(
        args.db,
        config,
        limit=args.limit,
        progress_every=args.progress_every,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
