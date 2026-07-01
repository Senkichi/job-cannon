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
from job_finder.web.scheduler._runners import _check_source_deadman, run_health_check

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
    """Empty user_activity -> 'No ingestion in last Xh' (and more) -> degraded."""
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
    # At minimum we expect the two missing-cadence issues (now with derived windows).
    issue_text = " ; ".join(meta["issues"])
    assert "No ingestion in last" in issue_text  # derived window, not hardcoded 14h
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
# Check #3 (error-burst) separator safety
# ---------------------------------------------------------------------------


def test_error_burst_excludes_failures_older_than_24h(migrated_db, events_file):
    """Check #3 must use epoch math, not a 'T'-vs-space string compare.

    A failure row is stored 'T'-separated ('...T12:00'); datetime('now','-24 hours')
    renders space-separated ('... 14:00'). With a raw string compare, 'T' (0x54)
    sorts after space (0x20), so a genuinely >24h-old failure was wrongly counted.
    Epoch math (strftime('%s')) excludes it. 5 failures 26h ago must NOT fire.
    """
    db_path, conn = migrated_db
    app = _make_app(db_path)

    stale_iso = _utc_naive(-26)  # genuinely older than the 24h window
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [("linkedin_sync", None, '{"status": "failed"}', stale_iso) for _ in range(5)],
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    meta = json.loads(_fetch_health_rows(conn)[0]["metadata"])
    issue_text = " ; ".join(meta["issues"])
    assert "failures in 24h" not in issue_text, (
        f"26h-old failures must not be counted (separator bug); issues: {meta['issues']}"
    )


def test_error_burst_fires_on_recent_failures(migrated_db, events_file):
    """Positive control: 5 recent failures of one action DO fire check #3."""
    db_path, conn = migrated_db
    app = _make_app(db_path)

    recent_iso = _utc_naive(-1)  # well within the 24h window
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [("linkedin_sync", None, '{"status": "failed"}', recent_iso) for _ in range(5)],
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    meta = json.loads(_fetch_health_rows(conn)[0]["metadata"])
    issue_text = " ; ".join(meta["issues"])
    assert "linkedin_sync: 5 failures in 24h" in issue_text, (
        f"recent failure burst must fire; issues: {meta['issues']}"
    )


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


# ---------------------------------------------------------------------------
# Source deadman alarm (issue #588)
# ---------------------------------------------------------------------------


def test_source_deadman_disabled_when_tolerance_zero(migrated_db):
    """source_deadman_tolerance <= 0 disables the deadman check."""
    db_path, conn = migrated_db
    config = {"health": {"source_deadman_tolerance": 0}}
    issues = _check_source_deadman(conn, config)
    assert issues == []


def test_source_deadman_returns_empty_when_fresh(migrated_db):
    """Fresh timestamps in all ATS recency classes → no issues."""
    db_path, conn = migrated_db
    config = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed fresh data (within window)
    now_iso = _utc_naive(-0.5)  # 30 minutes ago
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status, last_scanned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("Test Company", "Test Company", "hit", now_iso, now_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO company_scan_log (company_id, scanned_at) VALUES (?, ?)",
        (1, now_iso),
    )
    conn.commit()

    issues = _check_source_deadman(conn, config)
    assert issues == []


def test_source_deadman_fires_when_stale(migrated_db):
    """Old timestamps exceed window * tolerance → issues returned."""
    db_path, conn = migrated_db
    config = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed stale data (ATS window is 24h daily, tolerance 2.0 = 48h allowed)
    # Use 50 hours ago to exceed the allowed window
    old_iso = _utc_naive(-50)
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status, last_scanned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("Stale Company", "Stale Company", "hit", old_iso, old_iso, old_iso),
    )
    conn.execute(
        "INSERT INTO company_scan_log (company_id, scanned_at) VALUES (?, ?)",
        (1, old_iso),
    )
    conn.commit()

    issues = _check_source_deadman(conn, config)
    assert len(issues) == 2
    assert all(issue.startswith("Source deadman:") for issue in issues)
    assert "ATS scanner fleet" in issues[0]
    assert "ATS scan log" in issues[1]


def test_source_deadman_coverage_key_in_derived_keys(migrated_db):
    """'Source deadman' issue prefix maps to 'coverage' key."""
    from job_finder.web.scheduler._runners import _derive_degraded_keys

    issues = ["Source deadman: ATS scanner fleet — no successful scan in 50.0h (window 24h)"]
    keys = _derive_degraded_keys(issues, [])
    assert "coverage" in keys


def test_source_deadman_uses_fixed_ats_window(migrated_db):
    """Deadman uses fixed 24h ATS window (not cadence_preset-dependent)."""
    db_path, conn = migrated_db

    # Both presets use the same 24h ATS window * 2.0 tolerance = 48h allowed
    config_standard = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "standard"},
    }

    config_heavy = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "heavy"},
    }

    # Seed data 23 hours ago (normal state: last night's 07:00 scan, seen at 06:00 heartbeat)
    # This should be silent on both presets (within 48h window)
    normal_iso = _utc_naive(-23)
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status, last_scanned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("Test Company", "Test Company", "hit", normal_iso, normal_iso, normal_iso),
    )
    conn.execute(
        "INSERT INTO company_scan_log (company_id, scanned_at) VALUES (?, ?)",
        (1, normal_iso),
    )
    conn.commit()

    # Both presets: within window → no issue
    issues_standard = _check_source_deadman(conn, config_standard)
    assert issues_standard == []

    issues_heavy = _check_source_deadman(conn, config_heavy)
    assert issues_heavy == []

    # Seed data 50 hours ago (exceeds 48h window)
    stale_iso = _utc_naive(-50)
    conn.execute(
        "UPDATE companies SET last_scanned_at = ? WHERE name = 'Test Company'",
        (stale_iso,),
    )
    conn.execute(
        "UPDATE company_scan_log SET scanned_at = ? WHERE company_id = 1",
        (stale_iso,),
    )
    conn.commit()

    # Both presets: exceeds window → issue
    issues_standard_stale = _check_source_deadman(conn, config_standard)
    assert len(issues_standard_stale) == 2

    issues_heavy_stale = _check_source_deadman(conn, config_heavy)
    assert len(issues_heavy_stale) == 2


def test_source_deadman_tolerance_zero_does_not_affect_signal_1(migrated_db, events_file):
    """tolerance=0 disables deadman but signal #1 (ingestion alarm) still works."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "health": {"source_deadman_tolerance": 0},
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed ingestion outage 20h ago (exceeds 8h standard window)
    old_iso = _utc_naive(-20)
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_sync", None, "{}", old_iso),
    )
    # Seed recent staleness to avoid signal #2 firing
    recent_iso = _utc_naive(-0.5)
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_staleness", None, "{}", recent_iso),
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    rows = _fetch_health_rows(conn)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata"])
    # Signal #1 should fire (ingestion alarm is independent of tolerance)
    assert any("No ingestion in last 8h" in issue for issue in meta["issues"])
    # Deadman should be silent (tolerance=0 disables it)
    assert not any("Source deadman" in issue for issue in meta["issues"])


def test_signal_1_separator_safe_with_t_separated_timestamps(migrated_db, events_file):
    """Signal #1 uses epoch math, so 'T'-separated timestamps work correctly."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed ingestion outage 20h ago with 'T'-separated ISO timestamp
    old_iso = _utc_naive(-20)  # This produces 'T'-separated format
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_sync", None, "{}", old_iso),
    )
    # Seed recent staleness to avoid signal #2 firing
    recent_iso = _utc_naive(-0.5)
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_staleness", None, "{}", recent_iso),
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    rows = _fetch_health_rows(conn)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata"])
    # Signal #1 should fire (epoch math handles 'T' separator correctly)
    assert any("No ingestion in last 8h" in issue for issue in meta["issues"])
    # Should be exactly one issue (no double-count from deadman)
    assert len(meta["issues"]) == 1


def test_deadman_end_to_end_surface(migrated_db, events_file):
    """End-to-end: stale ATS fleet state surfaces through run_health_check."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed stale ATS fleet (50h ago, exceeds 48h window)
    stale_iso = _utc_naive(-50)
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status, last_scanned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("Stale Company", "Stale Company", "hit", stale_iso, stale_iso, stale_iso),
    )
    conn.execute(
        "INSERT INTO company_scan_log (company_id, scanned_at) VALUES (?, ?)",
        (1, stale_iso),
    )
    # Seed recent staleness to avoid signal #2 firing
    recent_iso = _utc_naive(-0.5)
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_staleness", None, "{}", recent_iso),
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    rows = _fetch_health_rows(conn)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata"])
    # Deadman issues should surface
    assert any("Source deadman" in issue for issue in meta["issues"])
    # Should have exactly 2 deadman issues (ATS fleet + scan log)
    deadman_issues = [i for i in meta["issues"] if "Source deadman" in i]
    assert len(deadman_issues) == 2


def test_deadman_end_to_end_escalation(migrated_db, events_file):
    """End-to-end: stale deadman state escalates to 'coverage' key after threshold."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "health": {"source_deadman_tolerance": 2.0},
        "scheduler": {"cadence_preset": "standard"},
    }

    # Seed stale ATS fleet (50h ago, exceeds 48h window)
    stale_iso = _utc_naive(-50)
    conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status, last_scanned_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("Stale Company", "Stale Company", "hit", stale_iso, stale_iso, stale_iso),
    )
    conn.execute(
        "INSERT INTO company_scan_log (company_id, scanned_at) VALUES (?, ?)",
        (1, stale_iso),
    )
    # Seed recent staleness to avoid signal #2 firing
    recent_iso = _utc_naive(-0.5)
    conn.execute(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        ("scheduled_staleness", None, "{}", recent_iso),
    )
    conn.commit()

    with (
        patch("job_finder.gmail_auth.get_credentials", return_value=object()),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        # Run 3 times to hit the default threshold of 3
        for _ in range(3):
            run_health_check(app)

    assert fire.call_count == 1
    escalated_keys = {e["signal_key"] for e in fire.call_args.args[0]}
    assert "coverage" in escalated_keys

    # Verify escalation state
    state = _esc_state(conn)
    assert state["coverage"]["consecutive_degraded"] >= 3
