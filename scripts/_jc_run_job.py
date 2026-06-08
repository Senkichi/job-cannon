#!/usr/bin/env python3
"""Run ONE overnight scheduled job in isolation, with full log streaming.

Creates a SKIP_SCHEDULER Flask app (no scheduler, no startup backfills) so the
job function runs with proper app context but nothing competes for the write
lock or fires on cron. The real DB path is forced to an ABSOLUTE path so this
is safe to run from any cwd (incl. a git worktree where the relative
``db.path: jobs.db`` would otherwise spawn an empty DB).

Usage:
    uv run python scripts/_jc_run_job.py <job_key>

job_key one of:
    ingestion enrichment staleness agentic ats_promote careers
    company_linkage homepage slug_probe ats_scan pipeline_detection health
"""

import logging
import os
import sys
import time
import traceback

# Clean scheduler suppression: init_scheduler Guard 2 skips the scheduler when
# WERKZEUG_RUN_MAIN == "true". This avoids the cfg["SKIP_SCHEDULER"] path, which
# is a no-op because create_app never propagates SKIP_SCHEDULER into app.config
# (it only does so for TESTING). Using TESTING instead would no-op 4 job funcs
# (careers_crawl, ats_scan, ats_slug_probe, ats_identity_reconcile). The env var
# is the side-effect-free lever. cfg["SKIP_SCHEDULER"] is still set below so the
# startup-backfill / file-logging gate in create_app is also skipped.
os.environ.setdefault("WERKZEUG_RUN_MAIN", "true")

# Windows console is cp1252; many INFO logs contain U+2192 etc. Reconfigure so the
# StreamHandler can't raise UnicodeEncodeError mid-run (seen on title-gate logs).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

def _default_db() -> str:
    """Resolve the jobs.db path portably: $JC_DB_PATH, else
    $JOB_CANNON_USER_DATA_DIR/jobs.db, else <repo-root>/jobs.db."""
    env = os.environ.get("JC_DB_PATH")
    if env:
        return env
    base = os.environ.get("JOB_CANNON_USER_DATA_DIR") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "jobs.db")


DB_ABS = _default_db()

# (module_path, func_name, call_style)  call_style: "dbcfg" -> f(db_path, cfg); "app" -> f(app)
JOBS = {
    "ingestion": ("job_finder.web.pipeline_runner", "run_ingestion", "dbcfg"),
    "enrichment": (
        "job_finder.web.scheduler._runners",
        "run_enrichment_backfill_two_stage",
        "dbcfg",
    ),
    "staleness": ("job_finder.web.expiry_checker", "run_staleness_check", "dbcfg"),
    "agentic": ("job_finder.web.agentic_enricher", "run_agentic_backfill", "dbcfg"),
    "ats_promote": ("job_finder.web.ats_scanner", "promote_ats_from_source_urls", "dbcfg"),
    "careers": ("job_finder.web.careers_crawler", "crawl_careers_batch", "dbcfg"),
    "company_linkage": ("job_finder.web.backfill_companies", "run_company_linkage", "dbcfg"),
    "homepage": ("job_finder.web.homepage_discoverer", "run_homepage_discovery", "dbcfg"),
    "slug_probe": ("job_finder.web.ats_scanner", "probe_ats_slugs", "dbcfg"),
    "ats_scan": ("job_finder.web.ats_scanner", "run_ats_scan", "dbcfg"),
    "pipeline_detection": ("job_finder.web.pipeline_detector", "run_pipeline_detection", "dbcfg"),
    "health": ("job_finder.web.scheduler._runners", "run_health_check", "app"),
}


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(h)
    # Quiet the known-noisy transitive loggers (mirrors create_app's suppression).
    logging.getLogger("primp").setLevel(logging.WARNING)
    logging.getLogger("ddgs").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print(f"usage: _jc_run_job.py <{'|'.join(JOBS)}>", file=sys.stderr)
        return 2

    key = sys.argv[1]
    mod_path, func_name, style = JOBS[key]

    _setup_logging()
    log = logging.getLogger("jc.runner")

    from job_finder.config import load_config

    cfg = load_config()
    cfg.setdefault("db", {})["path"] = DB_ABS  # force absolute; cwd-independent
    cfg["SKIP_SCHEDULER"] = True

    from job_finder.web import create_app

    app = create_app(config=cfg)
    db_path = app.config["DB_PATH"]
    log.info("RUNNER start job=%s db_path=%s", key, db_path)

    # Structured run-event instrumentation (optional, never fatal). run_id uses
    # the deterministic "{job}:{pid}" form so a detached _jc_supervise.py can
    # reconstruct it from (job, pid) and record reaped/stalled if we are killed.
    try:
        from job_finder.web import run_events
    except Exception:  # noqa: BLE001
        run_events = None
    run_id = f"{key}:{os.getpid()}"
    counters0 = run_events.db_counters(db_path) if run_events else None
    if run_events:
        run_events.start(
            run_id=run_id,
            job=key,
            source="harness",
            pid=os.getpid(),
            db_path=db_path,
            db_before=counters0,
            cmd=" ".join(sys.argv),
        )

    import importlib

    mod = importlib.import_module(mod_path)
    func = getattr(mod, func_name)

    t0 = time.time()
    try:
        if style == "app":
            with app.app_context():
                result = func(app)
        else:
            with app.app_context():
                result = func(db_path, cfg)
        elapsed = round(time.time() - t0, 1)
        log.info("RUNNER done job=%s elapsed=%ss", key, elapsed)
        print(f"\n>>> RESULT[{key}] ({elapsed}s): {result!r}", flush=True)
        if run_events:
            run_events.end(
                run_id,
                job=key,
                source="harness",
                disposition="completed",
                pid=os.getpid(),
                db_path=db_path,
                db_before=counters0,
                duration_s=elapsed,
                exit_code=0,
                result=result,
            )
        return 0
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.time() - t0, 1)
        log.error("RUNNER FAIL job=%s elapsed=%ss %s: %s", key, elapsed, type(exc).__name__, exc)
        traceback.print_exc()
        print(f"\n>>> RESULT[{key}] FAILED ({elapsed}s): {type(exc).__name__}: {exc}", flush=True)
        if run_events:
            run_events.end(
                run_id,
                job=key,
                source="harness",
                disposition="failed",
                pid=os.getpid(),
                db_path=db_path,
                db_before=counters0,
                duration_s=elapsed,
                exit_code=1,
                error=type(exc).__name__,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
