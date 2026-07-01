"""Tests for the cost-ledger free/paid health watch (Detector C, issue #581).

The detector groups the scoring_costs ledger by provider over a trailing N-day
window and flags two regressions of the free-first AI provider cascade:
  1. Paid inference detected: any paid-provider row appears (surprise spend).
  2. Free providers absent: zero free-provider rows despite scoring activity
     (broken free rung).

Both are read-only and feed the existing per-signal escalation/dedup, so the
integration tests assert the new signal surfaces as an issue and escalates
under its own key.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask

from job_finder.web.scheduler._runners import (
    _check_cost_health,
    _derive_degraded_keys,
    run_health_check,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["TESTING"] = True
    return app


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    """Redirect run_events.jsonl to a tmp file via the documented env override."""
    path = tmp_path / "run_events.jsonl"
    monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(path))
    return path


def _costs_conn() -> sqlite3.Connection:
    """Minimal scoring_costs table (the only columns the cost-health check reads)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE scoring_costs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id TEXT, purpose TEXT, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd REAL, timestamp TEXT, provider TEXT)"
    )
    return conn


def _add_cost(
    conn: sqlite3.Connection,
    provider: str,
    *,
    days_ago: float = 0,
    cost: float = 0.0,
) -> None:
    """Insert a scoring_costs row with a real-now-relative naive-UTC ISO timestamp."""
    timestamp = (
        (datetime.now(UTC) - timedelta(days=days_ago))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
        "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test-job", "score_job", "test-model", 100, 50, cost, timestamp, provider),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _check_cost_health (pure function unit tests)
# ---------------------------------------------------------------------------


def test_free_provider_present_is_healthy():
    """A free-provider row in the window returns None (healthy)."""
    conn = _costs_conn()
    _add_cost(conn, "ollama", days_ago=1)
    assert _check_cost_health(conn, {"health": {"cost_health_window_days": 7}}) is None


def test_paid_leak_detected():
    """A paid-provider row in the window fires the paid-leak alarm."""
    conn = _costs_conn()
    _add_cost(conn, "anthropic_api", days_ago=1)
    issue = _check_cost_health(conn, {"health": {"cost_health_window_days": 7}})
    assert issue is not None
    assert issue.startswith("Cost health:")
    assert "anthropic_api" in issue


def test_free_rung_broke():
    """Only paid providers in the window fires both paid-leak and free-rung-broke alarms."""
    conn = _costs_conn()
    _add_cost(conn, "groq", days_ago=1)
    _add_cost(conn, "groq", days_ago=2)
    _add_cost(conn, "groq", days_ago=3)
    issue = _check_cost_health(
        conn, {"health": {"cost_health_window_days": 7, "cost_health_min_activity": 1}}
    )
    assert issue is not None
    assert "paid inference detected: groq" in issue
    assert "free providers absent" in issue


def test_empty_ledger_is_healthy():
    """No scoring_costs rows returns None (healthy)."""
    conn = _costs_conn()
    assert _check_cost_health(conn, {"health": {"cost_health_window_days": 7}}) is None


def test_below_min_activity_not_alarmed():
    """Below min_activity threshold, the 'free providers absent' arm is silent."""
    conn = _costs_conn()
    _add_cost(conn, "ollama", days_ago=1)
    _add_cost(conn, "ollama", days_ago=2)
    # Below min_activity=5, free providers present → silent
    assert (
        _check_cost_health(
            conn, {"health": {"cost_health_window_days": 7, "cost_health_min_activity": 5}}
        )
        is None
    )

    # Below min_activity=5, paid rows present → paid-leak fires but free-rung-broke is silent
    conn2 = _costs_conn()
    _add_cost(conn2, "anthropic_api", days_ago=1)
    _add_cost(conn2, "anthropic_api", days_ago=2)
    issue = _check_cost_health(
        conn2, {"health": {"cost_health_window_days": 7, "cost_health_min_activity": 5}}
    )
    assert issue is not None
    assert "paid inference detected: anthropic_api" in issue
    assert "free providers absent" not in issue


def test_window_excludes_old_rows():
    """Rows older than the window do not influence the verdict."""
    conn = _costs_conn()
    _add_cost(conn, "anthropic_api", days_ago=30)
    assert _check_cost_health(conn, {"health": {"cost_health_window_days": 7}}) is None


def test_disabled_via_window():
    """window_days <= 0 disables the entire check."""
    conn = _costs_conn()
    _add_cost(conn, "anthropic_api", days_ago=1)
    assert _check_cost_health(conn, {"health": {"cost_health_window_days": 0}}) is None


def test_free_providers_imported_not_hardcoded():
    """FREE_PROVIDERS is imported, not hardcoded -- a real free provider is healthy."""
    from job_finder.web.claude_client import FREE_PROVIDERS

    # Pick a real free provider from the set
    free_provider = next(iter(FREE_PROVIDERS))
    conn = _costs_conn()
    _add_cost(conn, free_provider, days_ago=1)
    assert _check_cost_health(conn, {"health": {"cost_health_window_days": 7}}) is None


# ---------------------------------------------------------------------------
# Integration through run_health_check
# ---------------------------------------------------------------------------


def test_run_health_check_surfaces_cost_health(migrated_db, events_file):
    """A paid-provider row surfaces as a 'Cost health' issue on the health row."""
    import json

    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "health": {"cost_health_window_days": 7, "cost_health_min_activity": 1}
    }

    recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("scheduled_sync", None, "{}", recent),
            ("scheduled_staleness", None, "{}", recent),
        ],
    )
    conn.commit()

    # Seed a paid-provider row in scoring_costs
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
        "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test-job", "score_job", "test-model", 100, 50, 0.01, recent, "anthropic_api"),
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    row = conn.execute(
        "SELECT metadata FROM user_activity WHERE action = 'scheduled_health' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    issues = json.loads(row[0])["issues"]
    assert any(i.startswith("Cost health") for i in issues), issues


def test_cost_health_participates_in_escalation(migrated_db, events_file):
    """The cost_health signal escalates under its own key after the threshold."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {
        "health": {"cost_health_window_days": 7, "cost_health_min_activity": 1}
    }

    recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("scheduled_sync", None, "{}", recent),
            ("scheduled_staleness", None, "{}", recent),
        ],
    )
    conn.commit()

    with (
        patch("job_finder.gmail_auth.get_credentials", return_value=object()),
        patch("job_finder.web.scheduler._runners._fire_escalation") as fire,
    ):
        for _ in range(3):  # default escalation threshold
            # Re-seed the paid row before each call (load-bearing: escalation counter resets)
            conn.execute(
                "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
                "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-job", "score_job", "test-model", 100, 50, 0.01, recent, "anthropic_api"),
            )
            conn.commit()
            run_health_check(app)

    assert fire.call_count >= 1
    escalated_keys = {e["signal_key"] for e in fire.call_args.args[0]}
    assert "cost_health" in escalated_keys

    # Also assert the exact-set parity on _derive_degraded_keys
    assert _derive_degraded_keys(
        ["Cost health: paid inference detected: anthropic_api (3 calls)"], []
    ) == {"cost_health"}
