"""Trigger a paused scheduler job and report on its execution.

Usage:
    uv run python scripts/trigger_and_watch.py <job_id> [max_wait_seconds]

Polls logs/app.log until the job emits a success line ("<Name>: {...}") or a
failure line ("<Name> failed: ..."), then re-pauses the job. Default max wait
is 60s. Prints the success/error line plus any WARNING/ERROR/Traceback lines.
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "app.log"

# job_id -> the human-readable name used by the scheduler runner in logs.
# The scheduler wrapper logs "<name>: <result_dict>" via _make_simple_job /
# _make_tracked_job. Matches the names registered in scheduler/_jobs.py.
JOB_NAME = {
    "ingestion_poll": "Pipeline ingestion",
    "pipeline_detection": "Pipeline detection",
    "enrichment_backfill": "Enrichment backfill",
    "staleness_check": "Stale detection",
    "agentic_backfill": "Agentic enrichment",
    "ats_source_url_promote": "ATS source url promote",
    "careers_crawl": "Careers crawl",
    "company_linkage": "Company linkage",
    "health_heartbeat": "Health heartbeat",
    "homepage_discovery": "Homepage discovery",
    "ats_scan": "ATS scan",
    "ats_slug_probe": "ATS slug probe",
    "orphan_cleanup": "Orphan cleanup",
    "registry_hygiene": "Registry hygiene",
}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: trigger_and_watch.py <job_id> [max_wait_seconds]")
        return 2

    job_id = sys.argv[1]
    max_wait = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    name = JOB_NAME.get(job_id, job_id)

    start_size = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    trigger_url = f"http://127.0.0.1:5000/admin/jobs/{job_id}/run-now"
    req = urllib.request.Request(trigger_url, method="POST")
    print(f"[trigger] {urllib.request.urlopen(req).read().decode().strip()}")

    # Poll log for completion.
    deadline = time.time() + max_wait
    success_token = f"{name}:"
    fail_token = f"{name} failed:"
    completed = False
    last_size = start_size
    while time.time() < deadline:
        time.sleep(2)
        if not LOG_PATH.exists():
            continue
        with open(LOG_PATH, "rb") as f:
            f.seek(last_size)
            chunk = f.read().decode("utf-8", errors="replace")
            last_size = f.tell()
        if success_token in chunk or fail_token in chunk:
            completed = True
            break

    # Always re-pause so cron next-firing doesn't interfere.
    pause_url = f"http://127.0.0.1:5000/admin/jobs/{job_id}/pause"
    urllib.request.urlopen(urllib.request.Request(pause_url, method="POST")).read()

    # Read all log content since trigger.
    with open(LOG_PATH, "rb") as f:
        f.seek(start_size)
        new = f.read().decode("utf-8", errors="replace")

    # Print success/error lines + apscheduler ack + any ERROR/WARNING/Traceback.
    interesting = []
    for line in new.splitlines():
        if (
            success_token in line
            or fail_token in line
            or "apscheduler.executors" in line
            or "ERROR" in line
            or ("WARNING" in line and "blueprints.admin" not in line)
            or "Exception" in line
            or "Traceback" in line
        ):
            interesting.append(line)

    print(f"[status] completed={completed}")
    print(f"[log lines] ({len(interesting)} matched)")
    for line in interesting:
        print(f"  {line}")

    return 0 if completed else 1


if __name__ == "__main__":
    sys.exit(main())
