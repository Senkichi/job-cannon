"""Tests for the two owner-facing health alarms wired into ``run_health_check``.

These are the pre-mortem's first instruments — the cheap, inward-facing canaries
that watch the owner's own relationship to the tool:

  - ``_check_owner_idle``  -- "nobody's home": no HUMAN action in N days while the
    schedulers keep firing (death-mode #1, the founder-gets-a-job cliff).
  - ``_check_score_rot``   -- stored verdicts that today's classification rule
    would change, re-derived from saved sub-scores with ZERO model calls
    (death-mode #3, the only rot with no self-healing re-sweep).

Both are read-only and feed the existing per-signal escalation/dedup, so the
integration tests assert the new signals surface as issues and escalate under
their own keys.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask

from job_finder.web.scheduler._runners import (
    _check_owner_idle,
    _check_score_rot,
    run_health_check,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A clean "apply" tuple: all >= 3, exactly 3 strong axes (>= 4), mean == 3.5.
# Under the default apply_mean_floor (3.5) it classifies "apply"; raise the floor
# to 3.6 and the same row re-derives "consider" — our deterministic drift lever.
_APPLY = {
    "title_fit": 4,
    "location_fit": 4,
    "comp_fit": 4,
    "domain_match": 3,
    "seniority_match": 3,
    "skills_match": 3,
}
# A stable "consider" tuple: all >= 2, not all-3, never reaches the apply branch,
# so it is invariant to apply_mean_floor changes (no phantom drift).
_CONSIDER = {
    "title_fit": 3,
    "location_fit": 3,
    "comp_fit": 3,
    "domain_match": 3,
    "seniority_match": 3,
    "skills_match": 2,
}


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


def _days_ago(n: float) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).replace(tzinfo=None).isoformat()


def _ua_conn() -> sqlite3.Connection:
    """Minimal user_activity table (the only columns the idle check reads)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE user_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "action TEXT NOT NULL, entity_id TEXT, metadata TEXT, occurred_at TEXT NOT NULL)"
    )
    return conn


def _add_activity(conn: sqlite3.Connection, action: str, days_ago: float) -> None:
    conn.execute(
        "INSERT INTO user_activity (action, occurred_at) VALUES (?, ?)",
        (action, _days_ago(days_ago)),
    )
    conn.commit()


def _jobs_conn() -> sqlite3.Connection:
    """Minimal jobs table (only the columns the score-rot check reads)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY, classification TEXT, "
        "sub_scores_json TEXT, legitimacy_note TEXT, enrichment_tier TEXT, "
        "jd_full TEXT, scoring_model TEXT)"
    )
    return conn


def _add_job(
    conn: sqlite3.Connection,
    key: str,
    classification: str | None,
    sub_scores,
    *,
    scoring_model: str | None = "qwen2.5:14b",
    legitimacy_note: str | None = None,
    enrichment_tier: str | None = None,
    jd_full: str = "x" * 2000,
) -> None:
    if sub_scores is None:
        sub_json = None
    elif isinstance(sub_scores, str):
        sub_json = sub_scores  # raw (possibly malformed) payload
    else:
        sub_json = json.dumps(sub_scores)
    conn.execute(
        "INSERT INTO jobs (dedup_key, classification, sub_scores_json, legitimacy_note, "
        "enrichment_tier, jd_full, scoring_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, classification, sub_json, legitimacy_note, enrichment_tier, jd_full, scoring_model),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _check_owner_idle
# ---------------------------------------------------------------------------


def test_owner_idle_fires_past_threshold():
    conn = _ua_conn()
    _add_activity(conn, "expand_job", days_ago=20)
    issue = _check_owner_idle(conn, {"health": {"owner_idle_days": 14}})
    assert issue is not None and issue.startswith("Owner idle")
    assert "threshold 14d" in issue


def test_owner_idle_silent_when_recent():
    conn = _ua_conn()
    _add_activity(conn, "status_change", days_ago=2)
    assert _check_owner_idle(conn, {"health": {"owner_idle_days": 14}}) is None


def test_owner_idle_ignores_scheduled_actions():
    """A fresh scheduled row must NOT reset the human-idle clock."""
    conn = _ua_conn()
    _add_activity(conn, "expand_job", days_ago=30)  # stale human action
    _add_activity(conn, "scheduled_sync", days_ago=0)  # scheduler still firing
    _add_activity(conn, "scheduled_health", days_ago=0)
    issue = _check_owner_idle(conn, {"health": {"owner_idle_days": 14}})
    assert issue is not None and issue.startswith("Owner idle")


def test_owner_idle_silent_when_no_human_action_ever():
    """A brand-new install (only system rows) is not nagged."""
    conn = _ua_conn()
    _add_activity(conn, "scheduled_sync", days_ago=40)
    assert _check_owner_idle(conn, {"health": {"owner_idle_days": 14}}) is None


def test_owner_idle_disabled_by_nonpositive_threshold():
    conn = _ua_conn()
    _add_activity(conn, "expand_job", days_ago=100)
    assert _check_owner_idle(conn, {"health": {"owner_idle_days": 0}}) is None


def test_owner_idle_default_threshold_is_14():
    conn = _ua_conn()
    _add_activity(conn, "rescore", days_ago=10)
    assert _check_owner_idle(conn, {}) is None  # 10 < 14 default
    conn2 = _ua_conn()
    _add_activity(conn2, "rescore", days_ago=16)
    assert _check_owner_idle(conn2, {}) is not None  # 16 >= 14 default


# ---------------------------------------------------------------------------
# _check_score_rot
# ---------------------------------------------------------------------------


def test_score_rot_silent_when_no_drift():
    conn = _jobs_conn()
    _add_job(conn, "a", "apply", _APPLY)
    _add_job(conn, "b", "consider", _CONSIDER)
    assert _check_score_rot(conn, {}) is None  # default thresholds reproduce stored


def test_score_rot_fires_on_threshold_drift():
    conn = _jobs_conn()
    _add_job(conn, "a", "apply", _APPLY)  # stored 'apply' under floor 3.5
    issue = _check_score_rot(conn, {"scoring": {"apply_mean_floor": 3.6}})
    assert issue is not None and issue.startswith("Score rot")
    assert "1/1" in issue  # the one audited row drifted apply -> consider


def test_score_rot_excludes_low_signal_rows():
    """low_signal verdicts are unauditable (un-persisted 'degenerate' input)."""
    conn = _jobs_conn()
    # Stored low_signal but the saved sub-scores would re-derive 'apply'; must be
    # excluded from the audit, so the result is None (audited == 0), not drift.
    _add_job(conn, "ls", "low_signal", dict.fromkeys(_APPLY, 5))
    assert _check_score_rot(conn, {"scoring": {"apply_mean_floor": 3.6}}) is None


def test_score_rot_skips_heuristic_and_null_subscores():
    conn = _jobs_conn()
    _add_job(conn, "heuristic", "consider", _CONSIDER, scoring_model=None)  # excluded by SQL
    _add_job(conn, "nullsub", "consider", None)  # excluded by SQL (sub_scores NULL)
    _add_job(conn, "garbage", "apply", "not-json-at-all")  # parsed -> skipped, no raise
    # Nothing auditable -> None, and crucially no exception.
    assert _check_score_rot(conn, {"scoring": {"apply_mean_floor": 3.6}}) is None


def test_score_rot_respects_fraction_floor():
    conn = _jobs_conn()
    _add_job(conn, "drift", "apply", _APPLY)  # drifts under floor 3.6
    for i in range(3):
        _add_job(conn, f"stable{i}", "consider", _CONSIDER)  # stable
    # 1 drift / 4 audited = 0.25.
    cfg = {"scoring": {"apply_mean_floor": 3.6}}
    assert _check_score_rot(conn, {**cfg, "health": {"score_rot_fraction": 0.5}}) is None
    issue = _check_score_rot(conn, {**cfg, "health": {"score_rot_fraction": 0.1}})
    assert issue is not None and "25.0%" in issue


def test_score_rot_disabled_when_floor_above_one():
    conn = _jobs_conn()
    _add_job(conn, "a", "apply", _APPLY)
    cfg = {"scoring": {"apply_mean_floor": 3.6}, "health": {"score_rot_fraction": 1.5}}
    assert _check_score_rot(conn, cfg) is None


# ---------------------------------------------------------------------------
# Integration through run_health_check
# ---------------------------------------------------------------------------


def test_run_health_check_surfaces_owner_idle(migrated_db, events_file):
    """A stale human action surfaces as an 'Owner idle' issue on the health row."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {"health": {"owner_idle_days": 14}}

    recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("expand_job", None, "{}", _days_ago(20)),  # stale human action
            ("scheduled_sync", None, "{}", recent),  # keep ingestion/staleness green
            ("scheduled_staleness", None, "{}", recent),
        ],
    )
    conn.commit()

    with patch("job_finder.gmail_auth.get_credentials", return_value=object()):
        run_health_check(app)

    row = conn.execute(
        "SELECT metadata FROM user_activity WHERE action = 'scheduled_health' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    issues = json.loads(row[0])["issues"]
    assert any(i.startswith("Owner idle") for i in issues), issues


def test_owner_idle_participates_in_escalation(migrated_db, events_file):
    """The owner_idle signal escalates under its own key after the threshold."""
    db_path, conn = migrated_db
    app = _make_app(db_path)
    app.config["JF_CONFIG"] = {"health": {"owner_idle_days": 14}}

    recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    conn.executemany(
        "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) VALUES (?, ?, ?, ?)",
        [
            ("save_jd", None, "{}", _days_ago(30)),
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
            run_health_check(app)

    assert fire.call_count == 1
    escalated_keys = {e["signal_key"] for e in fire.call_args.args[0]}
    assert "owner_idle" in escalated_keys
