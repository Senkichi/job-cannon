"""One-shot: run every scheduled job in sequence as a regression smoke test.

Mirrors job_finder.web.scheduler._jobs.register_all_jobs' invocation
shape (same callables, same (db_path, config) signature, same Flask app
context) but executes them inline -- no APScheduler, no cron, no
race with a live scheduler. Sets TESTING=True so create_app() skips
its scheduler init.

Each job is wrapped with timing + exception capture. Output is
machine-readable JSON-per-line on stdout PLUS a human banner per job
on stderr. Exit code is 0 iff every job succeeded.

Usage:
    uv run python scripts/run_overnight_smoke.py [--agentic-limit 5]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# scripts/ is not on sys.path when invoked as `python scripts/foo.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("smoke")


def _banner(title: str) -> None:
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write(f"  {title}\n")
    sys.stderr.write("=" * 70 + "\n")
    sys.stderr.flush()


def _emit(record: dict[str, Any]) -> None:
    """Emit one machine-readable result line to stdout."""
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def _run_one(
    name: str,
    func: Callable[[], Any],
    *,
    summarize: Callable[[Any], dict[str, Any]] = lambda r: {"result": str(r)[:200]},
) -> dict[str, Any]:
    """Run a single job and return a structured result record."""
    _banner(f"START  {name}")
    t0 = time.time()
    record: dict[str, Any] = {"job": name, "status": "unknown"}
    try:
        result = func()
        elapsed = round(time.time() - t0, 2)
        record["status"] = "ok"
        record["elapsed_sec"] = elapsed
        record["summary"] = summarize(result)
        logger.info("OK  %s in %ss -> %s", name, elapsed, record["summary"])
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        record["status"] = "fail"
        record["elapsed_sec"] = elapsed
        record["error_type"] = type(e).__name__
        record["error_msg"] = str(e)
        record["traceback"] = traceback.format_exc()
        logger.error("FAIL  %s in %ss -> %s: %s", name, elapsed, type(e).__name__, e)
        # Tracebacks go to stderr too so the live monitor sees them.
        sys.stderr.write(record["traceback"] + "\n")
        sys.stderr.flush()
    _emit(record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentic-limit", type=int, default=5)
    args = parser.parse_args()

    from job_finder.config import load_config
    from job_finder.web import create_app

    cfg = load_config()
    # Skip the scheduler init in create_app() so we don't race the
    # portalocker pidfile or fire any cron jobs at app startup.
    cfg["TESTING"] = True
    app = create_app(config=cfg)

    db_path = app.config.get("DB_PATH", "jobs.db")
    logger.info("DB: %s", db_path)
    logger.info("agentic_limit: %d", args.agentic_limit)

    results: list[dict[str, Any]] = []

    with app.app_context():
        from job_finder.web.db_helpers import get_config_snapshot

        config = get_config_snapshot(app)

        # 1. Ingestion -------------------------------------------------------
        from job_finder.web.pipeline_runner import run_ingestion

        results.append(
            _run_one(
                "ingestion",
                lambda: run_ingestion(db_path, config),
                summarize=lambda r: {
                    "jobs_new": r.get("jobs_new", 0),
                    "gmail_fetched": r.get("gmail_fetched", 0),
                    "serpapi_fetched": r.get("serpapi_fetched", 0),
                    "thordata_fetched": r.get("thordata_fetched", 0),
                    "dataforseo_fetched": r.get("dataforseo_fetched", 0),
                },
            )
        )

        # 2. Staleness check -------------------------------------------------
        from job_finder.web.expiry_checker import run_staleness_check

        results.append(
            _run_one(
                "staleness_check",
                lambda: run_staleness_check(db_path, config),
                summarize=lambda r: {
                    "phase_a_stale_marked": r.get("phase_a", {}).get("stale_marked", 0),
                    "phase_b_companies_checked": r.get("phase_b", {}).get("companies_checked", 0),
                    "phase_c_checked": r.get("phase_c", {}).get("checked", 0),
                },
            )
        )

        # 3. Pipeline detection ---------------------------------------------
        from job_finder.web.pipeline_detector import run_pipeline_detection

        results.append(
            _run_one(
                "pipeline_detection",
                lambda: run_pipeline_detection(db_path, config),
                summarize=lambda r: {
                    "emails_scanned": r.get("emails_scanned", 0),
                    "auto_updated": r.get("auto_updated", 0),
                    "errors": r.get("errors", []),
                },
            )
        )

        # 4. ATS scan --------------------------------------------------------
        from job_finder.web.ats_scanner import run_ats_scan

        results.append(
            _run_one(
                "ats_scan",
                lambda: run_ats_scan(db_path, config),
                summarize=lambda r: {
                    "companies_scanned": r.get("companies_scanned", 0),
                    "jobs_discovered": r.get("jobs_discovered", 0),
                    "jobs_new": r.get("jobs_new", 0),
                },
            )
        )

        # 5. ATS slug probe --------------------------------------------------
        from job_finder.web.ats_scanner import probe_ats_slugs

        results.append(
            _run_one(
                "ats_slug_probe",
                lambda: probe_ats_slugs(db_path, config),
            )
        )

        # 6. ATS promote -----------------------------------------------------
        from job_finder.web.ats_scanner import promote_ats_from_source_urls

        results.append(
            _run_one(
                "ats_promote",
                lambda: promote_ats_from_source_urls(db_path, config),
            )
        )

        # 7. Careers crawl ---------------------------------------------------
        from job_finder.web.careers_crawler import crawl_careers_batch

        results.append(
            _run_one(
                "careers_crawl",
                lambda: crawl_careers_batch(db_path, config),
                summarize=lambda r: {
                    "companies_crawled": r.get("companies_crawled", 0),
                    "jobs_found": r.get("jobs_found", 0),
                    "jobs_new": r.get("jobs_new", 0),
                    "playwright_rendered": r.get("playwright_rendered", 0),
                },
            )
        )

        # 8. Company linkage -------------------------------------------------
        from job_finder.web.backfill_companies import run_company_linkage

        results.append(
            _run_one(
                "company_linkage",
                lambda: run_company_linkage(db_path, config),
            )
        )

        # 9. Orphan cleanup (destructive; runs only on 1st) -----------------
        from job_finder.web.backfill_companies import run_orphan_cleanup

        results.append(
            _run_one(
                "orphan_cleanup",
                lambda: run_orphan_cleanup(db_path, config),
            )
        )

        # 10. Homepage discovery --------------------------------------------
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        results.append(
            _run_one(
                "homepage_discovery",
                lambda: run_homepage_discovery(db_path, config),
            )
        )

        # 11. Company enrichment (Sun) --------------------------------------
        from job_finder.web.backfill_companies import run_scheduled_enrichment

        results.append(
            _run_one(
                "company_enrichment",
                lambda: run_scheduled_enrichment(db_path, config),
            )
        )

        # 12. Registry hygiene (1st of month; destructive) ------------------
        from job_finder.web.backfill_companies import run_registry_hygiene

        results.append(
            _run_one(
                "registry_hygiene",
                lambda: run_registry_hygiene(db_path, config),
            )
        )

        # 13. Enrichment backfill (two-stage) -------------------------------
        from job_finder.web.scheduler._runners import run_enrichment_backfill_two_stage

        results.append(
            _run_one(
                "enrichment_backfill",
                lambda: run_enrichment_backfill_two_stage(db_path, config),
                summarize=lambda r: {
                    "enriched": r.get("enriched", 0),
                    "scored": r.get("scored", 0),
                    "errors": r.get("errors", []),
                },
            )
        )

        # 14. Agentic backfill (limit-capped) -------------------------------
        from job_finder.web.agentic_enricher import run_agentic_backfill

        results.append(
            _run_one(
                "agentic_backfill",
                lambda: run_agentic_backfill(db_path, config, limit=args.agentic_limit),
                summarize=lambda r: {"jobs_enriched": r if isinstance(r, int) else 0},
            )
        )

        # 15. Health heartbeat (takes app, returns None) --------------------
        from job_finder.web.scheduler._runners import run_health_check

        results.append(
            _run_one(
                "health_heartbeat",
                lambda: run_health_check(app),
            )
        )

    # ----- Summary -----------------------------------------------------------
    _banner("SUMMARY")
    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    sys.stderr.write(f"  OK:   {n_ok}/{len(results)}\n")
    sys.stderr.write(f"  FAIL: {n_fail}/{len(results)}\n")
    for r in results:
        marker = "OK  " if r["status"] == "ok" else "FAIL"
        elapsed = r.get("elapsed_sec", "?")
        extra = ""
        if r["status"] == "fail":
            extra = f"  ({r.get('error_type')}: {r.get('error_msg')})"
        sys.stderr.write(f"  [{marker}] {r['job']:<22} {elapsed}s{extra}\n")
    sys.stderr.flush()

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
