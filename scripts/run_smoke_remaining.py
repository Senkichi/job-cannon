"""Run the 3 smoke jobs left over after the ats_scan portion completed:
ats_slug_probe, ats_promote, careers_crawl. Same TESTING-strip pattern
as run_overnight_smoke_skipped.py."""

from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("smoke_remaining")


def _banner(title: str) -> None:
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write(f"  {title}\n")
    sys.stderr.write("=" * 70 + "\n")
    sys.stderr.flush()


def _run_one(
    name: str,
    func: Callable[[], Any],
    *,
    summarize: Callable[[Any], dict[str, Any]] = lambda r: {"result": str(r)[:200]},
) -> dict[str, Any]:
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
        sys.stderr.write(record["traceback"] + "\n")
        sys.stderr.flush()
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()
    return record


def main() -> int:
    from job_finder.config import load_config
    from job_finder.web import create_app

    cfg = load_config()
    cfg["TESTING"] = True
    app = create_app(config=cfg)

    db_path = app.config.get("DB_PATH", "jobs.db")
    logger.info("DB: %s", db_path)

    with app.app_context():
        from job_finder.web.db_helpers import get_config_snapshot

        snap = deepcopy(get_config_snapshot(app))
        snap.pop("TESTING", None)

        results: list[dict[str, Any]] = []

        from job_finder.web.ats_scanner import probe_ats_slugs

        results.append(_run_one("ats_slug_probe", lambda: probe_ats_slugs(db_path, snap)))

        from job_finder.web.ats_scanner import promote_ats_from_source_urls

        results.append(
            _run_one("ats_promote", lambda: promote_ats_from_source_urls(db_path, snap))
        )

        from job_finder.web.careers_crawler import crawl_careers_batch

        results.append(
            _run_one(
                "careers_crawl",
                lambda: crawl_careers_batch(db_path, snap),
                summarize=lambda r: {
                    "companies_crawled": r.get("companies_crawled", 0),
                    "jobs_found": r.get("jobs_found", 0),
                    "jobs_new": r.get("jobs_new", 0),
                    "playwright_rendered": r.get("playwright_rendered", 0),
                    "errors": r.get("errors", []),
                },
            )
        )

    _banner("SUMMARY (remaining smoke)")
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
        sys.stderr.write(f"  [{marker}] {r['job']:<20} {elapsed}s{extra}\n")
    sys.stderr.flush()
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
