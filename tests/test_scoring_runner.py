"""Tests for batch-prefetch behavior in run_haiku_scoring and run_sonnet_evaluation.

Verifies that both functions issue exactly one SELECT with WHERE dedup_key IN (...)
instead of one per job key (N+1 query elimination).
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now().isoformat()

def _insert_job(conn: sqlite3.Connection, dedup_key: str, jd_full: str | None = None) -> None:
    """Insert a minimal job row for scoring tests."""
    conn.execute(
        """
        INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, first_seen, last_seen, score, score_breakdown,
             user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            "Senior Data Scientist",
            "Acme Corp",
            "Remote",
            '["linkedin"]',
            '["https://example.com/job/1"]',
            "job-001",
            _NOW,
            _NOW,
            0.0,
            "{}",
            "unreviewed",
            jd_full,
        ),
    )
    conn.commit()

class _TrackingConnection:
    """Wraps a sqlite3.Connection to count SQL queries matching a pattern."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.dedup_select_calls: list[str] = []

    def execute(self, sql: str, *args, **kwargs):
        # Track only batch IN queries (N+1 detection).
        # Per-job single-row reads (e.g. liveness gate source_urls re-read) use
        # "WHERE dedup_key = ?" and are intentionally excluded from this count.
        if "FROM jobs WHERE dedup_key IN" in sql:
            self.dedup_select_calls.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

def _make_tracking_connection_factory(calls_out: list[str]):
    """Return a standalone_connection replacement that tracks dedup SELECT calls.

    Appends each matching SQL string to calls_out.
    """
    from job_finder.web.db_helpers import standalone_connection as real_sc

    @contextmanager
    def _factory(db_path_arg):
        with real_sc(db_path_arg) as conn:
            tracker = _TrackingConnection(conn)
            yield tracker
            calls_out.extend(tracker.dedup_select_calls)

    return _factory

# ---------------------------------------------------------------------------
# Config shared across tests
# ---------------------------------------------------------------------------

_TEST_CONFIG = {
    "scoring": {"haiku_threshold": 5},
    "profile": {
        "target_titles": ["Data Scientist"],
        "target_locations": ["Remote"],
        "min_salary": 100000,
        "exclusions": {"title_keywords": [], "companies": []},
        "skills": [],
    },
    "sources": {},
}

# ---------------------------------------------------------------------------
# run_haiku_scoring batch fetch tests
# ---------------------------------------------------------------------------

def test_haiku_batch_fetch(migrated_db):
    """run_haiku_scoring issues exactly 1 SELECT WHERE dedup_key IN for 3 keys."""
    db_path, setup_conn = migrated_db
    keys = ["key-a", "key-b", "key-c"]
    for k in keys:
        _insert_job(setup_conn, k)
    setup_conn.commit()

    select_calls: list[str] = []

    import job_finder.web.scoring_runner as sr

    with (
        patch.object(sr, "standalone_connection", _make_tracking_connection_factory(select_calls)),
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", return_value={"score": 7}),
        patch.object(sr, "enrich_job", None),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
    ):
        sr.run_haiku_scoring(keys, _TEST_CONFIG, db_path)

    # Should be exactly 1 batch IN query, not 3 individual queries
    assert len(select_calls) == 1, (
        f"Expected 1 batch SELECT but got {len(select_calls)}: {select_calls}"
    )
    assert "IN" in select_calls[0], (
        f"Expected WHERE dedup_key IN (...) but got: {select_calls[0]}"
    )

def test_haiku_missing_key_skipped(migrated_db, caplog):
    """run_haiku_scoring logs a warning for a missing key and processes only existing ones."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "key-exists")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist(conn, job_row, config, profile, scorer_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 7}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", side_effect=mock_persist),
        patch.object(sr, "enrich_job", None),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
        caplog.at_level(logging.WARNING, logger="job_finder.web.scoring_runner"),
    ):
        sr.run_haiku_scoring(["key-exists", "key-missing"], _TEST_CONFIG, db_path)

    assert "key-exists" in scored_keys
    assert "key-missing" not in scored_keys
    assert any("not found in DB" in r.message for r in caplog.records), (
        f"Expected 'not found in DB' warning. Got: {[r.message for r in caplog.records]}"
    )

def test_haiku_empty_keys(migrated_db):
    """run_haiku_scoring returns ([], 0) immediately for empty key list without DB queries."""
    db_path, _ = migrated_db

    import job_finder.web.scoring_runner as sr

    db_touched: list[bool] = []

    @contextmanager
    def tracking_connection(db_path_arg):
        db_touched.append(True)
        from job_finder.web.db_helpers import standalone_connection as real_sc
        with real_sc(db_path_arg) as conn:
            yield conn

    with patch.object(sr, "standalone_connection", tracking_connection):
        result = sr.run_haiku_scoring([], _TEST_CONFIG, db_path)

    assert result == ([], 0)
    assert not db_touched, "DB should not be accessed for empty key list"

# ---------------------------------------------------------------------------
# run_sonnet_evaluation batch fetch tests
# ---------------------------------------------------------------------------

def test_sonnet_batch_fetch(migrated_db):
    """run_sonnet_evaluation issues exactly 1 SELECT WHERE dedup_key IN for 3 keys."""
    db_path, setup_conn = migrated_db
    keys = ["skey-a", "skey-b", "skey-c"]
    for k in keys:
        _insert_job(setup_conn, k, jd_full="Full job description text for Sonnet evaluation.")
    setup_conn.commit()

    select_calls: list[str] = []

    import job_finder.web.scoring_runner as sr

    with (
        patch.object(sr, "standalone_connection", _make_tracking_connection_factory(select_calls)),
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", return_value={"sonnet_score": 85}),
        patch.object(sr, "enrich_company_info", None),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "evaluate_job_sonnet", MagicMock()),
    ):
        sr.run_sonnet_evaluation(keys, _TEST_CONFIG, db_path)

    assert len(select_calls) == 1, (
        f"Expected 1 batch SELECT but got {len(select_calls)}: {select_calls}"
    )
    assert "IN" in select_calls[0], (
        f"Expected WHERE dedup_key IN (...) but got: {select_calls[0]}"
    )

def test_sonnet_missing_key_skipped(migrated_db, caplog):
    """run_sonnet_evaluation logs a warning for a missing key and evaluates only existing ones."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "skey-exists", jd_full="Full job description for Sonnet.")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    evaluated_keys: list[str] = []

    def mock_persist(conn, job_row, config, profile, evaluator_fn=None):
        evaluated_keys.append(job_row["dedup_key"])
        return {"sonnet_score": 85}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", side_effect=mock_persist),
        patch.object(sr, "enrich_company_info", None),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "evaluate_job_sonnet", MagicMock()),
        caplog.at_level(logging.WARNING, logger="job_finder.web.scoring_runner"),
    ):
        sr.run_sonnet_evaluation(["skey-exists", "skey-missing"], _TEST_CONFIG, db_path)

    assert "skey-exists" in evaluated_keys
    assert "skey-missing" not in evaluated_keys
    assert any("not found in DB" in r.message for r in caplog.records), (
        f"Expected 'not found in DB' warning. Got: {[r.message for r in caplog.records]}"
    )

def test_sonnet_empty_keys(migrated_db):
    """run_sonnet_evaluation returns 0 immediately for empty queue without DB queries."""
    db_path, _ = migrated_db

    import job_finder.web.scoring_runner as sr

    db_touched: list[bool] = []

    @contextmanager
    def tracking_connection(db_path_arg):
        db_touched.append(True)
        from job_finder.web.db_helpers import standalone_connection as real_sc
        with real_sc(db_path_arg) as conn:
            yield conn

    with patch.object(sr, "standalone_connection", tracking_connection):
        result = sr.run_sonnet_evaluation([], _TEST_CONFIG, db_path)

    assert result == 0
    assert not db_touched, "DB should not be accessed for empty queue"


# ---------------------------------------------------------------------------
# CLI availability gate tests (adapted from pre-refactor provider routing tests)
# ---------------------------------------------------------------------------


def test_haiku_scoring_no_cli_returns_zero(migrated_db):
    """Haiku scoring returns ([], 0) when claude CLI is not available."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "no-cli-1")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    with patch.object(sr, "shutil") as mock_shutil:
        mock_shutil.which.return_value = None
        sonnet_queue, haiku_scored = sr.run_haiku_scoring(
            ["no-cli-1"], _TEST_CONFIG, db_path,
        )

    assert sonnet_queue == []
    assert haiku_scored == 0


# ---------------------------------------------------------------------------
# Liveness gate tests (Fix 2: ingestion-time expiry check)
# ---------------------------------------------------------------------------


def test_liveness_gate_expired_archives_job_and_skips_haiku(migrated_db):
    """Liveness gate archives expired jobs before Haiku scoring."""
    import sqlite3
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "expired-job-1")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_haiku(conn, job_row, config, profile, scorer_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 7}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", side_effect=mock_persist_haiku),
        patch.object(sr, "enrich_job", MagicMock(return_value=None)),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="expired"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sonnet_queue, haiku_scored = sr.run_haiku_scoring(
            ["expired-job-1"], _TEST_CONFIG, db_path,
        )

    # Job must not reach Haiku scoring
    assert "expired-job-1" not in scored_keys
    assert haiku_scored == 0

    # Job must be archived in DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("expired-job-1",)
    ).fetchone()
    assert row["pipeline_status"] == "archived"
    conn.close()


def test_liveness_gate_live_proceeds_to_haiku(migrated_db):
    """Liveness gate passes LIVE jobs through to Haiku scoring."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "live-job-1")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_haiku(conn, job_row, config, profile, scorer_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 7}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", side_effect=mock_persist_haiku),
        patch.object(sr, "enrich_job", MagicMock(return_value=None)),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sr.run_haiku_scoring(["live-job-1"], _TEST_CONFIG, db_path)

    assert "live-job-1" in scored_keys


def test_liveness_gate_inconclusive_proceeds_to_haiku(migrated_db):
    """Liveness gate passes INCONCLUSIVE jobs through to Haiku scoring."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "inconclusive-job-1")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_haiku(conn, job_row, config, profile, scorer_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 7}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", side_effect=mock_persist_haiku),
        patch.object(sr, "enrich_job", MagicMock(return_value=None)),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="inconclusive"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sr.run_haiku_scoring(["inconclusive-job-1"], _TEST_CONFIG, db_path)

    assert "inconclusive-job-1" in scored_keys
