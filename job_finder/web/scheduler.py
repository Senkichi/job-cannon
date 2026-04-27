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

import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from job_finder.web.db_helpers import get_config_snapshot

logger = logging.getLogger(__name__)

# Module-level singleton -- prevents double initialization
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()
_pidfile_path: Path | None = None


def _acquire_scheduler_pidfile(app) -> bool:
    """Acquire a cross-process pidfile lock before starting the scheduler.

    Prevents two independent Python processes (e.g. the user launching `run.py`
    twice after a crashed session, or a stale background instance) from both
    running the 0,8,16 cron schedule — which previously caused the 16:00 PT
    ingestion to fire twice and double-bill Gmail/SerpAPI/DataForSEO.

    Self-heals stale pidfiles: if the recorded PID is no longer alive, the
    lock is taken cleanly. Cross-process liveness check uses psutil.

    Returns:
        True if the lock was acquired (safe to start scheduler), False if
        another live instance is already running (caller must skip).
    """
    global _pidfile_path

    db_path = app.config.get("DB_PATH", "jobs.db")
    pidfile = Path(db_path).resolve().parent / "logs" / "scheduler.pid"

    if pidfile.exists():
        try:
            existing_pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None  # corrupt pidfile — treat as stale

        if existing_pid and existing_pid != os.getpid():
            try:
                import psutil
                alive = psutil.pid_exists(existing_pid)
            except Exception:
                alive = False

            if alive:
                logger.warning(
                    "Scheduler: another instance (PID %d) is already running — "
                    "this process will NOT start a scheduler to prevent duplicate cron firings",
                    existing_pid,
                )
                return False

    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    _pidfile_path = pidfile

    def _cleanup_pidfile() -> None:
        try:
            if _pidfile_path and _pidfile_path.exists():
                # Only remove if WE still own it (avoid racing with another process
                # that may have taken over after a crash).
                try:
                    owner_pid = int(_pidfile_path.read_text().strip())
                except Exception:
                    owner_pid = None
                if owner_pid == os.getpid():
                    _pidfile_path.unlink()
        except Exception:
            pass  # best-effort cleanup; next start self-heals via liveness check

    atexit.register(_cleanup_pidfile)
    logger.info("Scheduler: acquired pidfile lock at %s (PID %d)", pidfile, os.getpid())
    return True


def _ensure_ollama_running(config: dict, *, poll_seconds: int = 30) -> None:
    """Ensure Ollama is reachable; auto-start 'ollama serve' if not.

    Agentic backfill runs nightly at 3:30 AM and requires Ollama. Previously
    Ollama had to be started manually — if the user forgot or it crashed, the
    entire backfill job aborted with a WARNING. This helper probes the service
    at scheduler init and spawns a detached `ollama serve` process when the
    probe fails. Best-effort; never raises.

    Binary location resolves in order:
      1. $OLLAMA_EXE environment variable (user override)
      2. %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe (default Windows install)
      3. 'ollama' on PATH

    Args:
        config: Full JF_CONFIG dict; passed to OllamaProvider for base_url.
        poll_seconds: Max seconds to wait for Ollama to come up after spawning.
    """
    try:
        from job_finder.web.providers.ollama_provider import OllamaProvider
    except ImportError as exc:
        logger.debug("Ollama auto-start skipped (provider import failed): %s", exc)
        return

    try:
        OllamaProvider(config=config)  # health check inside __init__
        logger.debug("Ollama: already running, skipping auto-start")
        return
    except RuntimeError:
        pass  # not running — try to start it

    # Locate the binary
    ollama_exe = os.environ.get("OLLAMA_EXE", "").strip() or None
    if not ollama_exe:
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            default_path = Path(localappdata) / "Programs" / "Ollama" / "ollama.exe"
            if default_path.exists():
                ollama_exe = str(default_path)
    if not ollama_exe:
        # Fall back to PATH lookup (Linux/macOS or Windows with PATH entry)
        import shutil
        ollama_exe = shutil.which("ollama")

    if not ollama_exe:
        logger.warning(
            "Ollama auto-start skipped: binary not found. Set OLLAMA_EXE env var or "
            "install Ollama (https://ollama.com/download). Agentic backfill will be disabled."
        )
        return

    try:
        if sys.platform == "win32":
            # Detach fully so Ollama outlives the Flask process
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [ollama_exe, "serve"],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [ollama_exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
    except Exception as exc:
        logger.warning("Ollama auto-start failed to spawn '%s serve': %s", ollama_exe, exc)
        return

    # Poll for readiness
    for attempt in range(poll_seconds):
        try:
            OllamaProvider(config=config)
            logger.info("Ollama auto-started successfully after %ds", attempt + 1)
            return
        except RuntimeError:
            time.sleep(1)

    logger.warning(
        "Ollama did not become ready within %ds of auto-start. Agentic backfill may fail tonight.",
        poll_seconds,
    )

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
                from job_finder.web.activity_tracker import log_activity, ACTION_SCHEDULED_SYNC
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
            max_instances=1,   # prevents overlap on long runs
            coalesce=True,     # skip missed runs if app was down
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
                app, "Staleness check",
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
                    "enabled", config.get("expiry", {}).get("enabled", True),
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
            """Backfill enrichment + score the freshly-enriched rows.

            Stage 1 fills jd_full / salary_min via the cost-ordered tier
            pipeline. Stage 2 scores every row that now has jd_full but no
            classification (capped at 500/run to keep the cron cycle short).
            Without Stage 2 the v3.0 multi-stage pipeline leaks: ingestion-
            time scoring sees empty jd_full and skips, and nothing else
            picks the row up after enrichment lands.
            """
            with app.app_context():
                config = get_config_snapshot(app)
                db_path = app.config.get("DB_PATH", "jobs.db")
                try:
                    from job_finder.web.data_enricher import run_enrichment_backfill
                    serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
                    enriched = run_enrichment_backfill(
                        db_path,
                        serpapi_key=serpapi_key,
                        config=config,
                        limit=200,
                    )
                    logger.info("Enrichment backfill: %s", enriched)
                except Exception as e:
                    logger.error("Enrichment backfill failed: %s", e)
                    return

                try:
                    from job_finder.web.scoring_runner import run_scoring
                    from job_finder.web.db_helpers import standalone_connection
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
                        return
                    summary = run_scoring(dedup_keys, config, db_path)
                    logger.info("Post-enrichment scoring: %s", summary)
                except Exception as e:
                    logger.error("Post-enrichment scoring failed: %s", e)

        scheduler.add_job(
            _run_enrichment_backfill,
            trigger=CronTrigger(hour="1,9,17"),
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
        logger.info("Scheduler started: ingestion 3x/day (0:00, 8:00, 16:00 local); enrichment 1h after each (1:00, 9:00, 17:00 local)")

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
