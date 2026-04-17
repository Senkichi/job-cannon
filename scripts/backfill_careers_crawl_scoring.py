"""Backfill enrichment + rescoring for careers_crawl jobs scored without JD.

Target: jobs where sources includes 'careers_crawl' and description is empty.
These jobs were Haiku-scored on title alone because the crawler produces
title+URL shells and, until the associated bug fix, the scorer ran without
enrichment and without a JD guard. Their haiku_score/sonnet_score are
meaningless and need to be cleared, re-enriched, and rescored.

Safe by default: --dry-run shows what would change, no writes until --commit.

Usage:
    uv run --active python scripts/backfill_careers_crawl_scoring.py --dry-run
    uv run --active python scripts/backfill_careers_crawl_scoring.py --commit
    uv run --active python scripts/backfill_careers_crawl_scoring.py --commit --limit 20
"""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Make job_finder importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_finder.config import DEFAULT_HAIKU_THRESHOLD, load_config
from job_finder.db import JOBS_ALL_COLUMNS
from job_finder.web.data_enricher import enrich_job
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_orchestrator import (
    load_scoring_profile,
    score_and_persist_haiku,
    score_and_persist_sonnet,
)

logger = logging.getLogger("backfill_careers_crawl")

# Fields to wipe before re-enrichment. Leaves title/company/URL intact.
_CLEAR_SQL = """
    UPDATE jobs
       SET haiku_score = NULL,
           haiku_summary = NULL,
           sonnet_score = NULL,
           fit_analysis = NULL,
           opus_score = NULL,
           score = NULL,
           score_breakdown = NULL,
           enrichment_tier = NULL
     WHERE dedup_key = ?
"""

_FIND_AFFECTED_SQL = """
    SELECT dedup_key, title, company, haiku_score, sonnet_score,
           CASE WHEN jd_full IS NULL OR jd_full='' THEN 0 ELSE 1 END AS has_jd
      FROM jobs
     WHERE sources LIKE '%careers_crawl%'
       AND (description IS NULL OR description = '')
       AND haiku_score IS NOT NULL
     ORDER BY haiku_score DESC
"""


def find_affected(conn: sqlite3.Connection, limit: int | None) -> list[dict]:
    sql = _FIND_AFFECTED_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def process_one(
    db_path: str,
    dedup_key: str,
    config: dict,
    profile: dict,
    serpapi_key: str | None,
    threshold: int,
) -> dict:
    """Clear + re-enrich + rescore a single job. Returns outcome dict."""
    outcome = {
        "dedup_key": dedup_key,
        "cleared": False,
        "enriched_fields": [],
        "haiku_score": None,
        "sonnet_scored": False,
        "skipped_reason": None,
        "error": None,
    }

    try:
        with standalone_connection(db_path) as conn:
            # 1. Clear stale scores + reset enrichment_tier so the pipeline
            #    runs all tiers fresh (otherwise it'll resume from "exhausted").
            conn.execute(_CLEAR_SQL, (dedup_key,))
            conn.commit()
            outcome["cleared"] = True

            # 2. Reload row post-clear
            row = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if row is None:
                outcome["skipped_reason"] = "row vanished after clear"
                return outcome
            job_row = dict(row)

            # 3. Enrich. enrich_job persists to DB and returns enriched fields.
            try:
                enriched = enrich_job(
                    job_row,
                    serpapi_key=serpapi_key,
                    conn=conn,
                    config=config,
                )
                if enriched:
                    outcome["enriched_fields"] = list(enriched.keys())
                    job_row.update(enriched)
            except Exception as enrich_err:
                logger.warning(
                    "enrichment failed for %s: %s", dedup_key, enrich_err,
                )
                # Continue — Haiku guard will skip if still empty

            # 4. Rescore with Haiku (new JD guard will skip if still empty)
            result = score_and_persist_haiku(conn, job_row, config, profile)
            if result is None:
                outcome["skipped_reason"] = (
                    "haiku skipped (no JD after enrichment)"
                )
                return outcome

            score = result.get("score", 0)
            outcome["haiku_score"] = score

            # 5. Sonnet eval if above threshold and JD present
            if score >= threshold and job_row.get("jd_full"):
                refreshed = conn.execute(
                    f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
                    (dedup_key,),
                ).fetchone()
                if refreshed:
                    s_result = score_and_persist_sonnet(
                        conn, dict(refreshed), config, profile,
                    )
                    if s_result is not None:
                        outcome["sonnet_scored"] = True

    except Exception as e:
        outcome["error"] = str(e)
        logger.exception("process_one failed for %s", dedup_key)

    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="Actually write changes (default: dry-run).")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview affected jobs without writing (default).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N jobs.")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    db_path = config.get("database", {}).get("path", "jobs.db")
    db_path = str(Path(db_path).resolve())
    logger.info("Using DB: %s", db_path)

    # Scope check
    with standalone_connection(db_path) as conn:
        affected = find_affected(conn, args.limit)

    if not affected:
        logger.info("No affected jobs found — nothing to do.")
        return 0

    has_jd = sum(1 for r in affected if r["has_jd"])
    no_jd = len(affected) - has_jd
    print(f"\nAffected jobs: {len(affected)}")
    print(f"  already have jd_full (rescore only): {has_jd}")
    print(f"  no jd_full (need full enrichment):   {no_jd}")
    print(f"\nTop 10 by current Haiku score:")
    for r in affected[:10]:
        jd_flag = "[jd]" if r["has_jd"] else "[no-jd]"
        print(f"  {int(r['haiku_score']):3d} {jd_flag:7s} {r['company'][:25]:25s} "
              f"| {r['title'][:60]}")

    if args.commit:
        print(f"\n=== COMMIT MODE: processing {len(affected)} jobs ===\n")
    else:
        print(f"\n=== DRY RUN: re-run with --commit to apply ===")
        return 0

    profile = load_scoring_profile(config)
    threshold = config.get("scoring", {}).get(
        "haiku_threshold", DEFAULT_HAIKU_THRESHOLD,
    )
    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")

    stats = {
        "processed": 0,
        "cleared": 0,
        "enriched": 0,
        "rescored": 0,
        "still_empty": 0,
        "sonnet": 0,
        "errors": 0,
    }
    started = datetime.now()

    for i, row in enumerate(affected, 1):
        outcome = process_one(
            db_path, row["dedup_key"], config, profile,
            serpapi_key, threshold,
        )
        stats["processed"] += 1
        if outcome["cleared"]:
            stats["cleared"] += 1
        if outcome["enriched_fields"]:
            stats["enriched"] += 1
        if outcome["haiku_score"] is not None:
            stats["rescored"] += 1
        if outcome["skipped_reason"] == "haiku skipped (no JD after enrichment)":
            stats["still_empty"] += 1
        if outcome["sonnet_scored"]:
            stats["sonnet"] += 1
        if outcome["error"]:
            stats["errors"] += 1

        status = "OK"
        if outcome["error"]:
            status = f"ERR: {outcome['error'][:50]}"
        elif outcome["skipped_reason"]:
            status = f"SKIP: {outcome['skipped_reason']}"
        elif outcome["haiku_score"] is not None:
            status = f"Haiku={outcome['haiku_score']}"
            if outcome["sonnet_scored"]:
                status += " +Sonnet"

        logger.info(
            "[%3d/%3d] %s @ %s — %s (enriched: %s)",
            i, len(affected), row["title"][:40], row["company"][:20], status,
            ",".join(outcome["enriched_fields"]) or "none",
        )

    elapsed = (datetime.now() - started).total_seconds()
    print(f"\n=== Backfill complete in {elapsed:.1f}s ===")
    for k, v in stats.items():
        print(f"  {k:15s}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
