#!/usr/bin/env python3
"""Run all missed overnight scheduled jobs in sequence.

Creates a TESTING-mode Flask app (no scheduler start) alongside the running
app so job functions get proper app context without launching a second
scheduler. Pauses pipeline_detection on the live scheduler first to avoid
interference.
"""

import importlib
import time
import traceback
from datetime import datetime

import requests
import yaml

ADMIN = "http://localhost:5000/admin/jobs"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def _pause(job_id: str) -> None:
    r = requests.post(f"{ADMIN}/{job_id}/pause", timeout=5)
    log(f"  paused {job_id}: {r.json()}")


def _resume(job_id: str) -> None:
    r = requests.post(f"{ADMIN}/{job_id}/resume", timeout=5)
    log(f"  resumed {job_id}: {r.json()}")


def _run(name: str, mod_path: str, func_name: str, db_path: str, cfg: dict, app) -> object:
    log(f"\n{'=' * 64}")
    log(f"START  {name}")
    t0 = time.time()
    try:
        mod = importlib.import_module(mod_path)
        func = getattr(mod, func_name)
        with app.app_context():
            result = func(db_path, cfg)
        elapsed = round(time.time() - t0, 1)
        log(f"DONE   {name}  ({elapsed}s)")
        log(f"       {result}")
        return result
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        log(f"FAIL   {name}  ({elapsed}s)  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None


def main() -> None:
    log("=== overnight catch-up run starting ===")

    log("Pausing pipeline_detection on live scheduler...")
    _pause("pipeline_detection")

    with open("config.yaml") as _cfg_fh:
        cfg = yaml.safe_load(_cfg_fh)
    # SKIP_SCHEDULER (not TESTING) is the right flag here. TESTING also makes
    # 4 job functions no-op silently (careers_crawl, ats_scan, ats_slug_probe,
    # ats_identity_reconcile) — see job_finder/web/scheduler/__init__.py:46
    # for both flags' shared "scheduler off" semantics; only TESTING propagates
    # into the job-function guards. This script is supposed to run the jobs
    # for real, so SKIP_SCHEDULER is correct.
    cfg["SKIP_SCHEDULER"] = True

    from job_finder.web import create_app

    app = create_app(config=cfg)
    db_path = app.config["DB_PATH"]
    log(f"App context ready, db_path={db_path}")

    # Chronological order matching scheduled times (midnight → 7:30 AM)
    sequence = [
        ("Ingestion (midnight + 8 AM)", "job_finder.web.pipeline_runner", "run_ingestion"),
        (
            "Enrichment backfill (1 AM)",
            "job_finder.web.scheduler._runners",
            "run_enrichment_backfill_two_stage",
        ),
        ("Staleness check (2 AM)", "job_finder.web.expiry_checker", "run_staleness_check"),
        ("Agentic backfill (4:15 AM)", "job_finder.web.agentic_enricher", "run_agentic_backfill"),
        (
            "ATS source-URL promote (4:45 AM)",
            "job_finder.web.ats_scanner",
            "promote_ats_from_source_urls",
        ),
        ("Careers crawl (5:00 AM)", "job_finder.web.careers_crawler", "crawl_careers_batch"),
        ("Company linkage (5:00 AM)", "job_finder.web.backfill_companies", "run_company_linkage"),
        (
            "Homepage discovery (6:30 AM)",
            "job_finder.web.homepage_discoverer",
            "run_homepage_discovery",
        ),
        ("ATS slug probe (7:30 AM)", "job_finder.web.ats_scanner", "probe_ats_slugs"),
        ("ATS scan (7:00 AM)", "job_finder.web.ats_scanner", "run_ats_scan"),
    ]

    results: dict[str, object] = {}
    t_total = time.time()
    for name, mod_path, func_name in sequence:
        results[name] = _run(name, mod_path, func_name, db_path, cfg, app)

    total = round(time.time() - t_total, 1)

    log(f"\n{'=' * 64}")
    log(f"ALL DONE  (total {total}s)")
    log("Summary:")
    for name, result in results.items():
        status = "OK  " if result is not None else "FAIL"
        log(f"  {status}  {name}")

    log("\nResuming pipeline_detection...")
    _resume("pipeline_detection")
    log("=== overnight catch-up run complete ===")


if __name__ == "__main__":
    main()
