"""Structured orchestration event log (``run_events.jsonl``).

One append-only JSON-lines stream capturing the lifecycle of every job run —
from BOTH the production APScheduler runners (in-process, via the scheduler
factories) and the manual overnight harness (out-of-process, watched by a
detached supervisor). One event per line.

WHY THIS EXISTS
---------------
The app's internal logs are already rich (per-tier, per-model-call,
per-persist), but nothing recorded the orchestration *envelope*: job launched
-> pid -> progress -> exit DISPOSITION. When a run died silently (a reaped or
stalled background task), it left no first-class record; the cause had to be
reconstructed forensically. This stream makes disposition explicit::

    run_start -> [heartbeat ...] -> run_end{disposition: completed|degraded|failed}

…or, when a clean ``run_end`` can't be written because the process was reaped
or wedged, a *separate* detached supervisor records ``reaped`` / ``stalled``
instead (it disambiguates by checking whether a terminal event for the same
``run_id`` already exists in this log).

DESIGN INVARIANTS
-----------------
* **Emission never raises into the caller.** Instrumentation failure must not
  break a job — every public helper swallows and logs a warning.
* **Events are immutable.** Each record is built fresh and never mutated.
* **Append is one ``json.dumps(...) + "\\n"`` write per event.** Line-sized
  appends are atomic enough for this single-host, low-concurrency use (a
  handful of scheduler jobs plus one supervisor). Documented, not lock-guarded.
* **Timestamps are naive UTC ISO** (the project's store-UTC / render-local
  invariant) — never ``datetime.utcnow()`` (deprecated) and never local time.

PATH RESOLUTION
---------------
``$JC_RUN_EVENTS_PATH`` if set (the harness points this at ``overnight_logs/``),
else ``<user-data>/logs/run_events.jsonl`` (the standard production location,
beside ``app.log``). No ``config.yaml`` surface — keeps the config-surface
guard obligations at zero.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_RESULT_CLIP = 500


# --------------------------------------------------------------------------- #
# Path + time primitives
# --------------------------------------------------------------------------- #
def events_path() -> Path:
    """Resolve the events-log path and ensure its parent dir exists.

    Env override (``JC_RUN_EVENTS_PATH``) wins; otherwise default beside the
    app log under the user-data root.
    """
    env = os.environ.get("JC_RUN_EVENTS_PATH")
    if env:
        path = Path(env)
    else:
        from job_finder.web import user_data_dirs

        path = user_data_dirs.logs_path().parent / "run_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utc_iso() -> str:
    """Naive UTC ISO-8601 to second resolution (store-UTC / render-local)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def make_run_id(job: str, pid: int, *, unique: bool = True) -> str:
    """Correlation id linking a run's start/heartbeat/end events.

    ``unique=False`` yields the deterministic ``"{job}:{pid}"`` form the harness
    uses so a *separate* supervisor process — which only knows (job, pid) — can
    reconstruct the same id. ``unique=True`` appends an epoch suffix so a
    long-lived in-process scheduler (one pid, many runs over days) does not
    collide run ids across runs.
    """
    return f"{job}:{pid}:{int(time.time())}" if unique else f"{job}:{pid}"


# --------------------------------------------------------------------------- #
# Low-level append (never raises)
# --------------------------------------------------------------------------- #
def _append(record: dict) -> None:
    try:
        line = json.dumps(record, default=str, ensure_ascii=False)
        with open(events_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break a job
        logger.warning("run_events: emit failed (%s): %s", type(exc).__name__, exc)


def _emit(event: str, *, run_id: str, job: str, source: str, **fields) -> dict:
    """Build one immutable event record and append it. Returns the record."""
    record = {
        "v": SCHEMA_VERSION,
        "ts": _utc_iso(),
        "event": event,
        "run_id": run_id,
        "job": job,
        "source": source,
    }
    # Drop None-valued extras so lines stay compact and greppable.
    record.update({key: val for key, val in fields.items() if val is not None})
    _append(record)
    return record


# --------------------------------------------------------------------------- #
# DB counters (read-only, slim) — the before/after deltas per run
# --------------------------------------------------------------------------- #
def db_counters(db_path: str | os.PathLike | None) -> dict | None:
    """Read-only snapshot of the few counters that matter for run deltas.

    Opens the DB ``mode=ro`` so it can never mutate the live DB. Any failure
    returns ``{"error": ...}`` rather than raising.
    """
    if not db_path:
        return None
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:

            def scalar(sql: str):
                row = conn.execute(sql).fetchone()
                return row[0] if row else None

            out["total_jobs"] = scalar("SELECT COUNT(*) FROM jobs")
            out["scoring_backlog"] = scalar(
                "SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND jd_full != '' "
                "AND classification IS NULL AND (pipeline_status IS NULL OR "
                "pipeline_status NOT IN ('archived','dismissed'))"
            )
            out["classification_null"] = scalar(
                "SELECT COUNT(*) FROM jobs WHERE classification IS NULL"
            )
            out["missing_jd_full"] = scalar(
                "SELECT COUNT(*) FROM jobs WHERE jd_full IS NULL OR jd_full = ''"
            )
            out["first_seen_today"] = scalar(
                "SELECT COUNT(*) FROM jobs WHERE date(first_seen)=date('now')"
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return out


def _delta(before: dict | None, after: dict | None) -> dict | None:
    """Integer field-wise (after - before). None unless both are dicts."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    out = {
        key: after[key] - val
        for key, val in before.items()
        if isinstance(val, int) and isinstance(after.get(key), int)
    }
    return out or None


# --------------------------------------------------------------------------- #
# High-level lifecycle helpers
# --------------------------------------------------------------------------- #
def start(
    *,
    job: str,
    source: str,
    pid: int | None = None,
    db_path: str | os.PathLike | None = None,
    db_before: dict | None = None,
    cmd: str | None = None,
    run_id: str | None = None,
) -> str:
    """Emit ``run_start``; return the ``run_id`` (generated if not supplied).

    If ``db_before`` is not supplied but ``db_path`` is, a counter snapshot is
    taken. The returned ``run_id`` MUST be passed back to :func:`end`.
    """
    pid = os.getpid() if pid is None else pid
    run_id = run_id or make_run_id(job, pid)
    if db_before is None and db_path:
        db_before = db_counters(db_path)
    _emit(
        "run_start",
        run_id=run_id,
        job=job,
        source=source,
        pid=pid,
        cmd=cmd,
        db_before=db_before,
    )
    return run_id


def end(
    run_id: str,
    *,
    job: str,
    source: str,
    disposition: str,
    pid: int | None = None,
    db_path: str | os.PathLike | None = None,
    db_before: dict | None = None,
    duration_s: float | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    result: object = None,
) -> None:
    """Emit ``run_end`` with the terminal ``disposition`` and a DB delta.

    ``disposition`` is one of ``completed`` | ``degraded`` | ``failed`` (the
    runner/factory owns these). ``reaped`` / ``stalled`` are owned by the
    supervisor via :func:`mark`.
    """
    after = db_counters(db_path) if db_path else None
    _emit(
        "run_end",
        run_id=run_id,
        job=job,
        source=source,
        pid=pid,
        disposition=disposition,
        duration_s=duration_s,
        exit_code=exit_code,
        error=error,
        result=(str(result)[:_RESULT_CLIP] if result is not None else None),
        db_after=after,
        db_delta=_delta(db_before, after),
    )


def heartbeat(
    run_id: str,
    *,
    job: str,
    source: str,
    pid: int | None = None,
    progress: dict | None = None,
    db_path: str | os.PathLike | None = None,
) -> None:
    """Emit a ``heartbeat`` (liveness + progress + optional counters)."""
    _emit(
        "heartbeat",
        run_id=run_id,
        job=job,
        source=source,
        pid=pid,
        progress=progress,
        db_counters=(db_counters(db_path) if db_path else None),
    )


def mark(event: str, run_id: str, *, job: str, source: str, pid: int | None = None, **fields) -> None:
    """Emit an arbitrary terminal/diagnostic event (e.g. ``reaped``, ``stalled``)."""
    _emit(event, run_id=run_id, job=job, source=source, pid=pid, **fields)


# --------------------------------------------------------------------------- #
# Read side (used by the supervisor + startup reconcile)
# --------------------------------------------------------------------------- #
def find_terminal(run_id: str, path: str | os.PathLike | None = None) -> str | None:
    """Return the disposition of a terminal event for ``run_id``, else None.

    Terminal events are ``run_end`` (disposition field) and ``reaped`` /
    ``stalled`` (the event name itself). Lets the supervisor tell a clean exit
    (a ``run_end`` already on disk) from a reap (none) without IPC.
    """
    target = Path(path) if path else events_path()
    try:
        if not target.exists():
            return None
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or run_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("run_id") != run_id:
                    continue
                if rec.get("event") == "run_end":
                    return rec.get("disposition") or "completed"
                if rec.get("event") in ("reaped", "stalled"):
                    return rec["event"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_events: find_terminal failed: %s", exc)
    return None
