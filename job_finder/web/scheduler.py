"""APScheduler background scheduler for automatic job ingestion.

Runs Gmail, SerpAPI, and Thordata ingestion 3x/day (midnight, 8am, 4pm Pacific).
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

logger = logging.getLogger(__name__)

# Module-level singleton -- prevents double initialization
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Job closure factories -- reduce per-job boilerplate
# ---------------------------------------------------------------------------

def _make_simple_job(app, name, import_func):
    """Factory for scheduler jobs that need only config + db_path + try/except.

    Args:
        app: Flask application instance.
        name: Human-readable job name for log messages.
        import_func: No-arg callable that returns the job function.
            Called lazily inside the closure to defer imports.
            The returned function must accept (db_path, config).
    """
    def wrapper():
        with app.app_context():
            config = get_config_snapshot(app)
            db_path = app.config.get("DB_PATH", "jobs.db")
            try:
                result = import_func()(db_path, config)
                logger.info("%s: %s", name, result)
            except Exception as e:
                logger.error("%s failed: %s", name, e)
    return wrapper

def _make_tracked_job(app, name, import_func, import_action, extract_metadata,
                      *, guard=None):
    """Factory for scheduler jobs with timing and activity logging.

    Returns a zero-arg ``wrapper`` closure suitable for ``scheduler.add_job``.
    The double-indirection in ``import_func`` is intentional: it defers the
    import of heavy job modules until the job actually runs in the background
    thread, rather than at scheduler-setup time inside ``init_scheduler``.

    Args:
        app: Flask application instance.
        name: Human-readable job name for log messages.
        import_func: No-arg callable that returns the job function.
            Called lazily inside the closure to defer imports.
            The returned function must accept (db_path, config).
        import_action: No-arg callable that returns the activity action constant.
            Also called lazily to defer activity_tracker imports.
        extract_metadata: Callable(result) -> dict of metadata fields for
            the success activity log entry. duration_seconds and status are
            added automatically.
        guard: Optional callable(config) -> bool. If provided and returns
            False, the job exits early without running.
    """
    def wrapper():
        import time as _time
        with app.app_context():
            from job_finder.web.activity_tracker import log_activity
            config = get_config_snapshot(app)
            db_path = app.config.get("DB_PATH", "jobs.db")
            action = import_action()

            if guard is not None and not guard(config):
                return

            t0 = _time.time()
            try:
                result = import_func()(db_path, config)
                logger.info("%s: %s", name, result)
                metadata = extract_metadata(result)
                metadata["duration_seconds"] = round(_time.time() - t0, 2)
                metadata["status"] = "success"
                log_activity(db_path, action, metadata=metadata)
            except Exception as e:
                logger.error("%s failed: %s", name, e)
                log_activity(
                    db_path, action,
                    metadata={
                        "status": "failed",
                        "error": type(e).__name__,
                        "duration_seconds": round(_time.time() - t0, 2),
                    },
                )
    return wrapper

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
        # Guard 3: Already initialized
        if _scheduler is not None:
            logger.debug("Scheduler: already initialized, skipping")
            return

        scheduler = BackgroundScheduler(daemon=True)

        # -- Ingestion pipeline (custom logging, kept inline) ---------------

        def run_pipeline():
            """Wrapped ingestion job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import log_activity, ACTION_SCHEDULED_SYNC
                from job_finder.web.pipeline_runner import run_ingestion
                config = get_config_snapshot(app)
                db_path = app.config.get("DB_PATH", "jobs.db")
                t0 = _time.time()
                try:
                    summary = run_ingestion(db_path, config)
                    logger.info(
                        "Scheduled ingestion: %d new jobs (gmail: %d, serpapi: %d, thordata: %d, scaleserp: %d, dataforseo: %d)",
                        summary["jobs_new"],
                        summary["gmail_fetched"],
                        summary["serpapi_fetched"],
                        summary.get("thordata_fetched", 0),
                        summary.get("scaleserp_fetched", 0),
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
                            "scaleserp_fetched": summary.get("scaleserp_fetched", 0),
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
            trigger=CronTrigger(hour="0,8,16", timezone="US/Pacific"),
            id="ingestion_poll",
            replace_existing=True,
            max_instances=1,   # prevents overlap on long runs
            coalesce=True,     # skip missed runs if app was down
        )

        # -- Stale detection (nightly) -------------------------------------

        def _import_stale():
            from job_finder.web.stale_detector import run_stale_detection
            return lambda db_path, config: run_stale_detection(db_path, config)

        def _import_stale_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_STALE_DETECTION
            return ACTION_SCHEDULED_STALE_DETECTION

        scheduler.add_job(
            _make_tracked_job(
                app, "Stale detection",
                import_func=_import_stale,
                import_action=_import_stale_action,
                extract_metadata=lambda r: {
                    "stale_marked": r.get("stale_marked", 0),
                    "stale_cleared": r.get("stale_cleared", 0),
                    "archived": r.get("archived", 0),
                },
            ),
            trigger=CronTrigger(hour=2, minute=0),
            id="stale_detection",
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
                app, "Pipeline detection",
                import_func=_import_detection,
                import_action=_import_detection_action,
                extract_metadata=lambda r: {
                    "emails_scanned": r.get("emails_scanned", 0),
                    "auto_updated": r.get("auto_updated", 0),
                    "queued": r.get("queued", 0),
                },
            ),
            trigger=IntervalTrigger(minutes=30),
            id="pipeline_detection",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Drive feedback poll (every 30 min) ----------------------------

        def _import_feedback():
            from job_finder.web.resume_feedback import run_drive_feedback_poll
            return run_drive_feedback_poll

        scheduler.add_job(
            _make_simple_job(app, "Drive feedback poll", _import_feedback),
            trigger=IntervalTrigger(minutes=30),
            id="drive_feedback_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Preference consolidation (weekly, Sunday 3:00 AM) -------------

        def _import_consolidation():
            from job_finder.web.resume_feedback import run_preference_consolidation
            return run_preference_consolidation

        scheduler.add_job(
            _make_simple_job(app, "Preference consolidation", _import_consolidation),
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
            id="preference_consolidation",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Rejection analysis (weekly, Monday 3:00 AM) -------------------

        def _import_rejection():
            from job_finder.web.rejection_analyzer import run_rejection_analysis
            return run_rejection_analysis

        def _import_rejection_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_REJECTION_ANALYSIS
            return ACTION_SCHEDULED_REJECTION_ANALYSIS

        scheduler.add_job(
            _make_tracked_job(
                app, "Rejection analysis",
                import_func=_import_rejection,
                import_action=_import_rejection_action,
                extract_metadata=lambda r: {
                    "rejections_analyzed": r.get("rejections_analyzed", 0),
                    "budget_exceeded": r.get("budget_exceeded", False),
                },
            ),
            trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
            id="rejection_analysis",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Rejection pattern analysis (weekly, Tuesday 3:00 AM) -----------

        def _import_rejection_patterns():
            from job_finder.web.rejection_patterns import run_rejection_pattern_analysis
            return run_rejection_pattern_analysis

        scheduler.add_job(
            _make_simple_job(app, "Rejection patterns", _import_rejection_patterns),
            trigger=CronTrigger(day_of_week="tue", hour=3, minute=0),
            id="rejection_patterns",
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
                app, "ATS scan",
                import_func=_import_ats_scan,
                import_action=_import_ats_scan_action,
                extract_metadata=lambda r: {
                    "companies_scanned": r.get("companies_scanned", 0),
                    "jobs_found": r.get("jobs_found", 0),
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

        # -- Careers crawl (daily 5:00 AM) ------------------------------------

        def _import_careers_crawl():
            from job_finder.web.careers_crawler import crawl_careers_batch
            return crawl_careers_batch

        def _import_careers_crawl_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_CAREERS_CRAWL
            return ACTION_SCHEDULED_CAREERS_CRAWL

        scheduler.add_job(
            _make_tracked_job(
                app, "Careers crawl",
                import_func=_import_careers_crawl,
                import_action=_import_careers_crawl_action,
                extract_metadata=lambda r: {
                    "companies_crawled": r.get("companies_crawled", 0),
                    "jobs_found": r.get("jobs_found", 0),
                    "jobs_new": r.get("jobs_new", 0),
                    "playwright_rendered": r.get("playwright_rendered", 0),
                },
                guard=lambda config: config.get("careers_crawl", {}).get(
                    "enabled", True
                ),
            ),
            trigger=CronTrigger(hour=5, minute=0),
            id="careers_crawl",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Expiry check (nightly 2:30 AM) --------------------------------

        def _import_expiry():
            from job_finder.web.expiry_checker import run_expiry_check
            return run_expiry_check

        def _import_expiry_action():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_EXPIRY_CHECK
            return ACTION_SCHEDULED_EXPIRY_CHECK

        scheduler.add_job(
            _make_tracked_job(
                app, "Expiry check",
                import_func=_import_expiry,
                import_action=_import_expiry_action,
                extract_metadata=lambda r: {
                    "checked": r.get("checked", 0),
                    "archived": r.get("archived", 0),
                    "live": r.get("live", 0),
                    "inconclusive": r.get("inconclusive", 0),
                },
                guard=lambda config: config.get("expiry", {}).get("enabled", True),
            ),
            trigger=CronTrigger(hour=2, minute=30),
            id="expiry_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- URL liveness check (nightly 3:00 AM) ----------------------------

        def _import_liveness_check():
            from job_finder.web.liveness_checker import run_liveness_check
            return run_liveness_check

        def _import_liveness_action():
            from job_finder.web.activity_tracker import ACTION_LIVENESS_CHECK
            return ACTION_LIVENESS_CHECK

        scheduler.add_job(
            _make_tracked_job(
                app, "Liveness check",
                import_func=_import_liveness_check,
                import_action=_import_liveness_action,
                extract_metadata=lambda r: {
                    "checked": r.get("checked", 0),
                    "expired": r.get("expired", 0),
                },
            ),
            trigger=CronTrigger(hour=3, minute=0),
            id="liveness_check",
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

        def _run_enrichment_backfill():
            """Backfill enrichment for jobs that were missed or prematurely exhausted."""
            with app.app_context():
                config = get_config_snapshot(app)
                db_path = app.config.get("DB_PATH", "jobs.db")
                try:
                    from job_finder.web.data_enricher import run_enrichment_backfill
                    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
                    result = run_enrichment_backfill(
                        db_path,
                        serpapi_key=serpapi_key,
                        config=config,
                        limit=200,
                    )
                    logger.info("Enrichment backfill: %s", result)
                except Exception as e:
                    logger.error("Enrichment backfill failed: %s", e)

        scheduler.add_job(
            _run_enrichment_backfill,
            trigger=CronTrigger(hour="1,9,17", timezone="US/Pacific"),
            id="enrichment_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # -- Agentic backfill (nightly 3:30 AM, paused until manual resume) -----
        # Uses lambda-wrapper pattern matching _import_stale (lines above) to prevent
        # signature drift if run_agentic_backfill gains new required parameters.
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
                app, "Agentic backfill",
                import_func=_import_agentic_backfill,
                import_action=_import_agentic_action,
                # "jobs_enriched" matches naming convention used by other tracked jobs
                # (jobs_found, jobs_new, jobs_scanned) for consistent dashboard display.
                extract_metadata=lambda r: {"jobs_enriched": r if isinstance(r, int) else 0},
            ),
            trigger=CronTrigger(hour=3, minute=30),
            id="agentic_backfill",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        scheduler.start()
        _scheduler = scheduler
        logger.info("Scheduler started: ingestion 3x/day (0:00, 8:00, 16:00 Pacific); enrichment 1h after each (1:00, 9:00, 17:00 Pacific)")

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
        summary = run_ingestion(db_path, config)
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
            "scaleserp_fetched": 0,
            "scaleserp_errors": [],
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
