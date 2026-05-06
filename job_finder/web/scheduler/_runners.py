"""Heavy job-runner functions used by the scheduler's register helpers.

Two functions live here because their bodies bloated ``_jobs.py`` past
the per-file size target:

  - ``run_enrichment_backfill_two_stage`` -- the two-stage post-ingestion
    enrichment + scoring job (called from the ``enrichment_backfill``
    register helper at 1, 9, 17 cron).
  - ``run_health_check`` -- the daily 6:00 AM heartbeat that asserts key
    subsystems ran recently (called from the ``health_heartbeat`` register
    helper).

Both are pure functions of their inputs (no module-level state). Tests
that exercise the scheduler at registration level still patch
``BackgroundScheduler`` / ``_jobs.CronTrigger``; tests that exercise
the runner bodies (none currently) would patch this module directly.
"""

import logging

logger = logging.getLogger(__name__)


def run_enrichment_backfill_two_stage(db_path: str, config: dict) -> dict:
    """Run the post-ingestion enrichment backfill, then score new rows.

    Stage 1: fill ``jd_full`` via the cost-ordered tier pipeline.
    Stage 2: score every newly-enriched row.

    Without stage 2 the v3.0 multi-stage pipeline leaks: ingestion-time
    scoring sees empty ``jd_full`` and skips, and nothing else picks
    the row up.

    Returns a metrics dict consumed by the scheduler's tracked-job
    extract_metadata callable.
    """
    from job_finder.web.data_enricher import run_enrichment_backfill
    from job_finder.web.db_helpers import standalone_connection
    from job_finder.web.scoring_runner import run_scoring

    result = {
        "enriched": 0,
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "errors": [],
    }

    # Stage 1: enrichment
    try:
        serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
        enriched = run_enrichment_backfill(
            db_path,
            serpapi_key=serpapi_key,
            config=config,
            limit=200,
        )
        result["enriched"] = enriched if isinstance(enriched, int) else 0
        logger.info("Enrichment backfill: %s", result["enriched"])
    except Exception as e:
        logger.error("Enrichment backfill failed: %s", e)
        result["errors"].append(f"enrichment: {type(e).__name__}: {e}")
        return result

    # Stage 2: post-enrichment scoring
    try:
        with standalone_connection(db_path) as score_conn:
            rows = score_conn.execute(
                "SELECT dedup_key FROM jobs "
                "WHERE jd_full IS NOT NULL AND jd_full != '' "
                "AND classification IS NULL "
                "AND (pipeline_status IS NULL "
                "     OR pipeline_status NOT IN ('archived', 'dismissed')) "
                "LIMIT 500"
            ).fetchall()
        dedup_keys = [r[0] for r in rows]
        if not dedup_keys:
            logger.info("Post-enrichment scoring: nothing to score")
            return result
        summary = run_scoring(dedup_keys, config, db_path)
        result["scored"] = summary.get("scored", 0)
        result["classified_apply"] = summary.get("classified_apply", 0)
        result["classified_consider"] = summary.get("classified_consider", 0)
        result["classified_skip"] = summary.get("classified_skip", 0)
        result["classified_reject"] = summary.get("classified_reject", 0)
        logger.info("Post-enrichment scoring: %s", summary)
    except Exception as e:
        logger.error("Post-enrichment scoring failed: %s", e)
        result["errors"].append(f"post_scoring: {type(e).__name__}: {e}")

    return result


def run_health_check(app) -> None:
    """Daily health heartbeat -- verify key subsystems ran recently.

    Logs ``HEALTH_OK`` (info) when ingestion + stale detection + OAuth all
    look nominal, otherwise logs ``HEALTH_DEGRADED`` (warning) with a
    semicolon-joined list of issues. Best-effort; never raises.
    """
    with app.app_context():
        db_path = app.config.get("DB_PATH", "jobs.db")
        issues = []

        try:
            from job_finder.web.db_helpers import standalone_connection as _sc

            with _sc(db_path) as conn:
                # 1. Did ingestion run in the last 14 hours?
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action IN ('scheduled_sync', 'sync') "
                    "AND occurred_at >= datetime('now', '-14 hours')"
                ).fetchone()
                if not row[0]:
                    issues.append("No ingestion in last 14h")

                # 2. Did stale detection run last night?
                # Writer uses ACTION_SCHEDULED_STALENESS = 'scheduled_staleness'
                # (see activity_tracker.py). The legacy 'scheduled_stale_detection'
                # string is no longer emitted by any code path.
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action = 'scheduled_staleness' "
                    "AND occurred_at >= datetime('now', '-26 hours')"
                ).fetchone()
                if not row[0]:
                    issues.append("Stale detection missed last night")

                # 3. Are there recent consecutive errors from the same source?
                rows = conn.execute(
                    "SELECT action, COUNT(*) as cnt FROM user_activity "
                    "WHERE json_extract(metadata, '$.status') = 'failed' "
                    "AND occurred_at >= datetime('now', '-24 hours') "
                    "GROUP BY action HAVING cnt >= 5"
                ).fetchall()
                for r in rows:
                    issues.append(f"{r[0]}: {r[1]} failures in 24h")

                # 4. OAuth token validity
                try:
                    from job_finder.gmail_auth import get_credentials

                    get_credentials()
                except Exception as e:
                    issues.append(f"OAuth token invalid: {e}")

        except Exception as e:
            issues.append(f"Health check DB error: {e}")

        if issues:
            logger.warning("HEALTH_DEGRADED: %s", "; ".join(issues))
        else:
            logger.info("HEALTH_OK: ingestion, stale detection, OAuth all nominal")
