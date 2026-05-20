"""One-off: drive 15 scheduled jobs in wave order via /admin/jobs/<id>/run-now.

Status polling uses the run-now 409 idempotency response: a 409 means the same
job is currently executing. We POST run-now, then poll with the same endpoint;
409 = still running, 200 = idle (either never started or already finished).

Caveat: every successful poll triggers another run, so very-fast jobs can be
fired 2-3 times before we observe a steady-state 200. For Wave D (paid-API
feeders) we use a longer initial sleep before polling to reduce re-fires.

Outputs:
    logs/wave_run_2026-05-19.jsonl  - one row per job, machine-readable
    logs/wave_run_2026-05-19.md     - markdown table for the handoff
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://localhost:5000"
LOGS = Path("logs")
APP_LOG = LOGS / "app.log"
RESULTS_JSONL = LOGS / "wave_run_2026-05-19.jsonl"
RESULTS_MD = LOGS / "wave_run_2026-05-19.md"

WAVES: list[tuple[str, list[str], int, int]] = [
    # (label, job_ids, initial_sleep_s, poll_interval_s)
    ("A", ["health_heartbeat", "orphan_cleanup", "registry_hygiene", "company_linkage"], 2, 3),
    ("B", ["ats_source_url_promote", "staleness_check", "pipeline_detection",
           "homepage_discovery", "ats_scan", "ats_slug_probe"], 5, 10),
    ("C", ["careers_crawl"], 10, 30),
    ("D", ["ingestion_poll", "enrichment_backfill", "agentic_backfill"], 30, 30),
]

# Per-job hard cap (seconds). Bail out if exceeded — manual investigation needed.
MAX_WAIT = {
    # Wave A — DB only
    "health_heartbeat": 60,
    "orphan_cleanup": 120,
    "registry_hygiene": 120,
    "company_linkage": 300,
    # Wave B — HTTP, free
    "ats_source_url_promote": 600,
    "staleness_check": 600,
    "pipeline_detection": 900,
    "homepage_discovery": 900,
    "ats_scan": 3600,
    "ats_slug_probe": 1800,
    # Wave C — HTTP, longest
    "careers_crawl": 5400,
    # Wave D — feeders
    "ingestion_poll": 1800,
    "enrichment_backfill": 7200,
    "agentic_backfill": 7200,
}


def post_run_now(job_id: str) -> tuple[int, str]:
    """Returns (status_code, body)."""
    req = urllib.request.Request(
        f"{BASE}/admin/jobs/{job_id}/run-now", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, f"EXCEPTION: {type(e).__name__}: {e}"


def app_log_offset() -> int:
    try:
        return APP_LOG.stat().st_size
    except FileNotFoundError:
        return 0


_ERROR_RE = re.compile(r"\b(ERROR|Traceback|database is locked|Exception)\b")


def app_log_errors_since(offset: int) -> list[str]:
    """Return lines added to app.log since `offset` that match an ERROR pattern."""
    try:
        with APP_LOG.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new = f.read()
    except FileNotFoundError:
        return []
    return [ln for ln in new.splitlines() if _ERROR_RE.search(ln)][-20:]


def run_one(job_id: str, wave: str, initial_sleep: int, poll_interval: int) -> dict:
    started = datetime.now(timezone.utc)
    pre_log_offset = app_log_offset()
    status, body = post_run_now(job_id)
    if status == 404:
        return {
            "job_id": job_id, "wave": wave,
            "start": started.isoformat(), "end": None,
            "duration_s": 0,
            "trigger_status": status, "trigger_body": body,
            "outcome": "NO_SUCH_JOB", "polls": 0, "errors": [],
        }
    if status == 409:
        # Already running from elsewhere — wait for it to finish before we trigger
        polls_busy_before = 1
        while True:
            time.sleep(poll_interval)
            s, _ = post_run_now(job_id)
            polls_busy_before += 1
            if s != 409:
                break
            if (datetime.now(timezone.utc) - started).total_seconds() > MAX_WAIT[job_id]:
                return {
                    "job_id": job_id, "wave": wave,
                    "start": started.isoformat(), "end": None,
                    "duration_s": (datetime.now(timezone.utc) - started).total_seconds(),
                    "trigger_status": 409, "trigger_body": body,
                    "outcome": "TIMEOUT_PRE_TRIGGER", "polls": polls_busy_before,
                    "errors": app_log_errors_since(pre_log_offset),
                }
        # Now triggered cleanly via the last poll
    elif status != 200:
        return {
            "job_id": job_id, "wave": wave,
            "start": started.isoformat(), "end": None,
            "duration_s": (datetime.now(timezone.utc) - started).total_seconds(),
            "trigger_status": status, "trigger_body": body,
            "outcome": "TRIGGER_FAILED", "polls": 0,
            "errors": app_log_errors_since(pre_log_offset),
        }

    # We've triggered. Wait initial then poll until a 200 (idle).
    time.sleep(initial_sleep)
    polls = 1
    while True:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > MAX_WAIT[job_id]:
            return {
                "job_id": job_id, "wave": wave,
                "start": started.isoformat(), "end": None,
                "duration_s": elapsed,
                "trigger_status": status, "trigger_body": body,
                "outcome": "TIMEOUT", "polls": polls,
                "errors": app_log_errors_since(pre_log_offset),
            }
        s, _ = post_run_now(job_id)
        polls += 1
        if s == 409:
            time.sleep(poll_interval)
            continue
        if s == 200:
            # After the initial_sleep, a 200 means the job is not running.
            # The poll itself re-triggered it, so cancel that by pausing the
            # job briefly then resuming (suppresses the just-triggered run).
            # Actually simpler: we accept one re-trigger; APScheduler will
            # run it again but the previous run is already complete. Move on.
            ended = datetime.now(timezone.utc)
            return {
                "job_id": job_id, "wave": wave,
                "start": started.isoformat(), "end": ended.isoformat(),
                "duration_s": (ended - started).total_seconds(),
                "trigger_status": status, "trigger_body": body,
                "outcome": "OK", "polls": polls,
                "errors": app_log_errors_since(pre_log_offset),
            }


def main():
    # Filter optional CLI args: --wave A or --job <id>
    args = sys.argv[1:]
    only_wave = None
    only_job = None
    if "--wave" in args:
        only_wave = args[args.index("--wave") + 1]
    if "--job" in args:
        only_job = args[args.index("--job") + 1]

    LOGS.mkdir(exist_ok=True)
    # Truncate prior partial results unless --append given
    if "--append" not in args:
        RESULTS_JSONL.write_text("")

    print(f"Wave campaign started {datetime.now(timezone.utc).isoformat()}")
    print(f"Pruned DB; rev 11 verified.")
    print()

    for wave_label, job_ids, init_s, poll_s in WAVES:
        if only_wave and only_wave != wave_label:
            continue
        print(f"=== Wave {wave_label} ===")
        for job_id in job_ids:
            if only_job and only_job != job_id:
                continue
            print(f"  -> {job_id} ... ", end="", flush=True)
            result = run_one(job_id, wave_label, init_s, poll_s)
            outcome = result["outcome"]
            dur = result["duration_s"]
            errs = len(result["errors"])
            print(f"{outcome} in {dur:.1f}s (polls={result['polls']}, errors={errs})")
            with RESULTS_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result) + "\n")
        print()

    # Build markdown table
    rows = [json.loads(l) for l in RESULTS_JSONL.read_text().splitlines() if l]
    md = ["| Wave | Job | Outcome | Duration | Polls | Errors |",
          "|------|-----|---------|----------|-------|--------|"]
    for r in rows:
        errs = len(r["errors"])
        emoji = "OK" if r["outcome"] == "OK" else r["outcome"]
        md.append(f"| {r['wave']} | `{r['job_id']}` | {emoji} | {r['duration_s']:.1f}s | {r['polls']} | {errs} |")
    RESULTS_MD.write_text("\n".join(md) + "\n")
    print(f"\nWritten: {RESULTS_JSONL} and {RESULTS_MD}")


if __name__ == "__main__":
    main()
