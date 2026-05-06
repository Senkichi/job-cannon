"""Scheduled-job registration helpers.

One register function per scheduled job. Each takes the running
``BackgroundScheduler`` instance and the Flask ``app``, and calls
``scheduler.add_job`` with the trigger appropriate to that job. The Flask
app is captured by reference inside each closure so the job can later
re-acquire app context at execution time.

The deferred-import idiom (``_import_func`` returns a no-arg callable) is
preserved from the legacy inline shape: heavy job modules
(``pipeline_runner``, ``careers_crawler``, ``agentic_enricher``, etc.)
do not load at scheduler-setup time — only when the job actually runs in
the background thread.
"""

import logging

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from job_finder.web.db_helpers import get_config_snapshot
from job_finder.web.scheduler._factories import _make_simple_job, _make_tracked_job

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ingestion pipeline (3x/day) -- custom logging, kept inline rather than
# routed through _make_tracked_job because the logged metadata fields
# diverge from the generic shape.
# ---------------------------------------------------------------------------


def register_ingestion(scheduler, app) -> None:
    """Register the 3x/day ingestion job (CronTrigger hour=0,8,16)."""

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


# ---------------------------------------------------------------------------
# Unified staleness check (nightly 2:00 AM). Replaces the old trio
# (stale_detection 2:00, expiry_check 2:30, liveness_check 3:00). Runs
# three phases in order: B (batch ATS reconciliation), A (time-based
# stale / archive), C (parallel HTTP cascade).
# See job_finder.web.expiry_checker.run_staleness_check.
# ---------------------------------------------------------------------------


def register_staleness(scheduler, app) -> None:
    """Register the nightly unified staleness check (2:00 AM)."""

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


# ---------------------------------------------------------------------------
# Pipeline detection (every 30 min)
# ---------------------------------------------------------------------------


def register_pipeline_detection(scheduler, app) -> None:
    """Register the 30-minute pipeline-detection job."""

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


# ---------------------------------------------------------------------------
# ATS scan (daily 7:00 AM)
# ---------------------------------------------------------------------------


def register_ats_scan(scheduler, app) -> None:
    """Register the daily ATS-scan job (7:00 AM)."""

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


# ---------------------------------------------------------------------------
# ATS slug probe (daily 7:30 AM)
# ---------------------------------------------------------------------------


def register_ats_slug_probe(scheduler, app) -> None:
    """Register the daily ATS-slug-probe job (7:30 AM)."""

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


# ---------------------------------------------------------------------------
# ATS source-URL promotion (daily 4:45 AM)
# ---------------------------------------------------------------------------


def register_ats_promote(scheduler, app) -> None:
    """Register the daily ATS source-URL promotion job (4:45 AM)."""

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


# ---------------------------------------------------------------------------
# Careers crawl (daily 5:00 AM)
# ---------------------------------------------------------------------------


def register_careers_crawl(scheduler, app) -> None:
    """Register the daily careers-crawl job (5:00 AM)."""

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


# ---------------------------------------------------------------------------
# Company linkage backfill (daily 5:00 AM)
# ---------------------------------------------------------------------------


def register_company_linkage(scheduler, app) -> None:
    """Register the daily company-linkage backfill (5:00 AM)."""

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


# ---------------------------------------------------------------------------
# Orphan cleanup (1st of month, 3:00 AM)
# ---------------------------------------------------------------------------


def register_orphan_cleanup(scheduler, app) -> None:
    """Register the monthly orphan-cleanup job (1st @ 3:00 AM)."""

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


# ---------------------------------------------------------------------------
# Homepage discovery (daily 6:30 AM)
# ---------------------------------------------------------------------------


def register_homepage_discovery(scheduler, app) -> None:
    """Register the daily homepage-discovery job (6:30 AM)."""

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


# ---------------------------------------------------------------------------
# Company enrichment (weekly, Sunday 4:00 AM). Uses retry-aware query that
# skips backoff and denylist companies. Switch to daily at 4:00 AM only
# after summary logs show meaningful backlog with acceptable failure rates.
# ---------------------------------------------------------------------------


def register_company_enrichment(scheduler, app) -> None:
    """Register the weekly company-enrichment job (Sun 4:00 AM)."""

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


# ---------------------------------------------------------------------------
# Registry hygiene (1st of month, 3:30 AM). Runs denylist cleanup then
# orphan cleanup in order. Replaces ad-hoc denylist splicing previously
# mixed into orphan cleanup internals.
# ---------------------------------------------------------------------------


def register_registry_hygiene(scheduler, app) -> None:
    """Register the monthly registry-hygiene job (1st @ 3:30 AM)."""

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


# ---------------------------------------------------------------------------
# Enrichment backfill (1 hour after each ingestion run: 1am, 9am, 5pm).
# Two stages: (1) fill jd_full via the cost-ordered tier pipeline,
# (2) score every newly-enriched row. Without stage 2 the v3.0 multi-stage
# pipeline leaks — ingestion-time scoring sees empty jd_full and skips,
# and nothing else picks the row up. Tracked via _make_tracked_job so
# user_activity captures every run (the dashboard's scheduler-health view
# depends on this).
# ---------------------------------------------------------------------------


def register_enrichment_backfill(scheduler, app) -> None:
    """Register the post-ingestion enrichment backfill (1, 9, 17)."""

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


# ---------------------------------------------------------------------------
# Agentic backfill (nightly 4:15 AM). Moved off the 3:30 slot to avoid
# colliding with day-1 monthly hygiene jobs (orphan_cleanup at 3:00,
# registry_hygiene at 3:30) and the tail of staleness_check (starts 2:00,
# runs ~2 hours). On May-1 the 3:30 collision DB-locked
# persist_job_expiry_state 113 times and killed the agentic run after
# enriching 1/50 jobs. 4:15 lands after staleness typically finishes
# (~4:00) and well before careers_crawl (5:00). OllamaProvider is
# instantiated INSIDE run_agentic_backfill, NOT here — the scheduler
# closure only defers the import.
# ---------------------------------------------------------------------------


def register_agentic_backfill(scheduler, app) -> None:
    """Register the nightly agentic-backfill job (4:15 AM)."""

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


# ---------------------------------------------------------------------------
# Health heartbeat (daily 6:00 AM)
# ---------------------------------------------------------------------------


def register_health_heartbeat(scheduler, app) -> None:
    """Register the daily health-heartbeat job (6:00 AM).

    Verifies key subsystems ran recently. Custom shape — does not route
    through _make_tracked_job because it doesn't follow the
    (db_path, config) -> result contract.
    """

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


def register_all_jobs(scheduler, app) -> None:
    """Register all 15 scheduled jobs on the given scheduler instance.

    Order matches the legacy inline shape so any future re-introduction of
    cross-job ordering invariants (e.g., agentic_backfill must register
    AFTER staleness so its metadata column appears second on the dashboard)
    has a stable insertion sequence.
    """
    register_ingestion(scheduler, app)
    register_staleness(scheduler, app)
    register_pipeline_detection(scheduler, app)
    register_ats_scan(scheduler, app)
    register_ats_slug_probe(scheduler, app)
    register_ats_promote(scheduler, app)
    register_careers_crawl(scheduler, app)
    register_company_linkage(scheduler, app)
    register_orphan_cleanup(scheduler, app)
    register_homepage_discovery(scheduler, app)
    register_company_enrichment(scheduler, app)
    register_registry_hygiene(scheduler, app)
    register_enrichment_backfill(scheduler, app)
    register_agentic_backfill(scheduler, app)
    register_health_heartbeat(scheduler, app)
