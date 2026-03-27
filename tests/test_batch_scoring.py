"""Tests for batch scoring background thread optimization.

Verifies BATCH-04 (pre-loop cancellation check) and BATCH-05 (deferred in-memory
counters) per the Phase 23 N+1 batching plan.
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


def _insert_session(conn, status="running", session_type="haiku", total=0):
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
    """Insert a job with haiku_score IS NULL (unscored)."""
    from job_finder.json_utils import utc_now_iso
    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
        "VALUES (?, ?, ?, 'Remote', ?, ?)",
        (dedup_key, title, company, now, now),
    )
    conn.commit()


def _insert_sonnet_eligible_job(conn, dedup_key, title="Engineer", company="Acme", haiku_score=75):
    """Insert a job eligible for Sonnet (haiku_score set, no sonnet_score, jd_full present)."""
    from job_finder.json_utils import utc_now_iso
    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO jobs "
        "(dedup_key, title, company, location, first_seen, last_seen, haiku_score, jd_full) "
        "VALUES (?, ?, ?, 'Remote', ?, ?, ?, ?)",
        (dedup_key, title, company, now, now, haiku_score, "Full job description text"),
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

# score_and_persist_* and load_scoring_profile are imported inside the bg functions
# via `from job_finder.web.scoring_orchestrator import ...`, so patch at source module.
# should_exclude is a top-level import in batch_scoring, so patch there.
_ANTHROPIC_PATCH = "anthropic.Anthropic"
_SCORE_HAIKU_PATCH = "job_finder.web.scoring_orchestrator.score_and_persist_haiku"
_SCORE_SONNET_PATCH = "job_finder.web.scoring_orchestrator.score_and_persist_sonnet"
_LOAD_PROFILE_PATCH = "job_finder.web.scoring_orchestrator.load_scoring_profile"
_SHOULD_EXCLUDE_PATCH = "job_finder.web.blueprints.batch_scoring.should_exclude"


# ---------------------------------------------------------------------------
# BATCH-04: Pre-loop cancellation check
# ---------------------------------------------------------------------------

class TestCancellationPreLoop:
    """BATCH-04: cancellation check fires once BEFORE the job loop."""

    def test_cancellation_check_once_haiku(self):
        """Haiku bg: status='cancelling' → immediate return, zero jobs scored."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = _make_db()
        try:
            # Insert 2 unscored jobs
            _insert_unscored_job(conn, "job-cancel-h-1")
            _insert_unscored_job(conn, "job-cancel-h-2")
            # Session already set to 'cancelling' before the bg thread runs
            session_id = _insert_session(conn, status="cancelling", session_type="haiku", total=2)

            score_mock = MagicMock(return_value=MagicMock())

            with patch(_ANTHROPIC_PATCH, return_value=MagicMock()), \
                 patch(_SCORE_HAIKU_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")):
                _run_batch_haiku_bg(path, session_id, _MOCK_CONFIG)

            # Must NOT have scored any jobs
            assert score_mock.call_count == 0, (
                f"score_and_persist_haiku called {score_mock.call_count} times; "
                "expected 0 (cancellation before loop)"
            )

            # Session must be 'cancelled'
            session = _get_session(conn, session_id)
            assert session["status"] == "cancelled"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_cancellation_check_once_sonnet(self):
        """Sonnet bg: status='cancelling' → immediate return, zero jobs evaluated."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_sonnet_bg

        path, conn = _make_db()
        try:
            _insert_sonnet_eligible_job(conn, "job-cancel-s-1")
            _insert_sonnet_eligible_job(conn, "job-cancel-s-2")
            session_id = _insert_session(conn, status="cancelling", session_type="sonnet", total=2)

            score_mock = MagicMock(return_value=MagicMock())

            with patch(_ANTHROPIC_PATCH, return_value=MagicMock()), \
                 patch(_SCORE_SONNET_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}):
                _run_batch_sonnet_bg(path, session_id, _MOCK_CONFIG)

            assert score_mock.call_count == 0, (
                f"score_and_persist_sonnet called {score_mock.call_count} times; "
                "expected 0 (cancellation before loop)"
            )

            session = _get_session(conn, session_id)
            assert session["status"] == "cancelled"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_no_cancellation_processes_all_haiku(self):
        """Haiku bg: status='running' → all 3 jobs are processed, session='done'."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-run-h-1")
            _insert_unscored_job(conn, "job-run-h-2")
            _insert_unscored_job(conn, "job-run-h-3")
            session_id = _insert_session(conn, status="running", session_type="haiku", total=3)

            score_mock = MagicMock(return_value=MagicMock())  # non-None → scored

            with patch(_ANTHROPIC_PATCH, return_value=MagicMock()), \
                 patch(_SCORE_HAIKU_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")), \
                 patch("job_finder.web.activity_tracker.log_activity"):
                _run_batch_haiku_bg(path, session_id, _MOCK_CONFIG)

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
    """BATCH-05: counters accumulated in memory and flushed once before _finish_session."""

    def test_counter_deferred_haiku(self):
        """Haiku bg: 2 scored + 1 None → session row has scored=2, skipped=1 at end."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-ctr-h-1")
            _insert_unscored_job(conn, "job-ctr-h-2")
            _insert_unscored_job(conn, "job-ctr-h-3")
            session_id = _insert_session(conn, status="running", session_type="haiku", total=3)

            # score_and_persist_haiku: return non-None, non-None, None (last job skipped)
            side_effects = [MagicMock(), MagicMock(), None]
            score_mock = MagicMock(side_effect=side_effects)

            with patch(_ANTHROPIC_PATCH, return_value=MagicMock()), \
                 patch(_SCORE_HAIKU_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")), \
                 patch("job_finder.web.activity_tracker.log_activity"):
                _run_batch_haiku_bg(path, session_id, _MOCK_CONFIG)

            session = _get_session(conn, session_id)
            assert session["status"] == "done"
            assert session["scored"] == 2, f"Expected scored=2, got {session['scored']}"
            assert session["skipped"] == 1, f"Expected skipped=1, got {session['skipped']}"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_counter_deferred_sonnet(self):
        """Sonnet bg: 2 scored + 1 None → session row has scored=2, skipped=1 at end."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_sonnet_bg

        path, conn = _make_db()
        try:
            _insert_sonnet_eligible_job(conn, "job-ctr-s-1")
            _insert_sonnet_eligible_job(conn, "job-ctr-s-2")
            _insert_sonnet_eligible_job(conn, "job-ctr-s-3")
            session_id = _insert_session(conn, status="running", session_type="sonnet", total=3)

            side_effects = [MagicMock(), MagicMock(), None]
            score_mock = MagicMock(side_effect=side_effects)

            with patch(_ANTHROPIC_PATCH, return_value=MagicMock()), \
                 patch(_SCORE_SONNET_PATCH, score_mock), \
                 patch(_LOAD_PROFILE_PATCH, return_value={}), \
                 patch("job_finder.web.activity_tracker.log_activity"):
                _run_batch_sonnet_bg(path, session_id, _MOCK_CONFIG)

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
