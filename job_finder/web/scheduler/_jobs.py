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
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from job_finder.web.db_helpers import get_config_snapshot
from job_finder.web.live_events import (
    COMPANIES_CHANGED,
    COSTS_CHANGED,
    DETECTIONS_CHANGED,
    JOBS_CHANGED,
    PIPELINE_CHANGED,
)
from job_finder.web.live_events import publish as publish_live
from job_finder.web.scheduler._factories import _make_simple_job, _make_tracked_job
from job_finder.web.scheduler._schedule import (
    HEAVY_MISFIRE_GRACE_S,
    LIGHT_MISFIRE_GRACE_S,
    assert_no_heavy_writer_collisions,
    enrichment_hour_expr,
    ingestion_hour_expr,
)

logger = logging.getLogger(__name__)


def _make_chainer(scheduler, successor_id: str, successor_wrapper):
    """Return a no-arg on_complete hook that schedules ``successor_wrapper`` as a
    one-shot DateTrigger(now), replacing any pending one-shot for the same id.

    Completion-chaining (#229 design 3a): the successor has no cron trigger of
    its own; it runs exactly once each time its predecessor finishes (on success
    OR failure — the hook fires from the predecessor wrapper's ``finally``).
    ``replace_existing=True`` keeps a single pending successor job if a
    predecessor somehow completes twice before the successor drains.
    """

    def _release() -> None:
        scheduler.add_job(
            successor_wrapper,
            trigger=DateTrigger(run_date=datetime.now()),
            id=successor_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
        )

    return _release


# ---------------------------------------------------------------------------
# Ingestion pipeline (3x/day) -- custom logging, kept inline rather than
# routed through _make_tracked_job because the logged metadata fields
# diverge from the generic shape.
# ---------------------------------------------------------------------------


# Back-compat alias — the canonical cadence map now lives in _schedule.py
# (single source of truth shared by ingestion AND enrichment_backfill). Kept so
# existing imports of `_cadence_to_hour_expr` from this module keep working.
#
# Note: rescheduling takes effect only on app restart — there is no
# live-reschedule path.
_cadence_to_hour_expr = ingestion_hour_expr


def register_ingestion(scheduler, app) -> None:
    """Register the ingestion job using the cadence_preset from config."""

    def run_pipeline():
        """Wrapped ingestion job executed by APScheduler."""
        import time as _time
        from datetime import datetime as _dt

        with app.app_context():
            from job_finder.web.activity_tracker import ACTION_SCHEDULED_SYNC, log_activity
            from job_finder.web.pipeline_runner import run_ingestion

            config = get_config_snapshot(app)
            db_path = app.config.get("DB_PATH", "jobs.db")
            t0 = _time.time()
            # Load-bearing decision #8 (NO-KEY-COMPENSATION-PLAN.md): Google CSE
            # runs once per day on the 8 AM slot. Free-API portals run every slot.
            # Manual "Sync Now" via _sync.run_immediate_sync leaves include_cse=True.
            include_cse = _dt.now().hour == 8
            try:
                summary = run_ingestion(db_path, config, include_cse=include_cse)
                logger.info(
                    "Scheduled ingestion: %d new jobs (gmail: %d, serpapi: %d, dataforseo: %d)",
                    summary["jobs_new"],
                    summary["gmail_fetched"],
                    summary["serpapi_fetched"],
                    summary.get("dataforseo_fetched", 0),
                )
                metadata = {
                    "jobs_new": summary.get("jobs_new", 0),
                    "gmail_fetched": summary.get("gmail_fetched", 0),
                    "serpapi_fetched": summary.get("serpapi_fetched", 0),
                    "dataforseo_fetched": summary.get("dataforseo_fetched", 0),
                    "portal_search_fetched": summary.get("portal_search_fetched", 0),
                    "duration_seconds": round(_time.time() - t0, 2),
                    "status": "success",
                }
                # Per-portal counts (portal_<name>_fetched keys) — match the
                # action='sync' schema so the dashboard's Recent Activity can
                # surface USAJobs/Adzuna/Jooble/etc. for both manual + scheduled
                # paths. Zero-yield portals are absent from summary by design.
                for k, v in summary.items():
                    if (
                        k.startswith("portal_")
                        and k.endswith("_fetched")
                        and k != "portal_search_fetched"
                    ):
                        metadata[k] = v
                log_activity(db_path, ACTION_SCHEDULED_SYNC, metadata=metadata)
                # New rows, fresh scores/costs, and pipeline-status churn — nudge
                # every live widget that reflects them.
                for _ev in (JOBS_CHANGED, COSTS_CHANGED, PIPELINE_CHANGED):
                    publish_live(_ev)
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

    config = get_config_snapshot(app)
    preset = config.get("scheduler", {}).get("cadence_preset", "standard")
    scheduler.add_job(
        run_pipeline,
        trigger=CronTrigger(hour=ingestion_hour_expr(preset)),
        id="ingestion_poll",
        replace_existing=True,
        max_instances=1,  # prevents overlap on long runs
        coalesce=True,  # skip missed runs if app was down
        misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
    )


# ---------------------------------------------------------------------------
# Unified staleness check (nightly 2:00 AM). Replaces the old trio
# (stale_detection 2:00, expiry_check 2:30, liveness_check 3:00). Runs
# three phases in order: B (batch ATS reconciliation), C (parallel HTTP
# cascade), A (time-based stale / archive — last, so it judges against
# the liveness evidence B and C just refreshed).
# See job_finder.web.expiry_checker.run_staleness_check.
# ---------------------------------------------------------------------------


def register_staleness(scheduler, app, *, on_complete=None) -> None:
    """Register the nightly unified staleness check (2:00 AM).

    Phase C runs a parallel HTTP cascade whose runtime scales with the live-job
    count and a per-job request timeout; the run can drift well past its 2:00
    slot. ``misfire_grace_time`` bounds a *late start* (coalesce drops a fire
    that could not start within the window rather than replaying it on top of a
    later run), and ``on_complete`` chains the agentic backfill off this job's
    completion instead of racing it on a fixed 4:15 slot — the heaviest two
    nightly DB writers can no longer overlap regardless of how long staleness
    actually takes.
    """

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
            on_complete=on_complete,
            extract_metadata=lambda r: {
                # Phase B (batch ATS reconciliation)
                "batch_companies_checked": r.get("phase_b", {}).get("companies_checked", 0),
                "batch_companies_skipped": r.get("phase_b", {}).get("companies_skipped", 0),
                "batch_live": r.get("phase_b", {}).get("live", 0),
                "batch_expired": r.get("phase_b", {}).get("expired", 0),
                "batch_unparseable": r.get("phase_b", {}).get("unparseable", 0),
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
            publish_events=(JOBS_CHANGED, PIPELINE_CHANGED),
        ),
        trigger=CronTrigger(hour=2, minute=0),
        id="staleness_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
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
            publish_events=(PIPELINE_CHANGED, DETECTIONS_CHANGED, JOBS_CHANGED),
        ),
        trigger=IntervalTrigger(minutes=30),
        id="pipeline_detection",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=LIGHT_MISFIRE_GRACE_S,
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
            publish_events=(JOBS_CHANGED, COMPANIES_CHANGED),
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
        _make_simple_job(
            app, "ATS slug probe", _import_slug_probe, publish_events=(COMPANIES_CHANGED,)
        ),
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
        _make_simple_job(
            app,
            "ATS source-URL promotion",
            _import_ats_promote,
            publish_events=(COMPANIES_CHANGED, JOBS_CHANGED),
        ),
        trigger=CronTrigger(hour=4, minute=45),
        id="ats_source_url_promote",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


# ---------------------------------------------------------------------------
# Careers crawl (daily 5:00 AM)
# ---------------------------------------------------------------------------


def register_careers_crawl(scheduler, app, *, on_complete=None) -> None:
    """Register the daily careers-crawl job (5:00 AM).

    Chains ``company_linkage`` off completion (#229): the two used to share the
    05:00 slot and both write the companies/jobs tables, contending on DB locks.
    company_linkage now runs as a one-shot when this job finishes, never on a
    cron slot of its own.
    """

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
            on_complete=on_complete,
            extract_metadata=lambda r: {
                "companies_crawled": r.get("companies_crawled", 0),
                "jobs_found": r.get("jobs_found", 0),
                "jobs_new": r.get("jobs_new", 0),
                "playwright_rendered": r.get("playwright_rendered", 0),
            },
            guard=lambda config: config.get("careers_crawl", {}).get("enabled", True),
            publish_events=(JOBS_CHANGED, COMPANIES_CHANGED),
        ),
        trigger=CronTrigger(hour=5, minute=0),
        id="careers_crawl",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
    )


# ---------------------------------------------------------------------------
# Company linkage backfill — chained off careers_crawl completion (no cron).
# Previously fired at 05:00, the same slot as careers_crawl; both are heavy
# writers, so the shared slot DB-locked under contention. Now a one-shot
# released by careers_crawl's on_complete hook.
# ---------------------------------------------------------------------------


def build_company_linkage_wrapper(app):
    """Build (but do not schedule) the company-linkage job wrapper.

    Returned wrapper is scheduled as a one-shot by careers_crawl's chainer.
    """

    def _import_company_linkage():
        from job_finder.web.backfill_companies import run_company_linkage

        return run_company_linkage

    return _make_simple_job(
        app,
        "Company linkage",
        _import_company_linkage,
        publish_events=(COMPANIES_CHANGED, JOBS_CHANGED),
    )


# ---------------------------------------------------------------------------
# Primary-source resolution (daily 5:45 AM). Company-batched direct_url
# resolver — runs after ats_promote (4:45) and careers_crawl /
# company_linkage (5:00) so freshly promoted ATS slugs are picked up
# same-day, and clear of the 2:00-4:00 staleness window (the 3:30→4:15
# agentic-backfill DB-lock precedent).
# ---------------------------------------------------------------------------


def register_primary_source_resolution(scheduler, app) -> None:
    """Register the daily primary-source resolution job (5:45 AM)."""

    def _import_primary_source():
        from job_finder.web.primary_source_resolver import run_primary_source_resolution

        return run_primary_source_resolution

    def _import_primary_source_action():
        from job_finder.web.activity_tracker import ACTION_SCHEDULED_PRIMARY_SOURCE

        return ACTION_SCHEDULED_PRIMARY_SOURCE

    scheduler.add_job(
        _make_tracked_job(
            app,
            "Primary-source resolution",
            import_func=_import_primary_source,
            import_action=_import_primary_source_action,
            extract_metadata=lambda r: {
                "companies_scanned": r.get("companies_scanned", 0),
                "jobs_checked": r.get("jobs_checked", 0),
                "promoted": r.get("promoted", 0),
                "resolved_strict": r.get("strict", 0),
                "resolved_loose": r.get("loose", 0),
                "merged": r.get("merged", 0),
                "llm_upgraded": r.get("llm_upgraded", 0),
            },
            guard=lambda config: ((config.get("direct_link") or {}).get("resolver") or {}).get(
                "enabled", True
            ),
            publish_events=(JOBS_CHANGED,),
        ),
        trigger=CronTrigger(hour=5, minute=45),
        id="primary_source_resolution",
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
        _make_simple_job(
            app, "Orphan cleanup", _import_orphan_cleanup, publish_events=(COMPANIES_CHANGED,)
        ),
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
        _make_simple_job(
            app,
            "Homepage discovery",
            _import_homepage_discovery,
            publish_events=(JOBS_CHANGED, COMPANIES_CHANGED),
        ),
        trigger=CronTrigger(hour=6, minute=30),
        id="homepage_discovery",
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
        _make_simple_job(
            app, "Registry hygiene", _import_registry_hygiene, publish_events=(COMPANIES_CHANGED,)
        ),
        trigger=CronTrigger(day=1, hour=3, minute=30),
        id="registry_hygiene",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


# ---------------------------------------------------------------------------
# Enrichment backfill (1 hour after each ingestion run: 1am, 9am, 5pm).
# Two stages: (1) fill jd_full via the cost-ordered tier pipeline (uncapped),
# (2) score every row that has jd_full but no classification (uncapped).
# Without stage 2 the v3.0 multi-stage pipeline leaks — ingestion-time
# scoring sees empty jd_full and skips, and nothing else picks the row up.
# Tracked via _make_tracked_job so user_activity captures every run.
# ---------------------------------------------------------------------------


def register_enrichment_backfill(scheduler, app) -> None:
    """Register the post-ingestion enrichment backfill.

    Hours are DERIVED from the same cadence_preset as ingestion (each ingestion
    slot + 1h) via _schedule.enrichment_hour_expr — so light/standard/heavy
    resize ingestion and its backfill together. Previously this was a hard-coded
    ``1,9,17`` that silently desynced from a non-standard ingestion cadence
    (bug (c) in #229).
    """

    def _import_enrichment_backfill():
        from job_finder.web.scheduler._runners import run_enrichment_backfill_two_stage

        return run_enrichment_backfill_two_stage

    def _import_enrichment_backfill_action():
        from job_finder.web.activity_tracker import ACTION_SCHEDULED_ENRICHMENT_BACKFILL

        return ACTION_SCHEDULED_ENRICHMENT_BACKFILL

    config = get_config_snapshot(app)
    preset = config.get("scheduler", {}).get("cadence_preset", "standard")
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
            publish_events=(JOBS_CHANGED, COSTS_CHANGED),
        ),
        trigger=CronTrigger(hour=enrichment_hour_expr(preset)),
        id="enrichment_backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
    )


# ---------------------------------------------------------------------------
# JD-content LLM adjudication — daily backfill of the AMBIGUOUS middle (PR2 of
# the jd-content contract). The deterministic re-sweep handles the high-precision
# REJECTs; this drains the ~21% AMBIGUOUS bodies through the local-LLM tie-breaker
# in bounded batches. Noon slot avoids the heavy nightly writers (staleness +
# agentic) that the comment below warns about DB-contending.
# ---------------------------------------------------------------------------


def register_jd_adjudication(scheduler, app) -> None:
    """Register the daily jd-content LLM adjudication backfill."""

    def _import_jd_adjudication():
        from job_finder.web.scheduler._runners import run_jd_adjudication

        return run_jd_adjudication

    def _import_jd_adjudication_action():
        from job_finder.web.activity_tracker import ACTION_SCHEDULED_JD_ADJUDICATION

        return ACTION_SCHEDULED_JD_ADJUDICATION

    scheduler.add_job(
        _make_tracked_job(
            app,
            "JD adjudication",
            import_func=_import_jd_adjudication,
            import_action=_import_jd_adjudication_action,
            extract_metadata=lambda r: {
                "scanned": r.get("scanned", 0),
                "llm_calls": r.get("llm_calls", 0),
                "kept": r.get("kept", 0),
                "rejected": r.get("rejected", 0),
            },
            publish_events=(JOBS_CHANGED, COSTS_CHANGED),
        ),
        trigger=CronTrigger(hour=12, minute=0),
        id="jd_adjudication",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEAVY_MISFIRE_GRACE_S,
    )


# ---------------------------------------------------------------------------
# Agentic backfill — chained off staleness_check completion (no cron). Was a
# fixed 4:15 slot chosen to clear the tail of staleness (starts 2:00, runs
# ~2 hours). But staleness runtime is unbounded, so a fixed slot still raced it
# on bad nights (the May-1 3:30 collision DB-locked persist_job_expiry_state 113
# times and killed the agentic run after 1/50 jobs). Chaining off staleness's
# completion makes the ordering exact regardless of how long staleness runs —
# the two heaviest nightly writers can no longer overlap. OllamaProvider is
# instantiated INSIDE run_agentic_backfill, NOT here — the closure only defers
# the import.
# ---------------------------------------------------------------------------


def build_agentic_backfill_wrapper(app):
    """Build (but do not schedule) the agentic-backfill job wrapper.

    Returned wrapper is scheduled as a one-shot by staleness_check's chainer.
    """

    def _import_agentic_backfill():
        from job_finder.web.agentic_enricher import run_agentic_backfill

        # Lambda wrapper matches the _import_stale pattern exactly:
        # returns a callable(db_path, config) rather than the raw function.
        return lambda db_path, config: run_agentic_backfill(db_path, config)

    def _import_agentic_action():
        from job_finder.web.activity_tracker import ACTION_SCHEDULED_AGENTIC_BACKFILL

        return ACTION_SCHEDULED_AGENTIC_BACKFILL

    return _make_tracked_job(
        app,
        "Agentic backfill",
        import_func=_import_agentic_backfill,
        import_action=_import_agentic_action,
        # "jobs_enriched" matches naming convention used by other tracked jobs
        # (jobs_found, jobs_new, jobs_scanned) for consistent dashboard display.
        extract_metadata=lambda r: {"jobs_enriched": r if isinstance(r, int) else 0},
        publish_events=(JOBS_CHANGED, COSTS_CHANGED),
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

    from job_finder.web.scheduler._runners import run_health_check

    scheduler.add_job(
        lambda: run_health_check(app),
        trigger=CronTrigger(hour=6, minute=0),
        id="health_heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


# ---------------------------------------------------------------------------
# Liveness heartbeat (every 60s -- serve-path freshness signal)
# ---------------------------------------------------------------------------


def register_heartbeat(scheduler, app) -> None:
    """Register the short-cadence liveness heartbeat job.

    Touches ``last_alive`` every ``HEARTBEAT_INTERVAL_S`` seconds so an
    out-of-process healthcheck can judge liveness by file freshness. Writes one
    heartbeat immediately (before the first interval tick) so ``last_alive``
    exists at boot rather than only after 60s — this closes the cold-start
    window where a healthcheck would otherwise see a missing/stale file.

    Unlike the daily ``health_heartbeat`` this writes no DB row: it must not
    compete for the WAL write lock every minute, so it is not a heavy writer and
    does not participate in ``assert_no_heavy_writer_collisions``. ``app`` is
    accepted for registrar-signature symmetry; the write resolves its path from
    the user-data root and needs no app context.
    """
    from job_finder.web.scheduler._heartbeat import HEARTBEAT_INTERVAL_S, write_heartbeat

    # Boot write: make last_alive exist immediately (cold-start window).
    write_heartbeat()

    scheduler.add_job(
        write_heartbeat,
        trigger=IntervalTrigger(seconds=HEARTBEAT_INTERVAL_S),
        id="heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=LIGHT_MISFIRE_GRACE_S,
    )


def register_all_jobs(scheduler, app) -> None:
    """Register all scheduled jobs on the given scheduler instance.

    Order matches the legacy inline shape so any future re-introduction of
    cross-job ordering invariants (e.g., agentic_backfill must register
    AFTER staleness so its metadata column appears second on the dashboard)
    has a stable insertion sequence.

    Two dependent pairs are completion-chained rather than cron-scheduled
    (#229), so the heaviest DB writers never share a slot:
      - staleness_check → agentic_backfill
      - careers_crawl   → company_linkage

    A boot-time assertion (assert_no_heavy_writer_collisions) fails fast if a
    future descriptor edit reintroduces a shared heavy-writer slot.
    """
    # Fail fast on a descriptor that would put two heavy writers on one slot.
    assert_no_heavy_writer_collisions()

    # Build the chained successors first (they have no cron trigger of their own;
    # the predecessor's on_complete hook schedules them as one-shots).
    agentic_wrapper = build_agentic_backfill_wrapper(app)
    company_linkage_wrapper = build_company_linkage_wrapper(app)

    register_ingestion(scheduler, app)
    register_staleness(
        scheduler,
        app,
        on_complete=_make_chainer(scheduler, "agentic_backfill", agentic_wrapper),
    )
    register_pipeline_detection(scheduler, app)
    register_ats_scan(scheduler, app)
    register_ats_slug_probe(scheduler, app)
    register_ats_promote(scheduler, app)
    register_careers_crawl(
        scheduler,
        app,
        on_complete=_make_chainer(scheduler, "company_linkage", company_linkage_wrapper),
    )
    register_primary_source_resolution(scheduler, app)
    register_orphan_cleanup(scheduler, app)
    register_homepage_discovery(scheduler, app)
    register_registry_hygiene(scheduler, app)
    register_enrichment_backfill(scheduler, app)
    register_jd_adjudication(scheduler, app)
    register_health_heartbeat(scheduler, app)
    register_heartbeat(scheduler, app)
