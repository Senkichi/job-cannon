"""Phase D / D1 episode-boundary attempt reset (invariant I1) + retry sweep.

``record_extraction``: a positive yield with NO override active ends the break
episode and resets ``heal_attempts``; positive yields THROUGH an override never
reset (a bad-but-yielding override must not grant itself an unbounded budget).

``run_health_check`` check 5: still-degraded sources are passed to ``run_heal``
daily; healthy sources whose last heal is older than ``heal_attempt_reset_days``
get their attempt budget back; sweep failures never break the heartbeat.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from flask import Flask

from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal import override_loader
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HTML = "<div class='job'><span class='title'>Engineer</span></div>" + "x" * 300

_EMAIL_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".title", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def _conn(tmp_path) -> tuple[str, sqlite3.Connection]:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return db, c


def _isolated_loader(tmp_path, monkeypatch) -> OverrideLoader:
    loader = OverrideLoader(overrides_root=tmp_path / "overrides")
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader


def _seed_attempts(conn, source: str, attempts: int, *, status="degraded", last_heal_at=None):
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, heal_attempts, last_heal_at) "
        "VALUES (?, 'email', ?, 0, 1.0, '', ?, ?)",
        (source, status, attempts, last_heal_at),
    )
    conn.commit()


def _attempts(conn, source: str) -> int:
    return conn.execute(
        "SELECT heal_attempts FROM source_health WHERE source = ?", (source,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Episode-boundary reset in record_extraction
# ---------------------------------------------------------------------------


def test_positive_yield_without_override_resets_attempts(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)  # empty loader → no override active
    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 2)

    hm.record_extraction(conn, "linkedin", "email", _HTML, job_count=3)

    assert _attempts(conn, "linkedin") == 0


def test_positive_yield_through_override_does_not_reset(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()
    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 2)

    hm.record_extraction(conn, "linkedin", "email", _HTML, job_count=3)

    assert _attempts(conn, "linkedin") == 2  # override active → no episode boundary


def test_zero_yield_does_not_reset(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 2)

    hm.record_extraction(conn, "linkedin", "email", _HTML, job_count=0)

    assert _attempts(conn, "linkedin") == 2


# ---------------------------------------------------------------------------
# Retry sweep (run_health_check check 5)
# ---------------------------------------------------------------------------


def _make_app(db_path: str, config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["TESTING"] = True
    app.config["JF_CONFIG"] = config or {}
    return app


def _run_health_check_quiet(app, tmp_path, monkeypatch):
    """Run the heartbeat with run-events redirected to tmp and OAuth mocked out."""
    from job_finder.web.scheduler._runners import run_health_check

    monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(tmp_path / "run_events.jsonl"))
    with patch("job_finder.gmail_auth.get_credentials", side_effect=RuntimeError("no creds")):
        run_health_check(app)


def test_sweep_passes_degraded_sources_to_run_heal(tmp_path, monkeypatch):
    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 0, status="degraded")
    _seed_attempts(conn, "ats:lever", 0, status="degraded")
    conn.commit()
    app = _make_app(db, {"autoheal": {"heal_enabled": True}})

    with patch("job_finder.web.autoheal.heal_pipeline.run_heal") as mock_rh:
        _run_health_check_quiet(app, tmp_path, monkeypatch)

    swept = sorted(c.args[2] for c in mock_rh.call_args_list)
    assert swept == ["ats:lever", "linkedin"]


def test_sweep_resets_stale_healthy_attempts(tmp_path, monkeypatch):
    """A source healthy with last_heal_at 31+ days back gets its budget back."""
    db, conn = _conn(tmp_path)
    _seed_attempts(
        conn, "linkedin", 3, status="healthy", last_heal_at="2026-01-01T00:00:00"
    )  # months stale (>= 31-day margin per the timestamp-granularity note)
    _seed_attempts(conn, "glassdoor", 2, status="healthy", last_heal_at=None)
    app = _make_app(db)

    _run_health_check_quiet(app, tmp_path, monkeypatch)

    assert _attempts(conn, "linkedin") == 0
    assert _attempts(conn, "glassdoor") == 2  # NULL last_heal_at → untouched


def test_sweep_does_not_reset_recent_healthy_attempts(tmp_path, monkeypatch):
    from job_finder.json_utils import utc_now_iso

    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 3, status="healthy", last_heal_at=utc_now_iso())
    app = _make_app(db)

    _run_health_check_quiet(app, tmp_path, monkeypatch)

    assert _attempts(conn, "linkedin") == 3


def test_sweep_failure_does_not_break_heartbeat(tmp_path, monkeypatch):
    """run_heal blowing up mid-sweep must not prevent the heartbeat verdict."""
    import json

    from job_finder.web.activity_tracker import ACTION_SCHEDULED_HEALTH

    db, conn = _conn(tmp_path)
    _seed_attempts(conn, "linkedin", 0, status="degraded")
    app = _make_app(db, {"autoheal": {"heal_enabled": True}})

    with patch("job_finder.web.autoheal.heal_pipeline.run_heal", side_effect=RuntimeError("boom")):
        _run_health_check_quiet(app, tmp_path, monkeypatch)  # must not raise

    rows = conn.execute(
        "SELECT metadata FROM user_activity WHERE action = ?", (ACTION_SCHEDULED_HEALTH,)
    ).fetchall()
    assert len(rows) == 1  # heartbeat verdict still landed
    meta = json.loads(rows[0][0])
    assert not any("autoheal" in i.lower() for i in meta["issues"])  # sweep never adds issues
