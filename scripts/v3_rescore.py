#!/usr/bin/env python
"""v3.0 rescore runner — Phase 34 Plan 4 (CONTEXT D-18, D-19).

Batched, stratified rescore of existing jobs through the production
score_job / persist_job_assessment path. Writes a structured report to
--report-path for scripts/v3_rescore_validate.py to consume.

CLI:
    uv run --active python scripts/v3_rescore.py \\
        --batch-size 150 --seed 20260421001 --batch-number 1 \\
        --report-path .planning/phases/34-greenfield-scorer-rewrite/rescore-batch-1-report.json

Row selection is stratified by legacy sonnet_score quartile and deterministic
via ORDER BY printf('%s%d', dedup_key, seed). Distinct seeds per batch
produce distinct row sets for reproducibility on re-run (CONTEXT D-19).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import yaml

# Ensure project root is on sys.path so job_finder imports resolve when
# this script is invoked via `uv run --active python scripts/v3_rescore.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.db import persist_job_assessment  # noqa: E402
from job_finder.web.job_scorer import score_job  # noqa: E402

log = logging.getLogger(__name__)


def select_batch_rows(
    conn: sqlite3.Connection,
    batch_size: int,
    seed: int,
    exclude_rescored: bool = True,
) -> list[str]:
    """Stratified sampling by legacy sonnet_score quartile.

    Returns dedup_keys, deterministic for a given (batch_size, seed) pair.
    SQL handles the NTILE(4) quartile assignment; Python's random.Random(seed)
    handles per-quartile shuffling so distinct seeds genuinely produce
    distinct row orders (SQLite has no seedable random — printf-based
    seeded ORDER BY does not actually re-shuffle when only a constant
    suffix differs across rows).
    """
    conn.row_factory = sqlite3.Row
    exclude_pred = " AND classification IS NULL " if exclude_rescored else ""
    sql = f"""
        SELECT
            dedup_key,
            NTILE(4) OVER (ORDER BY sonnet_score) AS quartile
        FROM jobs
        WHERE jd_full IS NOT NULL
          AND LENGTH(jd_full) >= 200
          AND sonnet_score IS NOT NULL
          {exclude_pred}
    """
    rows = conn.execute(sql).fetchall()

    by_quartile: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    for r in rows:
        by_quartile.setdefault(r["quartile"], []).append(r["dedup_key"])

    rng = random.Random(seed)
    per_quartile = batch_size // 4 + (1 if batch_size % 4 else 0)
    selected: list[str] = []
    for q in (1, 2, 3, 4):
        bucket = list(by_quartile.get(q, []))
        rng.shuffle(bucket)
        selected.extend(bucket[:per_quartile])

    return selected[:batch_size]


def _checkpoint_report(
    report_path: Path,
    results: list[dict],
    batch_number: int,
    seed: int,
    failures: int,
) -> None:
    """Flush partial report after every N rows for crash-resilience."""
    partial = {
        "batch_number": batch_number,
        "seed": seed,
        "partial": True,
        "row_count_rescored": sum(1 for r in results if r.get("status") == "ok"),
        "row_count_failed": failures,
        "per_row_results": results,
    }
    report_path.write_text(json.dumps(partial, indent=2))


def run_batch(
    conn: sqlite3.Connection,
    config: dict,
    dedup_keys: list[str],
    report_path: Path,
    batch_number: int,
    seed: int,
    force: bool = False,
) -> dict:
    """Score each row, persist via production path, collect per-row results."""
    conn.row_factory = sqlite3.Row
    results: list[dict] = []
    start = time.time()
    failures = 0
    model = (config.get("providers", {}).get("scoring") or {}).get("model")

    for i, key in enumerate(dedup_keys, 1):
        row = conn.execute(
            "SELECT * FROM jobs WHERE dedup_key = ?", (key,)
        ).fetchone()
        if row is None:
            results.append({"dedup_key": key, "status": "missing"})
            continue

        job = dict(row)

        if not force and job.get("classification"):
            results.append({
                "dedup_key": key,
                "status": "already_scored",
                "new_classification": job["classification"],
            })
            continue

        try:
            sr = score_job(job, conn, config)
        except Exception as exc:  # pragma: no cover - defensive
            failures += 1
            results.append({
                "dedup_key": key,
                "legacy_sonnet_score": job.get("sonnet_score"),
                "status": "error",
                "error": str(exc),
            })
            continue

        if sr.status != "ok" or sr.data is None:
            failures += 1
            results.append({
                "dedup_key": key,
                "legacy_sonnet_score": job.get("sonnet_score"),
                "status": sr.status,
                "provider": sr.provider,
                "error": sr.error,
            })
            continue

        persist_job_assessment(
            conn, key, sr.data, provider=sr.provider, model=model
        )

        results.append({
            "dedup_key": key,
            "legacy_sonnet_score": job.get("sonnet_score"),
            "new_sub_scores": dict(sr.data.sub_scores),
            "new_classification_placeholder": sr.data.classification,
            "provider": sr.provider,
            "model": model,
            "status": "ok",
            "error": None,
        })

        if i % 10 == 0:
            log.info(
                "batch %d progress: %d/%d rescored",
                batch_number, i, len(dedup_keys),
            )
            _checkpoint_report(report_path, results, batch_number, seed, failures)

    elapsed = round(time.time() - start, 1)
    report = {
        "batch_number": batch_number,
        "batch_size": len(dedup_keys),
        "seed": seed,
        "row_count_rescored": sum(1 for r in results if r.get("status") == "ok"),
        "row_count_failed": failures,
        "wall_clock_seconds": elapsed,
        "per_row_results": results,
    }
    report_path.write_text(json.dumps(report, indent=2))
    return report


def _load_db_path(config: dict, override: str | None) -> str:
    if override:
        return override
    return (
        (config.get("db") or {}).get("path")
        or (config.get("database") or {}).get("path")
        or "jobs.db"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="v3.0 rescore runner (Phase 34 Plan 4)"
    )
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--batch-number", type=int, required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--db-path", default=None)
    parser.add_argument(
        "--force-rescore",
        action="store_true",
        help="Bypass the classification-IS-NOT-NULL skip rail (for re-runs after fix commits).",
    )
    parser.add_argument("--config-path", default="config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = yaml.safe_load(Path(args.config_path).read_text())
    db_path = _load_db_path(config, args.db_path)
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        keys = select_batch_rows(
            conn, args.batch_size, args.seed,
            exclude_rescored=not args.force_rescore,
        )
        log.info("selected %d rows for batch %d", len(keys), args.batch_number)
        report = run_batch(
            conn, config, keys, report_path,
            args.batch_number, args.seed, force=args.force_rescore,
        )
        log.info(
            "batch %d complete: %d rescored, %d failed, %.0fs",
            args.batch_number,
            report["row_count_rescored"],
            report["row_count_failed"],
            report["wall_clock_seconds"],
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
