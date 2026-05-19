"""Concurrent-overlap re-test for the lock-contention fix (rev 7).

Fires four write-heavy scheduled jobs simultaneously and inspects logs/app.log
for `database is locked` errors, OperationalError, or Traceback frames.

Expected: all four jobs complete without lock-contention failures.

Usage:
    uv run python scripts/concurrent_overlap_test.py [max_wait_seconds]

Default max wait is 180s. Requires Flask running on :5000 with
JOB_CANNON_SKIP_STARTUP_BACKFILLS=1 so startup backfills don't interfere.
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.request
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "app.log"
BASE = "http://127.0.0.1:5000"

# job_id -> human-readable name the scheduler logs as "<name>: ..." on success
# or "<name> failed: ..." on failure.
TARGETS = {
    "orphan_cleanup": "Orphan cleanup",
    "registry_hygiene": "Registry hygiene",
    "staleness_check": "Stale detection",
    "ats_source_url_promote": "ATS source url promote",
}


def _post(path: str) -> str:
    req = urllib.request.Request(BASE + path, method="POST")
    return urllib.request.urlopen(req, timeout=10).read().decode().strip()


def _fire(job_id: str, results: dict[str, str]) -> None:
    try:
        results[job_id] = _post(f"/admin/jobs/{job_id}/run-now")
    except Exception as exc:  # noqa: BLE001
        results[job_id] = f"ERROR: {exc!r}"


def main() -> int:
    max_wait = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0

    if not LOG_PATH.exists():
        print(f"[fatal] {LOG_PATH} not found; is Flask running?")
        return 2

    # 1. Pause all 4 to prevent cron-driven firings during the test window.
    print("[setup] pausing all 4 targets")
    for jid in TARGETS:
        try:
            print(f"  pause {jid}: {_post(f'/admin/jobs/{jid}/pause')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  pause {jid} FAILED: {exc!r}")
            return 1

    # 2. Snapshot log size as baseline.
    start_size = LOG_PATH.stat().st_size
    print(f"[setup] baseline log size: {start_size} bytes")

    # 3. Fire 4 run-now POSTs concurrently via threads.
    print("[fire] dispatching 4 run-now requests in parallel")
    results: dict[str, str] = {}
    threads = [
        threading.Thread(target=_fire, args=(jid, results), name=f"fire-{jid}")
        for jid in TARGETS
    ]
    fire_start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fire_elapsed = time.time() - fire_start
    print(f"[fire] all run-now responses returned in {fire_elapsed:.2f}s")
    for jid, resp in results.items():
        print(f"  {jid}: {resp}")

    # 4. Poll log for completion of each job.
    print(f"[poll] waiting up to {max_wait}s for all 4 to finish")
    completed: set[str] = set()
    failed: set[str] = set()
    deadline = time.time() + max_wait
    last_size = start_size
    while time.time() < deadline and (completed | failed) != set(TARGETS):
        time.sleep(2)
        with open(LOG_PATH, "rb") as f:
            f.seek(last_size)
            chunk = f.read().decode("utf-8", errors="replace")
            last_size = f.tell()
        for jid, name in TARGETS.items():
            if jid in completed or jid in failed:
                continue
            if f"{name} failed:" in chunk:
                failed.add(jid)
                print(f"  [{time.strftime('%H:%M:%S')}] {jid} FAILED")
            elif f"{name}:" in chunk:
                completed.add(jid)
                print(f"  [{time.strftime('%H:%M:%S')}] {jid} completed")

    # 5. Re-pause all 4 so cron doesn't interfere.
    print("[teardown] re-pausing all 4")
    for jid in TARGETS:
        try:
            _post(f"/admin/jobs/{jid}/pause")
        except Exception as exc:  # noqa: BLE001
            print(f"  pause {jid} FAILED: {exc!r}")

    # 6. Inspect full delta for lock-contention markers.
    with open(LOG_PATH, "rb") as f:
        f.seek(start_size)
        full = f.read().decode("utf-8", errors="replace")

    locked_lines = [ln for ln in full.splitlines() if "database is locked" in ln]
    op_error_lines = [
        ln for ln in full.splitlines() if "OperationalError" in ln and "is locked" not in ln
    ]
    traceback_lines = [
        ln for ln in full.splitlines() if "Traceback" in ln or "  File \"" in ln
    ]
    error_lines = [
        ln for ln in full.splitlines()
        if "ERROR" in ln and "admin" not in ln.lower()
    ]

    print()
    print("=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(f"completed: {sorted(completed)}")
    print(f"failed:    {sorted(failed)}")
    print(f"timeout:   {sorted(set(TARGETS) - completed - failed)}")
    print()
    print(f"'database is locked' lines: {len(locked_lines)}")
    for ln in locked_lines[:10]:
        print(f"  {ln}")
    print(f"other OperationalError lines: {len(op_error_lines)}")
    for ln in op_error_lines[:10]:
        print(f"  {ln}")
    print(f"Traceback / File lines: {len(traceback_lines)}")
    for ln in traceback_lines[:20]:
        print(f"  {ln}")
    print(f"other ERROR lines: {len(error_lines)}")
    for ln in error_lines[:10]:
        print(f"  {ln}")

    print()
    if (completed | failed) != set(TARGETS):
        print("VERDICT: TIMEOUT — not all jobs completed within window")
        return 1
    if failed:
        print("VERDICT: AT LEAST ONE JOB FAILED")
        return 1
    if locked_lines:
        print("VERDICT: REGRESSION — lock-contention errors present")
        return 1
    print("VERDICT: PASS — 4 concurrent writers, 0 lock-contention errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
