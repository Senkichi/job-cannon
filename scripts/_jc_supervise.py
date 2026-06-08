#!/usr/bin/env python3
"""Detached supervisor for a single overnight harness job.

Watches a runner process (by PID) and its job log, and writes structured
lifecycle events to the SAME ``run_events.jsonl`` the runner writes to. Its
whole reason to exist: record the disposition the runner *cannot* — when the
runner is reaped (killed without a clean exit) or wedged (alive but its log
frozen), the runner never gets to emit ``run_end``. Because this supervisor is
launched as its own OS-detached process (the survivor pattern), it outlives the
runner and records ``reaped`` / ``stalled`` so a silent fail becomes a logged,
diagnosable fact.

It disambiguates a clean exit from a reap with NO IPC: on seeing the PID gone,
it checks the shared event log for a terminal event with the same ``run_id``
(``run_events.find_terminal``). Present -> the runner exited cleanly; absent ->
reaped.

run_id convention: the harness runner uses the deterministic ``"{job}:{pid}"``
form (``run_events.make_run_id(job, pid, unique=False)``), which this process
reconstructs from the ``--pid`` / ``--job`` it is given. They therefore agree
without sharing any state.

Usage (launched detached by the orchestrator, NOT by the runner — so it can't
be reaped together with it):
    uv run python scripts/_jc_supervise.py --pid <runner_pid> --job enrichment \
        --log overnight_logs/17_enrichment_drain2.log --db <abs jobs.db> \
        --stall-sec 420 --interval 60 --max-min 120

Requires JC_RUN_EVENTS_PATH in the environment (inherited from the launcher) so
it appends to the same stream as the runner.
"""

import argparse
import os
import sys
import time

# Make ``job_finder`` importable when run via ``uv run`` from the repo root.
from job_finder.web import run_events

PROGRESS_MARKER = "purpose=score_job"


# --------------------------------------------------------------------------- #
# Pure decision core (unit-tested in tests/test_run_supervisor.py)
# --------------------------------------------------------------------------- #
def classify(alive: bool, log_age: float | None, stall_sec: float, run_end_seen: bool) -> str:
    """Decide the supervisor's next action.

    Precedence:
      1. ``clean_exit`` — a terminal event for this run is already on disk
         (the runner finished and wrote ``run_end``); nothing to record.
      2. ``reaped``     — process gone AND no terminal event on disk.
      3. ``stalled``    — process alive but its log has been frozen past the
                          stall threshold (wedged).
      4. ``continue``   — healthy; keep watching.
    """
    if run_end_seen:
        return "clean_exit"
    if not alive:
        return "reaped"
    if log_age is not None and log_age > stall_sec:
        return "stalled"
    return "continue"


# --------------------------------------------------------------------------- #
# Liveness + log progress
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    """Best-effort cross-platform liveness check (psutil -> ctypes -> os.kill)."""
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError, ValueError):
        # OverflowError/ValueError: a pid larger than the platform pid_t max
        # (e.g. a sentinel "definitely dead" pid on 32-bit pid_t Linux) cannot
        # name a live process — treat as not alive rather than propagating.
        return False


def read_log(path: str) -> tuple[float | None, dict]:
    """Return (log_age_seconds, progress dict) for the job log.

    progress = {score_job, last_line, log_age_s}. Age is None if the log is
    missing/unreadable (so the caller does not false-trip a stall before the
    runner has written its first line).
    """
    progress: dict = {"score_job": 0, "last_line": None, "log_age_s": None}
    try:
        age = time.time() - os.path.getmtime(path)
        progress["log_age_s"] = round(age)
        scored = 0
        last = None
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if PROGRESS_MARKER in line:
                    scored += 1
                stripped = line.rstrip("\n")
                if stripped:
                    last = stripped
        progress["score_job"] = scored
        progress["last_line"] = last[-240:] if last else None
        return age, progress
    except OSError:
        return None, progress


# --------------------------------------------------------------------------- #
# Watch loop
# --------------------------------------------------------------------------- #
def supervise(args: argparse.Namespace) -> int:
    run_id = run_events.make_run_id(args.job, args.pid, unique=False)
    events = run_events.events_path()
    deadline = time.time() + args.max_min * 60

    while time.time() < deadline:
        alive = pid_alive(args.pid)
        log_age, progress = read_log(args.log) if args.log else (None, {})
        run_end_seen = run_events.find_terminal(run_id, events) is not None
        decision = classify(alive, log_age, args.stall_sec, run_end_seen)

        if decision == "continue":
            run_events.heartbeat(
                run_id,
                job=args.job,
                source="supervisor",
                pid=args.pid,
                progress=progress or None,
                db_path=args.db,
            )
            time.sleep(args.interval)
            continue

        if decision == "clean_exit":
            # Runner already wrote run_end; nothing to add. Stop quietly.
            return 0

        if decision == "reaped":
            run_events.mark(
                "reaped",
                run_id,
                job=args.job,
                source="supervisor",
                pid=args.pid,
                detail="process gone with no run_end on disk",
                last_progress=progress or None,
                db_counters=run_events.db_counters(args.db),
            )
            return 0

        if decision == "stalled":
            run_events.mark(
                "stalled",
                run_id,
                job=args.job,
                source="supervisor",
                pid=args.pid,
                log_age_s=progress.get("log_age_s"),
                stall_threshold_s=args.stall_sec,
                last_progress=progress or None,
            )
            return 0

    # Watcher hit its own ceiling; the job may still be running. Record that the
    # supervision window ended so the absence of a later terminal event is not
    # mistaken for "never watched".
    run_events.mark(
        "supervisor_timeout",
        run_id,
        job=args.job,
        source="supervisor",
        pid=args.pid,
        watched_min=args.max_min,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Detached supervisor for one harness job run.")
    parser.add_argument("--pid", type=int, required=True, help="runner process id to watch")
    parser.add_argument("--job", required=True, help="job key (matches the runner's job)")
    parser.add_argument("--log", default=None, help="path to the runner's job log")
    parser.add_argument("--db", default=None, help="absolute jobs.db path for counter snapshots")
    parser.add_argument("--stall-sec", type=float, default=420.0, dest="stall_sec")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-min", type=float, default=120.0, dest="max_min")
    args = parser.parse_args()
    return supervise(args)


if __name__ == "__main__":
    sys.exit(main())
