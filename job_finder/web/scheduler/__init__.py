"""APScheduler background scheduler for automatic job ingestion.

Runs Gmail, SerpAPI, and Thordata ingestion 3x/day (midnight, 8am, 4pm local).
The scheduler is started once per process via init_scheduler(app).

Guards:
1. Flask debug reloader guard: WERKZEUG_RUN_MAIN prevents double-start when
   Flask's reloader spawns a child process (run.py uses use_reloader=False,
   but this guard is a safety net).
2. Double-init guard: module-level _scheduler singleton prevents re-initialization
   if create_app() is called more than once in the same process.
3. Testing guard: scheduler is skipped when app.config["TESTING"] is True.
"""

import logging
import os
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from job_finder.web.db_helpers import get_config_snapshot
from job_finder.web.scheduler._factories import _make_simple_job, _make_tracked_job
from job_finder.web.scheduler._ollama import _ensure_ollama_running
from job_finder.web.scheduler._pidfile import _acquire_scheduler_pidfile

logger = logging.getLogger(__name__)

# Module-level singleton -- prevents double initialization
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


def init_scheduler(app) -> None:
    """Initialize and start the background scheduler.

    Called from create_app() after all blueprints are registered.
    Safe to call multiple times -- guards prevent double initialization.

    Args:
        app: Flask application instance (fully constructed).
    """
    global _scheduler

    # Guard 1: Skip in test mode
    if app.config.get("TESTING", False):
        logger.debug("Scheduler: skipped (TESTING=True)")
        return

    # Guard 2: Flask debug reloader -- skip in child process.
    # WERKZEUG_RUN_MAIN="true" is set by Flask's reloader in the child process.
    # run.py sets use_reloader=False so this guard normally never triggers, but
    # it is kept as a safety net in case the reloader is enabled by accident.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        logger.debug("Scheduler: skipped (werkzeug reloader child process)")
        return

    with _scheduler_lock:
        # Guard 3: Already initialized (same process)
        if _scheduler is not None:
            logger.debug("Scheduler: already initialized, skipping")
            return

        # Guard 4: Cross-process pidfile lock. If another Python process has
        # already claimed the pidfile and is still alive, skip scheduler start.
        if not _acquire_scheduler_pidfile(app):
            return

        # Eagerly start Ollama so the nightly agentic backfill (3:30 AM) has
        # a live service to talk to. Best-effort; never raises.
        try:
            _ensure_ollama_running(get_config_snapshot(app))
        except Exception as exc:
            logger.warning("Ollama auto-start helper raised unexpectedly: %s", exc)

        scheduler = BackgroundScheduler(daemon=True)

        # -- Ingestion pipeline (custom logging, kept inline) ---------------

        def run_pipeline():
            """Wrapped ingestion job executed by APScheduler."""
            import time as _time

            with app.app_context():
                from job_finder.web.activity_tracker import ACTION_SCHEDULED_SYNC, log_activity
                from job_finder.web.pipeline_runner import run_ingestion

                config = get_config_snapshot(app)
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    summary = run_ingestion(db_path, config)
                    logger.info(
                        "Scheduled ingestion: %d new jobs (gmail: %d, serpapi: %d, thordata: %d, dataforseo: %d)",
                        summary["jobs_new"],
                        summary["gmail_fetched"],
                        summary["serpapi_fetched"],
                        summary.get("thordata_fetched", 0),
                        summary.get("dataforseo_fetched", 0),
                    )
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_SYNC,
                        metadata={
                            "jobs_new": summary.get("jobs_new", 0),
                            "gmail_fetched": summary.get("gmail_fetched", 0),
                            "serpapi_fetched": summary.get("serpapi_fetched", 0),
                            "thordata_fetched": summary.get("thordata_fetched", 0),
                            "dataforseo_fetched": summary.get("dataforseo_fetched", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Scheduled ingestion failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_SYNC,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_pipeline,
            trigger=CronTrigger(hour="0,8,16"),
            id="ingestion_poll",
            replace_existing=True,
            max_instances=1,  # prevents overlap on long runs
            coalesce=True,  # skip missed runs if app was down
        )

        # -- Unified staleness check (nightly 2:00 AM) ---------------------
        # Replaces the old trio (stale_detection 2:00, expiry_check 2:30,
        # liveness_check 3:00). Runs three phases in order:
        #   B: batch ATS reconciliation
        #   A: time-based stale / archive
        #   C: parallel HTTP cascade
        # See job_finder.web.expiry_checker.run_staleness_check.

        def _import_staleness():
            from job_finder.web.expiry_checker import run_staleness_check

            return run_staleness_check

        def _import_staleness_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_STALENESS

            return ACTION_SCHEDULED_STALENESS

        scheduler.add_job(
            _make_tracked_job(
                app,
                "Staleness check",
                import_func=_import_staleness,
                import_action=_import_staleness_action,
                extract_metadata=lambda r: {
                    # Phase B (batch ATS reconciliation)
                    "batch_companies_checked": r.get("phase_b", {}).get("companies_checked", 0),
                    "batch_companies_skipped": r.get("phase_b", {}).get("companies_skipped", 0),
                    "batch_live": r.get("phase_b", {}).get("live", 0),
                    "batch_expired": r.get("phase_b", {}).get("expired", 0),
                    # Phase A (time-based stale detector)
                    "stale_marked": r.get("phase_a", {}).get("stale_marked", 0),
                    "stale_cleared": r.get("phase_a", {}).get("stale_cleared", 0),
                    "stale_archived": r.get("phase_a", {}).get("archived", 0),
                    # Phase C (parallel HTTP cascade)
                    "cascade_checked": r.get("phase_c", {}).get("checked", 0),
                    "cascade_live": r.get("phase_c", {}).get("live", 0),
                    "cascade_archived": r.get("phase_c", {}).get("archived", 0),
                    "cascade_inconclusive": r.get("phase_c", {}).get("inconclusive", 0),
                },
                guard=lambda config: config.get("staleness", {}).get(
                    "enabled",
                    config.get("expiry", {}).get("enabled", True),
                ),
            ),
            trigger=CronTrigger(hour=2, minute=0),
            id="staleness_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Pipeline detection (every 30 min) -----------------------------

        def _import_detection():
            from job_finder.web.pipeline_detector import run_pipeline_detection

            return run_pipeline_detection

        def _import_detection_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_PIPELINE_DETECTION

            return ACTION_SCHEDULED_PIPELINE_DETECTION

        scheduler.add_job(
            _make_tracked_job(
                app,
                "Pipeline detection",
                import_func=_import_detection,
                import_action=_import_detection_action,
                extract_metadata=lambda r: {
                    "emails_scanned": r.get("emails_scanned", 0),
                    "auto_updated": r.get("auto_updated", 0),
                    "queued": r.get("queued", 0),
                    "errors": r.get("errors", []),
                },
            ),
            trigger=IntervalTrigger(minutes=30),
            id="pipeline_detection",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- ATS scan (Mon/Wed 7:00 AM) ------------------------------------

        def _import_ats_scan():
            from job_finder.web.ats_scanner import run_ats_scan

            return run_ats_scan

        def _import_ats_scan_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_ATS_SCAN

            return ACTION_SCHEDULED_ATS_SCAN

        scheduler.add_job(
            _make_tracked_job(
                app,
                "ATS scan",
                import_func=_import_ats_scan,
                import_action=_import_ats_scan_action,
                extract_metadata=lambda r: {
                    "companies_scanned": r.get("companies_scanned", 0),
                    "jobs_found": r.get("jobs_discovered", 0),
                    "jobs_new": r.get("jobs_new", 0),
                },
                guard=lambda config: config.get("ats", {}).get("scan_enabled", True),
            ),
            trigger=CronTrigger(hour=7, minute=0),
            id="ats_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- ATS slug probe (Mon/Wed 7:30 AM) ------------------------------

        def _import_slug_probe():
            from job_finder.web.ats_scanner import probe_ats_slugs

            return probe_ats_slugs

        scheduler.add_job(
            _make_simple_job(app, "ATS slug probe", _import_slug_probe),
            trigger=CronTrigger(hour=7, minute=30),
            id="ats_slug_probe",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- ATS source-URL promotion (daily 4:45 AM) -------------------------

        def _import_ats_promote():
            from job_finder.web.ats_scanner import promote_ats_from_source_urls

            return promote_ats_from_source_urls

        scheduler.add_job(
            _make_simple_job(app, "ATS source-URL promotion", _import_ats_promote),
            trigger=CronTrigger(hour=4, minute=45),
            id="ats_source_url_promote",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Careers crawl (daily 5:00 AM) ------------------------------------

        def _import_careers_crawl():
            from job_finder.web.careers_crawler import crawl_careers_batch

            return crawl_careers_batch

        def _import_careers_crawl_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_CAREERS_CRAWL

            return ACTION_SCHEDULED_CAREERS_CRAWL

        scheduler.add_job(
            _make_tracked_job(
                app,
                "Careers crawl",
                import_func=_import_careers_crawl,
                import_action=_import_careers_crawl_action,
                extract_metadata=lambda r: {
                    "companies_crawled": r.get("companies_crawled", 0),
                    "jobs_found": r.get("jobs_found", 0),
                    "jobs_new": r.get("jobs_new", 0),
                    "playwright_rendered": r.get("playwright_rendered", 0),
                },
                guard=lambda config: config.get("careers_crawl", {}).get("enabled", True),
            ),
            trigger=CronTrigger(hour=5, minute=0),
            id="careers_crawl",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Company linkage backfill (daily 5:00 AM) ----------------------
        def _import_company_linkage():
            from job_finder.web.backfill_companies import run_company_linkage

            return run_company_linkage

        scheduler.add_job(
            _make_simple_job(app, "Company linkage", _import_company_linkage),
            trigger=CronTrigger(hour=5, minute=0),
            id="company_linkage",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Orphan cleanup (1st of month, 3:00 AM) ------------------------

        def _import_orphan_cleanup():
            from job_finder.web.backfill_companies import run_orphan_cleanup

            return run_orphan_cleanup

        scheduler.add_job(
            _make_simple_job(app, "Orphan cleanup", _import_orphan_cleanup),
            trigger=CronTrigger(day=1, hour=3, minute=0),
            id="orphan_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Homepage discovery (daily 6:30 AM) ----------------------------

        def _import_homepage_discovery():
            from job_finder.web.homepage_discoverer import run_homepage_discovery

            return run_homepage_discovery

        scheduler.add_job(
            _make_simple_job(app, "Homepage discovery", _import_homepage_discovery),
            trigger=CronTrigger(hour=6, minute=30),
            id="homepage_discovery",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Company enrichment (weekly, Sunday 4:00 AM) -------------------
        # Uses retry-aware query that skips backoff and denylist companies.
        # Switch to daily at 4:00 AM only after summary logs show meaningful
        # backlog with acceptable failure rates (manual judgment call).
        def _import_enrichment():
            from job_finder.web.backfill_companies import run_scheduled_enrichment

            return run_scheduled_enrichment

        scheduler.add_job(
            _make_simple_job(app, "Company enrichment", _import_enrichment),
            trigger=CronTrigger(day_of_week="sun", hour=4, minute=0),
            id="company_enrichment",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Registry hygiene (1st of month, 3:30 AM) ----------------------
        # Runs denylist cleanup then orphan cleanup in order.
        # Replaces the ad-hoc denylist splicing that was previously mixed
        # into orphan cleanup internals.

        def _import_registry_hygiene():
            from job_finder.web.backfill_companies import run_registry_hygiene

            return run_registry_hygiene

        scheduler.add_job(
            _make_simple_job(app, "Registry hygiene", _import_registry_hygiene),
            trigger=CronTrigger(day=1, hour=3, minute=30),
            id="registry_hygiene",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Enrichment backfill (1 hour after each ingestion run: 1am, 9am, 5pm Pacific) --
        # Two stages: (1) fill jd_full via the cost-ordered tier pipeline,
        # (2) score every newly-enriched row. Without stage 2 the v3.0
        # multi-stage pipeline leaks — ingestion-time scoring sees empty
        # jd_full and skips, and nothing else picks the row up.
        # Tracked via _make_tracked_job so user_activity captures every run
        # (the dashboard's scheduler-health view depends on this).

        def _import_enrichment_backfill():
            def _job(db_path, config):
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

            return _job

        def _import_enrichment_backfill_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_ENRICHMENT_BACKFILL

            return ACTION_SCHEDULED_ENRICHMENT_BACKFILL

        scheduler.add_job(
            _make_tracked_job(
                app,
                "Enrichment backfill",
                import_func=_import_enrichment_backfill,
                import_action=_import_enrichment_backfill_action,
                extract_metadata=lambda r: {
                    "jobs_enriched": r.get("enriched", 0),
                    "jobs_scored": r.get("scored", 0),
                    "classified_apply": r.get("classified_apply", 0),
                    "classified_consider": r.get("classified_consider", 0),
                    "classified_skip": r.get("classified_skip", 0),
                    "classified_reject": r.get("classified_reject", 0),
                    "errors": r.get("errors", []),
                },
            ),
            trigger=CronTrigger(hour="1,9,17"),
            id="enrichment_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Agentic backfill (nightly 4:15 AM) -----
        # Moved off the 3:30 slot to avoid colliding with day-1 monthly hygiene
        # jobs (orphan_cleanup at 3:00, registry_hygiene at 3:30) and the tail
        # of staleness_check (starts 2:00, runs ~2 hours). On May-1 the 3:30
        # collision DB-locked persist_job_expiry_state 113 times and killed the
        # agentic run after enriching 1/50 jobs.
        # 4:15 lands after staleness typically finishes (~4:00) and well before
        # careers_crawl (5:00).
        # OllamaProvider is instantiated INSIDE run_agentic_backfill, NOT here —
        # the scheduler closure only defers the import.

        def _import_agentic_backfill():
            from job_finder.web.agentic_enricher import run_agentic_backfill

            # Lambda wrapper matches the _import_stale pattern exactly:
            # returns a callable(db_path, config) rather than the raw function.
            return lambda db_path, config: run_agentic_backfill(db_path, config)

        def _import_agentic_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_AGENTIC_BACKFILL

            return ACTION_SCHEDULED_AGENTIC_BACKFILL

        scheduler.add_job(
            _make_tracked_job(
                app,
                "Agentic backfill",
                import_func=_import_agentic_backfill,
                import_action=_import_agentic_action,
                # "jobs_enriched" matches naming convention used by other tracked jobs
                # (jobs_found, jobs_new, jobs_scanned) for consistent dashboard display.
                extract_metadata=lambda r: {"jobs_enriched": r if isinstance(r, int) else 0},
            ),
            trigger=CronTrigger(hour=4, minute=15),
            id="agentic_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Health heartbeat (daily 6:00 AM) --------------------------------

        def _run_health_check():
            """Daily health heartbeat — verify key subsystems ran recently."""
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

        scheduler.add_job(
            _run_health_check,
            trigger=CronTrigger(hour=6, minute=0),
            id="health_heartbeat",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "Scheduler started: ingestion 3x/day (0:00, 8:00, 16:00 local); enrichment 1h after each (1:00, 9:00, 17:00 local)"
        )


def run_sync_now(app) -> dict:
    """Trigger an immediate ingestion run (for the Sync Now button).

    Runs synchronously in the current thread. Returns the ingestion summary.
    If the pipeline fails, returns an error summary dict.

    Args:
        app: Flask application instance.

    Returns:
        Summary dict from run_ingestion, or an error dict if ingestion failed.
    """
    from job_finder.web.pipeline_runner import run_ingestion

    config = get_config_snapshot(app)
    db_path = app.config.get("DB_PATH", "jobs.db")

    try:
        summary = run_ingestion(db_path, config, score=False)
        logger.info("Manual sync triggered: %d new jobs", summary.get("jobs_new", 0))
    except Exception as e:
        logger.error("Manual sync failed: %s", e)
        return {
            "gmail_fetched": 0,
            "gmail_errors": [str(e)],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "thordata_fetched": 0,
            "thordata_errors": [],
            "dataforseo_fetched": 0,
            "dataforseo_errors": [],
            "portal_search_fetched": 0,
            "portal_search_errors": [],
            "jobs_new": 0,
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.0,
            "error": str(e),
        }

    # Run pipeline detection after ingestion (non-blocking on failure)
    try:
        from job_finder.web.pipeline_detector import run_pipeline_detection

        detection_result = run_pipeline_detection(db_path, config)
        summary["detection_auto_updated"] = detection_result.get("auto_updated", 0)
        summary["detection_queued"] = detection_result.get("queued", 0)
        logger.info(
            "Manual sync detection: %d auto-updated, %d queued",
            summary["detection_auto_updated"],
            summary["detection_queued"],
        )
    except Exception as e:
        logger.error("Manual sync pipeline detection failed: %s", e)
        summary["detection_auto_updated"] = 0
        summary["detection_queued"] = 0

    return summary


def get_scheduler() -> BackgroundScheduler | None:
    """Return the running scheduler instance (or None if not started).

    Used for status checks and monitoring.
    """
    return _scheduler


def reset_scheduler() -> None:
    """Reset the scheduler singleton (test helper only).

    Shuts down the running scheduler if one exists, then clears the singleton.
    Only call this in tests to ensure a clean state between test runs.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                logger.debug("scheduler shutdown error", exc_info=True)
            _scheduler = None
