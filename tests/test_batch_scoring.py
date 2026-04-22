"""Tests for the unified v3.0 batch scoring background thread (Phase 34 Plan 3 Commit B).

Verifies BATCH-04 (pre-loop cancellation check) and BATCH-05 (deferred in-memory
counters) after the Haiku/Sonnet merge. The pre-v3 test file had parallel
"haiku" and "sonnet" copies of each test case; this file collapses them into a
single "scoring" test since `_run_batch_bg` now drives the whole pipeline.
"""

import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    """Create a temp DB with all migrations applied. Returns (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn

def _insert_session(conn, status="running", session_type="scoring", total=0):
    """Insert a batch_score_sessions row and return its id."""
    from job_finder.json_utils import utc_now_iso
    conn.execute(
        "INSERT INTO batch_score_sessions (session_type, status, total, scored, skipped, started_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (session_type, status, total, utc_now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def _insert_unscored_job(conn, dedup_key, title="Engineer", company="Acme"):
    """Insert a job with classification IS NULL (unscored by v3 pipeline)."""
    from job_finder.json_utils import utc_now_iso
    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
        "VALUES (?, ?, ?, 'Remote', ?, ?)",
        (dedup_key, title, company, now, now),
    )
    conn.commit()

def _get_session(conn, session_id):
    """Fetch a session row by id."""
    return conn.execute(
        "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
    ).fetchone()

# ---------------------------------------------------------------------------
# Test fixtures / common patches
# ---------------------------------------------------------------------------

_MOCK_CONFIG = {}

# score_and_persist_job and load_scoring_profile are imported inside the bg
# function via `from job_finder.web.scoring_orchestrator import ...`, so patch
# at the source module.
# should_exclude is a top-level import in batch_scoring, so patch there.
_SCORE_JOB_PATCH = "job_finder.web.scoring_orchestrator.score_and_persist_job"
_LOAD_PROFILE_PATCH = "job_finder.web.scoring_orchestrator.load_scoring_profile"
_SHOULD_EXCLUDE_PATCH = "job_finder.web.blueprints.batch_scoring.should_exclude"

# ---------------------------------------------------------------------------
# BATCH-04: Pre-loop cancellation check
# ---------------------------------------------------------------------------

class TestCancellationPreLoop:
    """BATCH-04: cancellation check fires once BEFORE the job loop."""

    def test_cancellation_check_once(self):
        """Unified bg: status='cancelling' → immediate return, zero jobs scored."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            # Insert 2 unscored jobs
            _insert_unscored_job(conn, "job-cancel-1")
            _insert_unscored_job(conn, "job-cancel-2")
            # Session already set to 'cancelling' before the bg thread runs
            session_id = _insert_session(
                conn, status="cancelling", session_type="scoring", total=2,
            )

            score_mock = MagicMock(return_value=MagicMock())

            with patch(_SCORE_JOB_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            # Must NOT have scored any jobs
            assert score_mock.call_count == 0, (
                f"score_and_persist_job called {score_mock.call_count} times; "
                "expected 0 (cancellation before loop)"
            )

            # Session must be 'cancelled'
            session = _get_session(conn, session_id)
            assert session["status"] == "cancelled"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_no_cancellation_processes_all(self):
        """Unified bg: status='running' → all 3 jobs are processed, session='done'."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-run-1")
            _insert_unscored_job(conn, "job-run-2")
            _insert_unscored_job(conn, "job-run-3")
            session_id = _insert_session(
                conn, status="running", session_type="scoring", total=3,
            )

            score_mock = MagicMock(return_value=MagicMock())  # non-None → scored

            with patch(_SCORE_JOB_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")), \
                 patch("job_finder.web.activity_tracker.log_activity"):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            assert score_mock.call_count == 3, (
                f"Expected 3 scoring calls, got {score_mock.call_count}"
            )

            session = _get_session(conn, session_id)
            assert session["status"] == "done"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

# ---------------------------------------------------------------------------
# BATCH-05: Deferred in-memory counters
# ---------------------------------------------------------------------------

class TestDeferredCounters:
    """BATCH-05: counters accumulated in memory, flushed periodically and before _finish_session."""

    def test_counter_deferred(self):
        """Unified bg: 2 scored + 1 None → session row has scored=2, skipped=1 at end."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-ctr-1")
            _insert_unscored_job(conn, "job-ctr-2")
            _insert_unscored_job(conn, "job-ctr-3")
            session_id = _insert_session(
                conn, status="running", session_type="scoring", total=3,
            )

            # score_and_persist_job: return non-None, non-None, None (last job skipped)
            side_effects = [MagicMock(), MagicMock(), None]
            score_mock = MagicMock(side_effect=side_effects)

            with patch(_SCORE_JOB_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")), \
                 patch("job_finder.web.activity_tracker.log_activity"):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            session = _get_session(conn, session_id)
            assert session["status"] == "done"
            assert session["scored"] == 2, f"Expected scored=2, got {session['scored']}"
            assert session["skipped"] == 1, f"Expected skipped=1, got {session['skipped']}"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

# ---------------------------------------------------------------------------
# Dead code removal
# ---------------------------------------------------------------------------

class TestDeadCodeRemoved:
    """_update_session_counter must be removed after BATCH-05 migration."""

    def test_update_session_counter_removed(self):
        """_update_session_counter must NOT exist in batch_scoring module."""
        import job_finder.web.blueprints.batch_scoring as batch_scoring_module
        assert not hasattr(batch_scoring_module, "_update_session_counter"), (
            "_update_session_counter still defined in batch_scoring module; "
            "should have been removed as dead code after BATCH-05 migration"
        )


# ---------------------------------------------------------------------------
# v3.0 Plan 3 Commit B invariants — unified route shape
# ---------------------------------------------------------------------------

class TestUnifiedRouteShape:
    """Plan 3 Commit B: single batch_score_start + _run_batch_bg, session_type='scoring'."""

    def test_batch_score_start_exists(self):
        """The unified batch_score_start route function exists."""
        from job_finder.web.blueprints import batch_scoring as bs
        assert hasattr(bs, "batch_score_start"), (
            "Plan 3 Commit B must define batch_score_start"
        )

    def test_run_batch_bg_exists(self):
        """The unified _run_batch_bg worker function exists."""
        from job_finder.web.blueprints import batch_scoring as bs
        assert hasattr(bs, "_run_batch_bg"), (
            "Plan 3 Commit B must define _run_batch_bg"
        )

    def test_legacy_haiku_sonnet_bg_functions_removed(self):
        """The old _run_batch_haiku_bg / _run_batch_sonnet_bg workers are gone."""
        from job_finder.web.blueprints import batch_scoring as bs
        assert not hasattr(bs, "_run_batch_haiku_bg"), (
            "_run_batch_haiku_bg still defined; Plan 3 Commit B must merge it into _run_batch_bg"
        )
        assert not hasattr(bs, "_run_batch_sonnet_bg"), (
            "_run_batch_sonnet_bg still defined; Plan 3 Commit B must merge it into _run_batch_bg"
        )

    def test_predicate_uses_classification_not_haiku_score(self):
        """The worker SQL filters on `classification IS NULL`, not `haiku_score IS NULL`.

        Checks the compiled function bytecode's string constants rather than the
        source text to avoid false positives from docstring references.
        """
        from job_finder.web.blueprints import batch_scoring as bs
        consts = bs._run_batch_bg.__code__.co_consts
        sql_strings = [c for c in consts if isinstance(c, str) and "jobs" in c.lower()]
        combined = " ".join(sql_strings)
        assert "classification IS NULL" in combined, (
            f"_run_batch_bg SQL must query on `classification IS NULL`. "
            f"Found SQL strings: {sql_strings!r}"
        )
        assert "haiku_score IS NULL" not in combined, (
            f"_run_batch_bg SQL must not use the legacy `haiku_score IS NULL` predicate. "
            f"Found SQL strings: {sql_strings!r}"
        )

    def _build_app(self, db_path):
        """Helper — build a real create_app-backed Flask app with full templates."""
        from job_finder.web import create_app
        app = create_app(config={
            "db": {"path": db_path},
            "scoring": {"daily_budget_usd": 25.0},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        })
        app.config["TESTING"] = True
        return app

    def test_session_type_inserted_is_scoring(self):
        """batch_score_start inserts a session with session_type='scoring'."""
        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-route-1")
            app = self._build_app(path)
            with app.test_client() as client:
                resp = client.post("/dashboard/batch-score/start")
            assert resp.status_code == 200

            session_type = conn.execute(
                "SELECT session_type FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            assert session_type == "scoring", (
                f"Expected session_type='scoring' from unified route; got {session_type!r}"
            )
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_legacy_haiku_route_delegates(self):
        """POST /dashboard/batch-score/haiku/start still works — it delegates to the unified route."""
        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-legacy-1")
            app = self._build_app(path)
            with app.test_client() as client:
                resp = client.post("/dashboard/batch-score/haiku/start")
            assert resp.status_code == 200
            session_type = conn.execute(
                "SELECT session_type FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            # Legacy wrapper still writes the v3 session_type value.
            assert session_type == "scoring"
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)
