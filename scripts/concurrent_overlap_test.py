"""Concurrent-overlap re-test for the lock-contention fix.

Fires four write-heavy scheduled jobs simultaneously and inspects the rotated
log stream (logs/app.log + app.log.1..N) for `database is locked` errors,
OperationalError, or Traceback frames.

Expected: all four jobs complete without lock-contention failures.

Rotation-safety: rev 8 of NEXT_STEPS_2026-05-19.md notes a blind spot in the
prior implementation — RotatingFileHandler at 5MB rotates app.log mid-test,
and the old byte-offset seek then reads from a brand-new file at offset 0,
silently missing lock errors recorded in app.log.1. This version filters
by timestamp prefix across all rotated files, so rotation cannot hide
errors.

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

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_PATH = LOG_DIR / "app.log"
BASE = "http://127.0.0.1:5000"

# Walk rotated logs from oldest -> newest so the merged stream is monotonic.
# RotatingFileHandler keeps backupCount=3 → app.log.{3,2,1} plus app.log.
_ROTATION_SUFFIXES = (".3", ".2", ".1", "")
_TS_LEN = 19  # "YYYY-MM-DD HH:MM:SS"

# job_id -> human-readable name the scheduler logs as "<name>: ..." on success
# or "<name> failed: ..." on failure.
TARGETS = {
    "orphan_cleanup": "Orphan cleanup",
    "registry_hygiene": "Registry hygiene",
    "staleness_check": "Stale detection",
    "ats_source_url_promote": "ATS source url promote",
}

# Lock-bound jobs: must complete in-window. These are the writers whose
# 245s recalibration UPDATE was rev 8's bug.
# Everything else in TARGETS is HTTP-bound (per-company ATS API calls); a
# timeout on those does NOT indicate a lock-contention regression, so the
# verdict logic treats them as informational rather than failing.
LOCK_BOUND = {"orphan_cleanup", "registry_hygiene"}


def _post(path: str) -> str:
    req = urllib.request.Request(BASE + path, method="POST")
    return urllib.request.urlopen(req, timeout=10).read().decode().strip()


def _fire(job_id: str, results: dict[str, str]) -> None:
    try:
        results[job_id] = _post(f"/admin/jobs/{job_id}/run-now")
    except Exception as exc:  # noqa: BLE001
        results[job_id] = f"ERROR: {exc!r}"


def _is_timestamped(line: str) -> bool:
    """True if the line begins with a YYYY-MM-DD HH:MM:SS timestamp prefix."""
    if len(line) < _TS_LEN:
        return False
    return (
        line[4] == "-"
        and line[7] == "-"
        and line[10] == " "
        and line[13] == ":"
        and line[16] == ":"
    )


def _gather_since(threshold: str) -> str:
    """Return concatenated log lines emitted at or after `threshold`.

    Reads all rotated files (app.log.3 .. app.log.1, app.log) in chronological
    order, filters by leading timestamp prefix. Continuation lines from
    multiline log records (tracebacks, etc.) inherit the in-window/out-of-window
    state of the entry they belong to.

    `threshold` is a "YYYY-MM-DD HH:MM:SS" string — lexical compare is correct
    because the timestamp format is fixed-width and zero-padded.
    """
    kept: list[str] = []
    in_window = False
    for suffix in _ROTATION_SUFFIXES:
        path = LOG_DIR / f"app.log{suffix}"
        if not path.exists():
            continue
        try:
            with open(path, "rb") as fh:
                for raw in fh:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if _is_timestamped(line):
                        in_window = line[:_TS_LEN] >= threshold
                    if in_window:
                        kept.append(line)
        except OSError as exc:
            # Rotation can race a read; missing file is fine, real OS errors
            # surface to stderr but don't abort the inspection.
            print(f"[warn] could not read {path}: {exc!r}", file=sys.stderr)
    return "\n".join(kept)


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

    # 2. Snapshot wall-clock baseline. We filter the rotated stream by
    # timestamp prefix, which is rotation-safe (a rotation during the test
    # cannot move an event "before" the threshold).
    # Subtract 1 second of slack: log timestamps are second-precision while
    # time.time() is sub-second; lines emitted in our same second should be
    # included so a sentinel posted at T+ε is captured.
    start_struct = time.localtime(time.time() - 1.0)
    threshold = time.strftime("%Y-%m-%d %H:%M:%S", start_struct)
    print(f"[setup] threshold timestamp: {threshold}")

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

    # 4. Poll the rotated stream for completion of each job.
    print(f"[poll] waiting up to {max_wait}s for all 4 to finish")
    completed: set[str] = set()
    failed: set[str] = set()
    deadline = time.time() + max_wait
    while time.time() < deadline and (completed | failed) != set(TARGETS):
        time.sleep(2)
        chunk = _gather_since(threshold)
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

    # 6. Inspect full delta for lock-contention markers across rotated files.
    full = _gather_since(threshold)

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
    # Lock-contention is the primary regression-test question — check first.
    if locked_lines:
        print("VERDICT: REGRESSION — lock-contention errors present")
        return 1

    # Lock-bound jobs MUST complete or fail within window. Missing or failed
    # is a regression.
    missing_lock_bound = LOCK_BOUND - completed - failed
    if missing_lock_bound:
        print(
            "VERDICT: REGRESSION — lock-bound job(s) did not complete in window: "
            f"{sorted(missing_lock_bound)}"
        )
        return 1
    if failed & LOCK_BOUND:
        print(
            "VERDICT: REGRESSION — lock-bound job(s) failed: "
            f"{sorted(failed & LOCK_BOUND)}"
        )
        return 1
    if failed:
        # HTTP-bound jobs that actually emitted "<Name> failed:" lines are
        # still a fail — just not a lock-contention regression.
        print(f"VERDICT: FAIL — non-lock-bound job(s) failed: {sorted(failed)}")
        return 1

    http_pending = sorted(set(TARGETS) - LOCK_BOUND - completed - failed)
    note = f" (HTTP-bound still running: {http_pending})" if http_pending else ""
    print(f"VERDICT: PASS — 0 lock-contention errors, lock-bound jobs OK{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
