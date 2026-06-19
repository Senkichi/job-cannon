"""Tests for ``run_health_check`` -- the daily health heartbeat runner.

Verifies the issue #235 fix: the verdict now reaches durable channels
(``user_activity`` + ``run_events``) instead of being discarded in a log line.

Covered:
- degraded path: empty DB -> ``scheduled_health`` row with ``status='degraded'``
  and a non-empty ``issues`` list; ``run_events.end`` disposition='degraded'.
- nominal path: seeded recent ``scheduled_sync`` + ``scheduled_staleness``
  rows with ``get_credentials`` mocked to succeed -> ``status='success'`` and
  empty ``issues``; disposition='completed'.
- best-effort contract: ``run_health_check`` does not raise even when the
  configured DB_PATH is bogus.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask

from job_finder.web.activity_tracker import ACTION_SCHEDULED_HEALTH
from job_finder.web.scheduler._runners import run_health_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["TESTING"] = True
    return app


def _utc_naive(offset_hours: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(hours=offset_hours)).replace(tzinfo=None).isoformat()


def _fetch_health_rows(conn) -> list:
    return conn.execute(
        "SELECT action, metadata FROM user_activity WHERE action = ?",
        (ACTION_SCHEDULED_HEALTH,),
    ).fetchall()


def _read_events(events_path) -> list[dict]:
    if not events_path.exists():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    """Redirect run_events.jsonl to a tmp file via the documented env override."""
    path = tmp_path / "run_events.jsonl"
    monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# Degraded path
# ---------------------------------------------------------------------------


def test_degraded_writes_activity_row_and_run_end(migrated_db, events_file):
    """Empty user_activity -> 'No ingestion in last 14h' (and more) -> degraded."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    # Force OAuth to "fail" so the degraded path is deterministic regardless of
    # whether the test host actually has Gmail credentials.
    with patch(
        "job_finder.gmail_auth.get_credentials",
        side_effect=RuntimeError("no creds in test"),
    ):
        run_health_check(app)

    rows = _fetch_health_rows(conn)
    assert len(rows) == 1, f"expected exactly 1 scheduled_health row, got {len(rows)}"
    meta = json.loads(rows[0]["metadata"])
    assert meta["status"] == "degraded"
    assert isinstance(meta["issues"], list) and len(meta["issues"]) > 0
    # At minimum we expect the two missing-cadence issues.
    issue_text = " ; ".join(meta["issues"])
    assert "No ingestion in last 14h" in issue_text
    assert "Stale detection missed last night" in issue_text

    events = _read_events(events_file)
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert events[1]["disposition"] == "degraded"
    assert events[0]["job"] == events[1]["job"] == "health"
    assert events[0]["source"] == events[1]["source"] == "scheduler"


# ---------------------------------------------------------------------------
# Nominal path
# ---------------------------------------------------------------------------


def test_nominal_writes_success_row_and_completed_run_end(migrated_db, events_file):
    """Recent sync + staleness rows + OAuth ok -> status='success', completed."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    # Seed recent rows so signals 1 + 2 are satisfied. Signal 3 (>=5 failures
    # of any one action) is already satisfied trivially -- table is otherwise
    # empty. Signal 4 (OAuth) is mocked to succeed.
    now_iso = _utc_naive(-0.5)  # 30 minutes ago, well within both windows
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("scheduled_sync", None, "{}", now_iso),
            ("scheduled_staleness", None, "{}", now_iso),
        ],
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    rows = _fetch_health_rows(conn)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata"])
    assert meta["status"] == "success", f"unexpected issues: {meta.get('issues')}"
    assert meta["issues"] == []

    events = _read_events(events_file)
    # Only run_start + run_end for this one invocation, in this order.
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert events[1]["disposition"] == "completed"


# ---------------------------------------------------------------------------
# Best-effort contract
# ---------------------------------------------------------------------------


def test_does_not_raise_on_bogus_db_path(tmp_path, events_file):
    """Best-effort contract: bogus DB_PATH must not propagate an exception."""
    bogus = tmp_path / "definitely-not-here" / "missing.db"
    app = _make_app(str(bogus))

    # Mock OAuth so a missing Gmail credential file doesn't masquerade as the
    # behavior under test (we want to be sure it's the DB error that's swallowed).
    with patch(
        "job_finder.gmail_auth.get_credentials",
        side_effect=RuntimeError("no creds in test"),
    ):
        # Must not raise.
        run_health_check(app)

    # And the run_events envelope still gets emitted (start + end), terminating
    # in 'degraded' because the DB error appended an issue.
    events = _read_events(events_file)
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert events[1]["disposition"] == "degraded"


# ---------------------------------------------------------------------------
# Action constant
# ---------------------------------------------------------------------------


def test_action_constant_value():
    """The new constant is wired to the documented string."""
    assert ACTION_SCHEDULED_HEALTH == "scheduled_health"


# ---------------------------------------------------------------------------
# Escalation (#440): N-consecutive-degraded fires the notification egress
# ---------------------------------------------------------------------------


def _make_nominal(conn) -> None:
    """Seed recent sync + staleness rows so signals 1 + 2 pass (OAuth still mocked)."""
    now_iso = _utc_naive(-0.5)
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("scheduled_sync", None, "{}", now_iso),
            ("scheduled_staleness", None, "{}", now_iso),
        ],
    )
    conn.commit()


def _esc_state(conn) -> dict:
    """Return {signal_key: sqlite3.Row} for health_escalation_state."""
    return {
        r["signal_key"]: r
        for r in conn.execute("SELECT * FROM health_escalation_state").fetchall()
    }


def test_escalation_fires_at_threshold(migrated_db, events_file):
    """N consecutive degraded runs -> exactly one egress call; counters >= N."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        for _ in range(3):  # default threshold
            run_health_check(app)

    assert fire.call_count == 1, f"expected exactly one egress fire, got {fire.call_count}"
    state = _esc_state(conn)
    assert state["ingestion"]["consecutive_degraded"] >= 3
    assert state["staleness"]["consecutive_degraded"] >= 3
    # The escalated payload carried both crossing keys.
    escalated_keys = {e["signal_key"] for e in fire.call_args.args[0]}
    assert {"ingestion", "staleness"} <= escalated_keys


def test_no_premature_fire(migrated_db, events_file):
    """N-1 consecutive degraded runs produce zero egress calls."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        for _ in range(2):  # threshold - 1
            run_health_check(app)

    assert fire.call_count == 0
    state = _esc_state(conn)
    assert state["ingestion"]["consecutive_degraded"] == 2


def test_reset_on_recovery(migrated_db, events_file):
    """A nominal run resets counters; the streak must restart before re-firing."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    # One degraded run -> ingestion/staleness counters at 1.
    with patch(
        "job_finder.gmail_auth.get_credentials",
        side_effect=RuntimeError("no creds in test"),
    ):
        run_health_check(app)
    assert _esc_state(conn)["ingestion"]["consecutive_degraded"] == 1

    # Nominal run -> counters reset to 0.
    _make_nominal(conn)
    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)
    state = _esc_state(conn)
    assert state["ingestion"]["consecutive_degraded"] == 0
    assert state["ingestion"]["last_status"] == "healthy"
    assert state["ingestion"]["last_escalated_at"] is None

    # Wipe the nominal seed so subsequent runs degrade again.
    conn.execute(
        "DELETE FROM user_activity WHERE action IN ('scheduled_sync', 'scheduled_staleness')"
    )
    conn.commit()

    # A fresh degraded streak must again reach the threshold before firing.
    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        run_health_check(app)
        run_health_check(app)
        assert fire.call_count == 0  # only 2 in the new streak
        run_health_check(app)
        assert fire.call_count == 1  # third crosses threshold


def test_fire_once_suppression(migrated_db, events_file):
    """After firing, further consecutive degraded runs do not re-fire."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        for _ in range(6):  # well past the threshold of 3
            run_health_check(app)

    assert fire.call_count == 1, "escalation must fire exactly once per streak"
    state = _esc_state(conn)
    assert state["ingestion"]["consecutive_degraded"] == 6
    assert state["ingestion"]["last_escalated_at"] is not None


def test_escalation_does_not_propagate_egress_exception(migrated_db, events_file):
    """A forced exception inside the egress hook must not escape run_health_check."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch(
            "job_finder.web.notifications.notify",
            side_effect=RuntimeError("egress boom"),
        ),
    ):
        for _ in range(3):  # would fire on the third run
            run_health_check(app)  # must not raise

    # The health row was still written despite the egress blowing up.
    rows = _fetch_health_rows(conn)
    assert len(rows) == 3


def test_threshold_is_config_driven(migrated_db, events_file):
    """A custom escalation_consecutive_threshold changes the run count to fire."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {"health": {"escalation_consecutive_threshold": 2}}

    with (
        patch(
            "job_finder.gmail_auth.get_credentials",
            side_effect=RuntimeError("no creds in test"),
        ),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        run_health_check(app)
        assert fire.call_count == 0  # one run, threshold is 2
        run_health_check(app)
        assert fire.call_count == 1  # second run crosses the custom threshold
