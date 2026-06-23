"""Out-of-process health verdict for the ``job-cannon healthcheck`` CLI (#434).

Invoked on a cadence by the **OS scheduler** (Windows Task Scheduler / cron /
launchd) — NOT by the in-process APScheduler. It MUST NOT call ``create_app()``,
construct a scheduler, or acquire the pidfile lock. It reads state directly from
the on-disk liveness markers (``logs/server-<slug>.json``, the launcher's
``(host, port)``-keyed sidecars) and the SQLite DB (**read-only**), computes a
verdict from three inputs, and returns a deterministic exit code plus a
machine-readable JSON verdict on stdout.

Three inputs:
1. **Liveness** — the launcher's ``logs/server-<slug>.json`` markers (legacy
   fixed-name ``server.json`` also honoured): is any JC process alive
   (``psutil.pid_exists``) and is its ``start_time_utc`` parseable?
2. **Heartbeat** — the most recent ``scheduled_health`` row in ``user_activity``
   (its ``metadata.status`` / ``metadata.issues``), and whether it is *stale*
   (older than ``--heartbeat-max-age-hours``).
3. **Degraded sources** — rows in ``source_health WHERE status = 'degraded'``.

Exit codes (locked):
    0 = OK            — live process, fresh non-degraded heartbeat, no degraded sources.
    1 = DEGRADED      — degraded/stale heartbeat, or any degraded source.
    2 = DOWN/UNKNOWN  — no live process, unreadable DB, or no heartbeat ever recorded.

Read-only DB access: the DB is opened through a ``file:...?mode=ro`` URI so an
external probe can never *create* an empty DB (plain ``sqlite3.connect`` would)
nor mutate the live one (no ``PRAGMA journal_mode=WAL`` write) — the correct
contract for a passive observer.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from job_finder.json_utils import utc_now_iso

# Default staleness window for the daily ``scheduled_health`` heartbeat, in
# hours. Matches the 26h window ``run_health_check`` uses for "missed last
# night" (_runners.py) so the external probe and the in-process check agree.
DEFAULT_HEARTBEAT_MAX_AGE_HOURS = 26.0


# ---------------------------------------------------------------------------
# Immutable verdict value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivenessVerdict:
    """Result of probing ``server.json`` for a live JC process."""

    alive: bool
    pid: int | None
    reason: str


@dataclass(frozen=True)
class HeartbeatVerdict:
    """Result of reading the latest ``scheduled_health`` row.

    ``status`` is the row's ``metadata.status`` (``success`` / ``degraded``) or
    a probe-level sentinel: ``missing`` (no row / no DB), ``unreadable`` (DB
    open or query error), or ``unknown`` (row present but no status field).
    """

    status: str
    stale: bool
    issues: tuple[str, ...]
    occurred_at: str | None


@dataclass(frozen=True)
class HealthVerdict:
    """The reduced verdict emitted as JSON and mapped to an exit code."""

    status: str  # "ok" | "degraded" | "down"
    exit_code: int  # 0 | 1 | 2
    reasons: tuple[str, ...]
    degraded_sources: tuple[str, ...]
    checked_at_utc: str

    def to_dict(self) -> dict:
        """Return the stdout JSON payload (ordered, list-valued collections)."""
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "reasons": list(self.reasons),
            "degraded_sources": list(self.degraded_sources),
            "checked_at_utc": self.checked_at_utc,
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (naive or tz-aware), or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _is_stale(occurred_at: str | None, max_age_hours: float) -> bool:
    """True if ``occurred_at`` (naive UTC) is older than ``max_age_hours``.

    An unparseable / missing timestamp is treated as stale — the probe cannot
    confirm freshness, so it errs toward DEGRADED rather than a false OK.
    """
    parsed = _parse_iso(occurred_at)
    if parsed is None:
        return True
    # scheduled_health.occurred_at is naive UTC; normalize defensively in case a
    # tz-aware value ever sneaks in, then compare against naive-UTC "now".
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    now = _parse_iso(utc_now_iso())
    assert now is not None  # utc_now_iso() is always parseable
    return (now - parsed).total_seconds() / 3600.0 > max_age_hours


def _safe_json(value: str | None) -> dict:
    """Parse a JSON object string, or return {} on any error / non-object."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ro_connect(db_path: str) -> sqlite3.Connection:
    """Open ``db_path`` read-only (URI ``mode=ro``); raises if the file is absent.

    Read-only by construction: never creates the DB and never issues the WAL
    pragma, so the live app's DB is never mutated by a healthcheck.
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# The three readers (each isolates its own failure mode)
# ---------------------------------------------------------------------------


def read_liveness(logs_dir: Path) -> LivenessVerdict:
    """Probe ``logs_dir`` for a live JC process across all instance markers.

    Markers are ``(host, port)``-keyed (``server-<slug>.json``; the legacy
    fixed-name ``server.json`` is also matched for backward compatibility), so
    this scans every ``server*.json`` and reports the first one naming a live
    pid — a machine-level "is *any* Job Cannon up?" probe. When markers exist
    but none is alive, the most recently inspected dead verdict is returned so
    the caller still sees *why*; when no marker exists at all, the app is simply
    not running.
    """
    markers = sorted(logs_dir.glob("server*.json"))
    if not markers:
        return LivenessVerdict(alive=False, pid=None, reason="no server marker (app not running)")
    last_dead = LivenessVerdict(alive=False, pid=None, reason="no server marker (app not running)")
    for marker in markers:
        verdict = _read_one_marker(marker)
        if verdict.alive:
            return verdict
        last_dead = verdict
    return last_dead


def _read_one_marker(server_json: Path) -> LivenessVerdict:
    """Liveness verdict for a single ``server*.json`` marker file.

    Each failure mode (unreadable, not an object, no valid pid, dead pid) gets
    its own reason so a caller inspecting a lone marker still learns the cause.
    """
    try:
        meta = json.loads(server_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return LivenessVerdict(
            alive=False, pid=None, reason=f"{server_json.name} unreadable: {exc}"
        )
    if not isinstance(meta, dict):
        return LivenessVerdict(
            alive=False, pid=None, reason=f"{server_json.name} is not an object"
        )
    pid = meta.get("pid")
    if not isinstance(pid, int):
        return LivenessVerdict(
            alive=False, pid=None, reason=f"{server_json.name} has no valid pid"
        )
    try:
        alive = psutil.pid_exists(pid)
    except Exception:
        alive = False
    if not alive:
        return LivenessVerdict(alive=False, pid=pid, reason=f"process pid {pid} is not alive")
    start = meta.get("start_time_utc")
    parseable = _parse_iso(start) is not None
    suffix = "" if parseable else " (start_time_utc unparseable)"
    return LivenessVerdict(alive=True, pid=pid, reason=f"process pid {pid} alive{suffix}")


def read_heartbeat(db_path: str, max_age_hours: float) -> HeartbeatVerdict:
    """Read the latest ``scheduled_health`` row and classify fresh-vs-stale.

    Sentinels: ``missing`` (no DB / no row), ``unreadable`` (open or query
    error). Otherwise the row's ``metadata.status`` with a computed ``stale``.
    """
    if not Path(db_path).exists():
        return HeartbeatVerdict(status="missing", stale=True, issues=(), occurred_at=None)
    try:
        conn = _ro_connect(db_path)
    except sqlite3.Error:
        return HeartbeatVerdict(status="unreadable", stale=True, issues=(), occurred_at=None)
    try:
        row = conn.execute(
            "SELECT occurred_at, metadata FROM user_activity "
            "WHERE action = 'scheduled_health' ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return HeartbeatVerdict(status="unreadable", stale=True, issues=(), occurred_at=None)
    finally:
        conn.close()
    if row is None:
        return HeartbeatVerdict(status="missing", stale=True, issues=(), occurred_at=None)
    occurred_at = row["occurred_at"]
    meta = _safe_json(row["metadata"])
    status = str(meta.get("status", "unknown"))
    issues = tuple(str(i) for i in meta.get("issues", []) if i)
    return HeartbeatVerdict(
        status=status,
        stale=_is_stale(occurred_at, max_age_hours),
        issues=issues,
        occurred_at=occurred_at,
    )


def read_degraded_sources(db_path: str) -> tuple[str, ...]:
    """Return the sorted names of sources currently flagged ``degraded``.

    Returns ``()`` on a missing/unreadable DB or absent ``source_health`` table —
    the heartbeat reader owns the DOWN-on-unreadable signal, so this stays quiet.
    """
    if not Path(db_path).exists():
        return ()
    try:
        conn = _ro_connect(db_path)
    except sqlite3.Error:
        return ()
    try:
        rows = conn.execute(
            "SELECT source FROM source_health WHERE status = 'degraded' ORDER BY source"
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        conn.close()
    return tuple(r["source"] for r in rows)


# ---------------------------------------------------------------------------
# Pure reducer
# ---------------------------------------------------------------------------


def compute_verdict(
    liveness: LivenessVerdict,
    heartbeat: HeartbeatVerdict,
    degraded: tuple[str, ...] | list[str],
    *,
    checked_at_utc: str = "",
) -> HealthVerdict:
    """Reduce the three inputs to a single verdict. Pure — mutates nothing.

    Precedence: DOWN (exit 2) conditions are checked first (no live process, or
    no/unreadable health signal), then DEGRADED (exit 1) accumulators, else OK.
    ``checked_at_utc`` is injected by the caller so this stays deterministic.
    """
    degraded_sources = tuple(degraded)

    # DOWN / UNKNOWN (exit 2) — checked first; these dominate any degrade signal.
    if not liveness.alive:
        return HealthVerdict("down", 2, (liveness.reason,), degraded_sources, checked_at_utc)
    if heartbeat.status == "missing":
        return HealthVerdict(
            "down", 2, ("no health heartbeat ever recorded",), degraded_sources, checked_at_utc
        )
    if heartbeat.status == "unreadable":
        return HealthVerdict(
            "down", 2, ("health database unreadable",), degraded_sources, checked_at_utc
        )

    # DEGRADED (exit 1) — accumulate every distinct cause for an actionable verdict.
    reasons: list[str] = []
    if heartbeat.stale:
        reasons.append(f"health heartbeat stale (last at {heartbeat.occurred_at})")
    if heartbeat.status == "degraded":
        reasons.extend(heartbeat.issues or ("health heartbeat reported degraded",))
    if degraded_sources:
        reasons.append("degraded sources: " + ", ".join(degraded_sources))
    if reasons:
        return HealthVerdict("degraded", 1, tuple(reasons), degraded_sources, checked_at_utc)

    return HealthVerdict("ok", 0, (), degraded_sources, checked_at_utc)


# ---------------------------------------------------------------------------
# CLI entry — resolve paths, gather inputs, print JSON, return the exit code
# ---------------------------------------------------------------------------


def run_healthcheck(args) -> int:
    """Resolve paths from ``args``, compute the verdict, print JSON, return code.

    Never starts the app: imports only ``user_data_dirs`` for path resolution
    and reads the DB read-only. Honors ``--user-data-dir`` by setting
    ``JOB_CANNON_USER_DATA_DIR`` before any path resolves.
    """
    import os

    from job_finder.web import user_data_dirs

    override = getattr(args, "user_data_dir", None)
    if override:
        os.environ["JOB_CANNON_USER_DATA_DIR"] = override

    root = user_data_dirs.user_data_root()
    logs_dir = root / "logs"
    db = str(user_data_dirs.db_path())
    max_age = float(getattr(args, "heartbeat_max_age_hours", DEFAULT_HEARTBEAT_MAX_AGE_HOURS))

    liveness = read_liveness(logs_dir)
    heartbeat = read_heartbeat(db, max_age)
    degraded = read_degraded_sources(db)
    verdict = compute_verdict(liveness, heartbeat, degraded, checked_at_utc=utc_now_iso())

    print(json.dumps(verdict.to_dict()))
    return verdict.exit_code
