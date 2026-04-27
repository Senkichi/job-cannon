"""One-shot: enrichment backfill + v3.0 scoring.

Fills jd_full for unenriched jobs (up to `limit`), then runs v3.0 scoring on
every row that now has jd_full but no classification. Used to exercise the
end-to-end Haiku/Sonnet path on the v3.0 schema after the multi-day gap where
the scheduled enrichment/agentic backfills did not run.
"""

import logging
import sqlite3
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("enrich_and_score")

from job_finder.web import create_app

app = create_app()

ENRICH_LIMIT = 200  # matches run_catchup.py default

with app.app_context():
    from job_finder.web.data_enricher import run_enrichment_backfill
    from job_finder.web.db_helpers import get_config_snapshot
    from job_finder.web.scoring_runner import run_scoring

    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")
    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
    logger.info("DB: %s  enrich_limit: %d", db_path, ENRICH_LIMIT)

    # ---- Stage 1: enrichment backfill ----
    logger.info("=" * 60)
    logger.info("START: Enrichment backfill")
    t0 = time.time()
    try:
        enriched = run_enrichment_backfill(
            db_path,
            serpapi_key=serpapi_key,
            config=config,
            limit=ENRICH_LIMIT,
        )
        elapsed = round(time.time() - t0, 1)
        logger.info("DONE enrichment (%ss): enriched_count=%d", elapsed, enriched)
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        logger.exception("FAIL enrichment (%ss): %s", elapsed, exc)
        sys.exit(1)

    # ---- Stage 2: collect dedup_keys of newly-fillable rows + score ----
    logger.info("=" * 60)
    logger.info("START: Collect scorable rows")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT dedup_key FROM jobs
               WHERE jd_full IS NOT NULL AND jd_full != ''
                 AND classification IS NULL
                 AND (pipeline_status IS NULL OR pipeline_status != 'archived')
               LIMIT 500"""
        ).fetchall()
    dedup_keys = [r[0] for r in rows]
    logger.info("DONE collect: %d dedup_keys with jd_full + no classification", len(dedup_keys))

    if not dedup_keys:
        logger.info("No scorable rows — exiting")
        sys.exit(0)

    logger.info("=" * 60)
    logger.info("START: v3.0 scoring on %d rows", len(dedup_keys))
    t0 = time.time()
    try:
        summary = run_scoring(dedup_keys, config, db_path)
        elapsed = round(time.time() - t0, 1)
        logger.info("DONE scoring (%ss):", elapsed)
        for k, v in summary.items():
            logger.info("  %s = %s", k, v)
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        logger.exception("FAIL scoring (%ss): %s", elapsed, exc)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("COMPLETE")
