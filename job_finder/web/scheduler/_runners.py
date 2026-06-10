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
from typing import Any

from job_finder.secrets import get_secret

logger = logging.getLogger(__name__)


def run_enrichment_backfill_two_stage(
    db_path: str, config: dict, *, run_id: str | None = None
) -> dict[str, Any]:
    """Run the post-ingestion enrichment backfill, then score new rows.

    Stage 1: fill ``jd_full`` via the cost-ordered tier pipeline.
    Stage 2: score every newly-enriched row.

    Without stage 2 the v3.0 multi-stage pipeline leaks: ingestion-time
    scoring sees empty ``jd_full`` and skips, and nothing else picks
    the row up.

    Returns a metrics dict consumed by the scheduler's tracked-job
    extract_metadata callable.

    ``run_id`` (issue #215): the run-envelope correlation id from the
    scheduler/harness wrapper. Threaded into ``run_scoring`` so each
    per-job ``score`` event the orchestrator emits onto
    ``run_events.jsonl`` carries the same id as this run's
    ``run_start`` / ``run_end`` envelope.
    """
    from job_finder.web.data_enricher import run_enrichment_backfill
    from job_finder.web.db_helpers import standalone_connection
    from job_finder.web.scoring_runner import run_scoring

    result: dict[str, Any] = {
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
        serpapi_key = get_secret("sources.serpapi.api_key", config=config)
        enriched = run_enrichment_backfill(
            db_path,
            serpapi_key=serpapi_key,
            config=config,
            limit=None,
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
                "     OR pipeline_status NOT IN ('archived', 'dismissed'))"
            ).fetchall()
        dedup_keys = [r[0] for r in rows]
        if not dedup_keys:
            logger.info("Post-enrichment scoring: nothing to score")
            return result
        summary = run_scoring(dedup_keys, config, db_path, run_id=run_id)
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

    Routes the verdict to durable channels: writes one ``scheduled_health``
    row to ``user_activity`` (surfaces in the dashboard "User Activity" table
    via the existing ``meta.status`` branch) and emits a ``run_events``
    ``run_start``/``run_end`` envelope with ``disposition='degraded'`` when
    any issue was detected, ``'completed'`` otherwise. Both writers are
    no-raise, so the heartbeat's best-effort contract holds.
    """
    import time as _time

    from job_finder.web import run_events
    from job_finder.web.activity_tracker import ACTION_SCHEDULED_HEALTH, log_activity

    with app.app_context():
        db_path = app.config.get("DB_PATH", "jobs.db")
        issues: list[str] = []

        t0 = _time.time()
        run_id = run_events.start(job="health", source="scheduler", db_path=db_path)

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

        # 5. Autoheal: retry heals for still-degraded sources (run_heal gates
        #    flag/backoff/attempt-cap itself) + attempt-counter hygiene. The
        #    sweep never contributes to `issues` — it must not fail the heartbeat.
        try:
            from job_finder.web.autoheal.heal_pipeline import run_heal
            from job_finder.web.db_helpers import get_config_snapshot
            from job_finder.web.db_helpers import standalone_connection as _sc

            config = get_config_snapshot(app)
            reset_days = float(config.get("autoheal", {}).get("heal_attempt_reset_days", 30))
            with _sc(db_path) as conn:
                # Hygiene backstop (plan invariant I1): a source healthy for
                # 30+ days since its last heal gets its attempt budget back
                # even while an override is active.
                conn.execute(
                    "UPDATE source_health SET heal_attempts = 0 "
                    "WHERE status = 'healthy' AND heal_attempts > 0 "
                    "AND last_heal_at IS NOT NULL "
                    "AND last_heal_at < datetime('now', ?)",
                    (f"-{reset_days} days",),
                )
                conn.commit()
                degraded = [
                    r[0]
                    for r in conn.execute(
                        "SELECT source FROM source_health WHERE status = 'degraded'"
                    ).fetchall()
                ]
                for source in degraded:
                    try:
                        run_heal(conn, config, source)
                    except Exception:
                        logger.exception("health-check heal retry failed for %s", source)
        except Exception:
            logger.exception("health-check autoheal sweep failed")

        status = "degraded" if issues else "success"
        if issues:
            logger.warning("HEALTH_DEGRADED: %s", "; ".join(issues))
        else:
            logger.info("HEALTH_OK: ingestion, stale detection, OAuth all nominal")

        log_activity(
            db_path,
            ACTION_SCHEDULED_HEALTH,
            metadata={"status": status, "issues": issues},
        )
        run_events.end(
            run_id,
            job="health",
            source="scheduler",
            disposition="degraded" if issues else "completed",
            db_path=db_path,
            duration_s=round(_time.time() - t0, 2),
            result={"issues": issues},
        )
