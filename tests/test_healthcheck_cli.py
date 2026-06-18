"""Tests for the out-of-process ``job-cannon healthcheck`` CLI (#434).

Layers:
- ``compute_verdict`` truth table — pure-function unit tests (ok/degraded/down).
- ``read_heartbeat`` staleness boundary + sentinels against a seeded temp DB.
- ``read_degraded_sources`` against seeded ``source_health`` rows.
- ``read_liveness`` against a hand-written ``server.json`` marker.
- a CLI-level end-to-end test that invokes ``python -m job_finder healthcheck``
  in a subprocess and asserts the exit code + parsed-JSON stdout — and, by
  construction, that no listener is bound and no pidfile lock is taken.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_finder.json_utils import utc_now_iso
from job_finder.web.healthcheck import (
    HeartbeatVerdict,
    LivenessVerdict,
    compute_verdict,
    read_degraded_sources,
    read_heartbeat,
    read_liveness,
    run_healthcheck,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _naive_utc(offset_hours: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(hours=offset_hours)).replace(tzinfo=None).isoformat()


def _seed_health_row(conn, status: str, *, age_hours: float = 0.0, issues=None) -> None:
    conn.execute(
        "INSERT INTO user_activity (action, metadata, occurred_at) VALUES (?, ?, ?)",
        (
            "scheduled_health",
            json.dumps({"status": status, "issues": issues or []}),
            _naive_utc(-age_hours),
        ),
    )
    conn.commit()


def _seed_degraded_source(conn, source: str) -> None:
    conn.execute(
        "INSERT INTO source_health (source, surface, status, updated_at) "
        "VALUES (?, 'web', 'degraded', ?)",
        (source, utc_now_iso()),
    )
    conn.commit()


def _alive() -> LivenessVerdict:
    return LivenessVerdict(alive=True, pid=1234, reason="process pid 1234 alive")


def _fresh_success() -> HeartbeatVerdict:
    return HeartbeatVerdict(status="success", stale=False, issues=(), occurred_at=_naive_utc())


# ---------------------------------------------------------------------------
# compute_verdict — truth table (pure)
# ---------------------------------------------------------------------------


def test_verdict_ok_when_alive_fresh_and_clean():
    v = compute_verdict(_alive(), _fresh_success(), (), checked_at_utc="T")
    assert (v.status, v.exit_code) == ("ok", 0)
    assert v.reasons == ()
    assert v.degraded_sources == ()
    assert v.checked_at_utc == "T"


def test_verdict_down_when_not_alive():
    dead = LivenessVerdict(alive=False, pid=99, reason="process pid 99 is not alive")
    v = compute_verdict(dead, _fresh_success(), ())
    assert (v.status, v.exit_code) == ("down", 2)
    assert "not alive" in v.reasons[0]


def test_verdict_down_when_heartbeat_missing():
    hb = HeartbeatVerdict(status="missing", stale=True, issues=(), occurred_at=None)
    v = compute_verdict(_alive(), hb, ())
    assert (v.status, v.exit_code) == ("down", 2)
    assert "no health heartbeat" in v.reasons[0]


def test_verdict_down_when_db_unreadable():
    hb = HeartbeatVerdict(status="unreadable", stale=True, issues=(), occurred_at=None)
    v = compute_verdict(_alive(), hb, ())
    assert (v.status, v.exit_code) == ("down", 2)
    assert "unreadable" in v.reasons[0]


def test_verdict_degraded_when_heartbeat_degraded():
    hb = HeartbeatVerdict(
        status="degraded", stale=False, issues=("OAuth token invalid",), occurred_at=_naive_utc()
    )
    v = compute_verdict(_alive(), hb, ())
    assert (v.status, v.exit_code) == ("degraded", 1)
    assert "OAuth token invalid" in v.reasons


def test_verdict_degraded_when_heartbeat_stale():
    hb = HeartbeatVerdict(status="success", stale=True, issues=(), occurred_at=_naive_utc(-40))
    v = compute_verdict(_alive(), hb, ())
    assert (v.status, v.exit_code) == ("degraded", 1)
    assert "stale" in v.reasons[0]


def test_verdict_degraded_when_sources_degraded():
    v = compute_verdict(_alive(), _fresh_success(), ("serpapi", "thordata"))
    assert (v.status, v.exit_code) == ("degraded", 1)
    assert v.degraded_sources == ("serpapi", "thordata")
    assert any("serpapi" in r and "thordata" in r for r in v.reasons)


def test_verdict_down_dominates_degraded_sources():
    """A dead process is DOWN even if there are degraded sources to report."""
    dead = LivenessVerdict(alive=False, pid=None, reason="no server.json marker (app not running)")
    v = compute_verdict(dead, _fresh_success(), ("serpapi",))
    assert (v.status, v.exit_code) == ("down", 2)
    # The sources are still surfaced in the payload for the operator.
    assert v.degraded_sources == ("serpapi",)


def test_verdict_to_dict_shape():
    v = compute_verdict(
        _alive(), _fresh_success(), ("serpapi",), checked_at_utc="2026-06-18T00:00:00"
    )
    d = v.to_dict()
    assert set(d) == {"status", "exit_code", "reasons", "degraded_sources", "checked_at_utc"}
    assert isinstance(d["reasons"], list)
    assert d["degraded_sources"] == ["serpapi"]
    assert d["checked_at_utc"] == "2026-06-18T00:00:00"


def test_compute_verdict_is_pure_no_mutation():
    """A frozen HealthVerdict cannot be mutated — invalid states are unrepresentable."""
    v = compute_verdict(_alive(), _fresh_success(), ())
    with pytest.raises(Exception):
        v.status = "down"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# read_heartbeat — sentinels + staleness boundary
# ---------------------------------------------------------------------------


def test_read_heartbeat_missing_db():
    hb = read_heartbeat("/no/such/path/jobs.db", 26.0)
    assert hb.status == "missing"
    assert hb.stale is True


def test_read_heartbeat_no_row_is_missing(migrated_db):
    db_path, _conn = migrated_db
    hb = read_heartbeat(db_path, 26.0)
    assert hb.status == "missing"


def test_read_heartbeat_fresh_success(migrated_db):
    db_path, conn = migrated_db
    _seed_health_row(conn, "success")
    hb = read_heartbeat(db_path, 26.0)
    assert hb.status == "success"
    assert hb.stale is False
    assert hb.issues == ()


def test_read_heartbeat_degraded_carries_issues(migrated_db):
    db_path, conn = migrated_db
    _seed_health_row(conn, "degraded", issues=["No ingestion in last 14h"])
    hb = read_heartbeat(db_path, 26.0)
    assert hb.status == "degraded"
    assert hb.issues == ("No ingestion in last 14h",)


def test_read_heartbeat_staleness_boundary(migrated_db):
    db_path, conn = migrated_db
    # 25h old with a 26h window -> fresh; bump past the window -> stale.
    _seed_health_row(conn, "success", age_hours=25)
    assert read_heartbeat(db_path, 26.0).stale is False

    conn.execute("DELETE FROM user_activity")
    _seed_health_row(conn, "success", age_hours=27)
    assert read_heartbeat(db_path, 26.0).stale is True


def test_read_heartbeat_picks_latest_row(migrated_db):
    db_path, conn = migrated_db
    _seed_health_row(conn, "degraded", age_hours=48)
    _seed_health_row(conn, "success", age_hours=1)
    hb = read_heartbeat(db_path, 26.0)
    assert hb.status == "success"  # newest by occurred_at


def test_read_heartbeat_unreadable_db(tmp_path):
    """A file that is not a SQLite DB surfaces as 'unreadable' (-> DOWN)."""
    bogus = tmp_path / "jobs.db"
    bogus.write_bytes(b"this is definitely not a sqlite database")
    hb = read_heartbeat(str(bogus), 26.0)
    assert hb.status == "unreadable"


# ---------------------------------------------------------------------------
# read_degraded_sources
# ---------------------------------------------------------------------------


def test_read_degraded_sources_missing_db():
    assert read_degraded_sources("/no/such/jobs.db") == ()


def test_read_degraded_sources_none(migrated_db):
    db_path, _conn = migrated_db
    assert read_degraded_sources(db_path) == ()


def test_read_degraded_sources_sorted(migrated_db):
    db_path, conn = migrated_db
    _seed_degraded_source(conn, "thordata")
    _seed_degraded_source(conn, "serpapi")
    assert read_degraded_sources(db_path) == ("serpapi", "thordata")


# ---------------------------------------------------------------------------
# read_liveness — server.json marker
# ---------------------------------------------------------------------------


def _write_server_json(logs_dir: Path, payload: dict) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "server.json").write_text(json.dumps(payload), encoding="utf-8")


def test_read_liveness_no_marker(tmp_path):
    v = read_liveness(tmp_path / "logs")
    assert v.alive is False
    assert "no server.json" in v.reason


def test_read_liveness_unparseable_marker(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "server.json").write_text("{ not json", encoding="utf-8")
    v = read_liveness(logs)
    assert v.alive is False
    assert "unreadable" in v.reason


def test_read_liveness_no_pid(tmp_path):
    logs = tmp_path / "logs"
    _write_server_json(logs, {"url": "http://127.0.0.1:5000"})
    v = read_liveness(logs)
    assert v.alive is False
    assert "no valid pid" in v.reason


def test_read_liveness_alive_for_current_process(tmp_path):
    logs = tmp_path / "logs"
    _write_server_json(logs, {"pid": os.getpid(), "start_time_utc": datetime.now(UTC).isoformat()})
    v = read_liveness(logs)
    assert v.alive is True
    assert v.pid == os.getpid()


def test_read_liveness_dead_pid(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    _write_server_json(logs, {"pid": 4242, "start_time_utc": datetime.now(UTC).isoformat()})
    monkeypatch.setattr("job_finder.web.healthcheck.psutil.pid_exists", lambda _pid: False)
    v = read_liveness(logs)
    assert v.alive is False
    assert "4242" in v.reason


def test_read_liveness_alive_but_unparseable_start(tmp_path):
    logs = tmp_path / "logs"
    _write_server_json(logs, {"pid": os.getpid(), "start_time_utc": "not-a-time"})
    v = read_liveness(logs)
    assert v.alive is True
    assert "start_time_utc unparseable" in v.reason


# ---------------------------------------------------------------------------
# run_healthcheck — in-process wiring (prints JSON, returns code)
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.user_data_dir = kw.get("user_data_dir")
        self.heartbeat_max_age_hours = kw.get("heartbeat_max_age_hours", 26.0)
        self.json = True


def _seed_user_data_root(tmp_path) -> Path:
    """Build a user-data root with a live marker + migrated DB + fresh heartbeat."""
    from job_finder.web.db_migrate import run_migrations

    root = tmp_path / "ud"
    (root / "logs").mkdir(parents=True)
    _write_server_json(
        root / "logs", {"pid": os.getpid(), "start_time_utc": datetime.now(UTC).isoformat()}
    )
    db = root / "jobs.db"
    run_migrations(str(db))
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        _seed_health_row(conn, "success")
    finally:
        conn.close()
    return root


def test_run_healthcheck_ok_prints_json(tmp_path, capsys):
    root = _seed_user_data_root(tmp_path)
    code = run_healthcheck(_Args(user_data_dir=str(root)))
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0
    assert payload["degraded_sources"] == []
    assert payload["checked_at_utc"]


def test_run_healthcheck_degraded_source(tmp_path, capsys):
    root = _seed_user_data_root(tmp_path)
    import sqlite3

    conn = sqlite3.connect(str(root / "jobs.db"))
    try:
        _seed_degraded_source(conn, "serpapi")
    finally:
        conn.close()
    code = run_healthcheck(_Args(user_data_dir=str(root)))
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "degraded"
    assert payload["degraded_sources"] == ["serpapi"]


# ---------------------------------------------------------------------------
# CLI end-to-end — subprocess, real exit code + JSON stdout, no side effects
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_healthcheck_subprocess_ok(tmp_path):
    root = _seed_user_data_root(tmp_path)

    env = dict(os.environ)
    env["JOB_CANNON_USER_DATA_DIR"] = str(root)
    env["JOB_CANNON_NO_BROWSER"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "healthcheck", "--user-data-dir", str(root)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0

    # No listener was bound and no scheduler pidfile lock was taken: the probe
    # path never builds the app. server.lock / scheduler.pid must not appear.
    assert not (root / "logs" / "server.lock").exists()
    assert not (root / "logs" / "scheduler.pid").exists()
