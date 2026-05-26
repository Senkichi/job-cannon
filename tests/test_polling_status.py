"""Unit tests for ``render_polling_status`` + ``PollingSessionConfig``.

The helper is the shared body for HTMX polling routes (sync_status,
batch_score_status). Tests use a stub Flask app with two trivial templates so
the helper can render and exercise the full branch matrix without dragging in
either real blueprint's template/context shape.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask

from job_finder.json_utils import utc_now_iso
from job_finder.web.db_helpers import (
    PollingSessionConfig,
    render_polling_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE batch_score_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_type TEXT NOT NULL,
            status TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            scored INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT NULL,
            error_msg TEXT DEFAULT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def app(tmp_path):
    """Tiny Flask app with two stub templates in a temp template dir.

    Lets us exercise ``render_polling_status`` end-to-end (real
    ``render_template`` + ``make_response``) without depending on the real
    sync/batch templates' DOM shape.
    """
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "progress.html").write_text(
        "PROGRESS sid={{ session_id }} total={{ total }} scored={{ scored }}"
    )
    (templates_dir / "done.html").write_text(
        "DONE status={{ status }} error_msg={{ error_msg or '' }} "
        "scored={{ scored }} skipped={{ skipped }}"
    )
    application = Flask(__name__, template_folder=str(templates_dir))
    return application


def _insert(db_path: str, status: str, started_at: str, **extras) -> int:
    cols = {"session_type": "scoring", "status": status, "started_at": started_at}
    cols.update(extras)
    keys = ", ".join(cols.keys())
    placeholders = ", ".join("?" for _ in cols)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        f"INSERT INTO batch_score_sessions ({keys}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    assert rowid is not None
    return rowid


def _cfg(hx_trigger: dict | None = None, timeout_minutes: int = 30) -> PollingSessionConfig:
    def done_ctx(row, status, error_msg):
        return {
            "status": status,
            "error_msg": error_msg,
            "scored": row["scored"],
            "skipped": row["skipped"],
        }

    def progress_ctx(row):
        return {
            "session_id": row["id"],
            "total": row["total"],
            "scored": row["scored"],
        }

    return PollingSessionConfig(
        progress_template="progress.html",
        done_template="done.html",
        progress_ctx=progress_ctx,
        done_ctx=done_ctx,
        not_found_ctx={
            "status": "error",
            "error_msg": "Session not found",
            "scored": 0,
            "skipped": 0,
        },
        hx_trigger_after_settle=hx_trigger,
        timeout_minutes=timeout_minutes,
        session_label="Test",
    )


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


class TestRenderPollingStatus:
    def test_session_not_found_renders_done_with_not_found_ctx(self, app, db_path):
        with app.test_request_context():
            out = render_polling_status(db_path, 99999, _cfg())
        # render_template returns the rendered string when no trigger.
        assert "DONE status=error" in out
        assert "error_msg=Session not found" in out

    def test_session_not_found_does_not_attach_hx_trigger(self, app, db_path):
        """The 'not found' path returns a plain string even when
        hx_trigger_after_settle is set — matches batch_scoring.py legacy shape."""
        cfg = _cfg(hx_trigger={"dashboard-refresh": None})
        with app.test_request_context():
            out = render_polling_status(db_path, 99999, cfg)
        # Plain string, not a Response — no headers attribute.
        assert isinstance(out, str)

    def test_terminal_state_done_renders_done_template(self, app, db_path):
        sid = _insert(db_path, "done", utc_now_iso(), total=5, scored=4, skipped=1)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        assert "DONE status=done" in out
        assert "scored=4" in out and "skipped=1" in out

    def test_terminal_state_error_passes_error_msg(self, app, db_path):
        sid = _insert(
            db_path,
            "error",
            utc_now_iso(),
            total=5,
            scored=4,
            skipped=1,
            error_msg="boom",
        )
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        assert "DONE status=error" in out
        assert "error_msg=boom" in out

    def test_terminal_state_cancelled_passes_no_error_msg(self, app, db_path):
        """status='cancelled' is terminal but error_msg should be None even if set."""
        sid = _insert(
            db_path,
            "cancelled",
            utc_now_iso(),
            total=5,
            scored=2,
            skipped=0,
            error_msg="should-be-ignored",
        )
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        # error_msg only flows through when status == 'error'.
        assert "DONE status=cancelled" in out
        assert "error_msg=" in out and "error_msg=should-be-ignored" not in out

    def test_running_renders_progress_template(self, app, db_path):
        sid = _insert(db_path, "running", utc_now_iso(), total=10, scored=2)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        assert "PROGRESS" in out
        assert "total=10" in out and "scored=2" in out

    def test_timeout_flips_to_error_and_returns_done(self, app, db_path):
        """A row started >timeout_minutes ago is auto-marked error."""
        old = (datetime.now(UTC) - timedelta(minutes=45)).replace(tzinfo=None).isoformat()
        sid = _insert(db_path, "running", old, total=3, scored=1)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg(timeout_minutes=30))
        assert "DONE status=error" in out
        # ``>`` is HTML-escaped by Jinja autoescape in the rendered template.
        # The DB row (asserted below) holds the unescaped form.
        assert "Session timed out (&gt;30 min)" in out

        # DB row was actually flipped, with the unescaped timeout message.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error_msg FROM batch_score_sessions WHERE id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row["status"] == "error"
        assert row["error_msg"] == "Session timed out (>30 min)"

    def test_timeout_respects_custom_timeout_minutes(self, app, db_path):
        """timeout_minutes=10 fires at 15 minutes elapsed; 30-default-aware tests stay clean."""
        old = (datetime.now(UTC) - timedelta(minutes=15)).replace(tzinfo=None).isoformat()
        sid = _insert(db_path, "running", old, total=3, scored=1)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg(timeout_minutes=10))
        assert "DONE status=error" in out
        assert "Session timed out (&gt;10 min)" in out

    def test_running_under_timeout_renders_progress(self, app, db_path):
        """Started 5 minutes ago — well under the 30-min timeout."""
        recent = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        sid = _insert(db_path, "running", recent, total=10, scored=3)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        assert "PROGRESS" in out

    def test_terminal_state_attaches_hx_trigger_when_configured(self, app, db_path):
        sid = _insert(db_path, "done", utc_now_iso(), scored=4, skipped=1)
        cfg = _cfg(hx_trigger={"dashboard-refresh": None, "jobs-updated": None})
        with app.test_request_context():
            resp = render_polling_status(db_path, sid, cfg)
        # When trigger payload is set, helper wraps in make_response → has headers.
        assert hasattr(resp, "headers")
        trigger = resp.headers.get("HX-Trigger-After-Settle")
        assert trigger is not None
        parsed = json.loads(trigger)
        assert "dashboard-refresh" in parsed and "jobs-updated" in parsed

    def test_terminal_state_no_hx_trigger_when_unconfigured(self, app, db_path):
        """When hx_trigger_after_settle=None, helper returns the rendered string directly."""
        sid = _insert(db_path, "done", utc_now_iso(), scored=4, skipped=1)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        # Plain string return — Flask will materialize Response with no extra headers.
        assert isinstance(out, str)

    def test_timeout_attaches_hx_trigger_when_configured(self, app, db_path):
        old = (datetime.now(UTC) - timedelta(minutes=45)).replace(tzinfo=None).isoformat()
        sid = _insert(db_path, "running", old, total=3, scored=1)
        cfg = _cfg(
            hx_trigger={"dashboard-refresh": None, "jobs-updated": None},
            timeout_minutes=30,
        )
        with app.test_request_context():
            resp = render_polling_status(db_path, sid, cfg)
        assert hasattr(resp, "headers")
        assert resp.headers.get("HX-Trigger-After-Settle")

    def test_malformed_started_at_skips_timeout_and_falls_through(self, app, db_path):
        """A non-ISO started_at logs at debug and continues to status-branch."""
        sid = _insert(db_path, "running", "garbage-not-a-date", total=10, scored=2)
        with app.test_request_context():
            out = render_polling_status(db_path, sid, _cfg())
        # Status is still 'running' → progress fragment (timeout branch silently dropped).
        assert "PROGRESS" in out
