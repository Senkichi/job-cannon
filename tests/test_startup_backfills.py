"""Tests for startup_backfills.py.

Covers the two public functions:
- run_description_reformat_once: TESTING guard, missing API key, thread launch
- run_data_backfills_once: TESTING guard, sentinel idempotency, locations_raw backfill,
  posted_date backfill, SerpAPI enrichment path, and sentinel insertion

All threading is tested synchronously by patching threading.Thread so the
daemon thread runs inline (or is captured as a mock call).

threading is imported lazily (inside each function body), so we patch
`threading.Thread` at the threading module level rather than at the
startup_backfills module level.
"""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.startup_backfills import (
    run_data_backfills_once,
    run_description_reformat_once,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_job(conn: sqlite3.Connection, dedup_key: str, **kwargs) -> None:
    """Insert a minimal job row. Keyword args override defaults."""
    defaults = {
        "title": "Data Scientist",
        "company": "Acme",
        "location": "Remote",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-03-01T00:00:00",
    }
    defaults.update(kwargs)
    cols = ", ".join(["dedup_key"] + list(defaults.keys()))
    placeholders = ", ".join(["?"] * (1 + len(defaults)))
    conn.execute(
        f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({placeholders})",
        [dedup_key] + list(defaults.values()),
    )
    conn.commit()


def _run_backfill_synchronously(db_path: str, config: dict) -> None:
    """Run run_data_backfills_once with threading.Thread replaced by a synchronous runner.

    Patches threading.Thread at the module level so the lazy import inside
    run_data_backfills_once picks up the patched class.
    """
    captured: list = []

    class _SyncThread:
        def __init__(self, target, daemon=False):
            captured.append(target)
            self.daemon = daemon

        def start(self):
            captured[0]()

    with patch("threading.Thread", _SyncThread):
        run_data_backfills_once(db_path, config)


# ---------------------------------------------------------------------------
# run_description_reformat_once — TESTING guard
# ---------------------------------------------------------------------------


def test_description_reformat_skipped_in_testing():
    """run_description_reformat_once returns immediately when TESTING=True."""
    started: list = []

    class _SyncThread:
        def __init__(self, target, daemon=False):
            pass

        def start(self):
            started.append(True)

    with patch("threading.Thread", _SyncThread):
        run_description_reformat_once("/fake/path.db", {"TESTING": True})

    assert started == [], "No thread should start in TESTING mode"


# ---------------------------------------------------------------------------
# run_description_reformat_once — missing API key
# ---------------------------------------------------------------------------


def test_description_reformat_skipped_without_anthropic():
    """run_description_reformat_once does not spawn a thread when anthropic is not installed."""
    started: list = []

    class _SyncThread:
        def __init__(self, target, daemon=False):
            pass

        def start(self):
            started.append(True)

    # Simulate anthropic not being importable
    with patch("threading.Thread", _SyncThread), \
         patch.dict("sys.modules", {"anthropic": None}):
        run_description_reformat_once("/fake/path.db", {})

    assert started == [], "No thread should start without anthropic installed"


# ---------------------------------------------------------------------------
# run_description_reformat_once — thread launched (key via telemetry)
# ---------------------------------------------------------------------------


def test_description_reformat_spawns_thread():
    """run_description_reformat_once spawns a daemon thread (key from telemetry)."""
    started: list = []
    daemon_flags: list = []

    class _SyncThread:
        def __init__(self, target, daemon=False):
            daemon_flags.append(daemon)

        def start(self):
            # Don't actually run: we just want to confirm start() is called
            started.append(True)

    with patch("threading.Thread", _SyncThread):
        run_description_reformat_once("/fake/path.db", {})

    assert started == [True], "Thread should be started"
    assert daemon_flags == [True], "Thread should be daemon=True"


# ---------------------------------------------------------------------------
# run_data_backfills_once — TESTING guard
# ---------------------------------------------------------------------------


def test_data_backfills_skipped_in_testing():
    """run_data_backfills_once returns immediately when TESTING=True."""
    started: list = []

    class _SyncThread:
        def __init__(self, target, daemon=False):
            pass

        def start(self):
            started.append(True)

    with patch("threading.Thread", _SyncThread):
        run_data_backfills_once("/fake/path.db", {"TESTING": True})

    assert started == [], "No thread should start in TESTING mode"


# ---------------------------------------------------------------------------
# run_data_backfills_once — sentinel idempotency
# ---------------------------------------------------------------------------


def test_data_backfills_sentinel_prevents_rerun(migrated_db):
    """When the backfill_v1 sentinel already exists, no UPDATE statements are issued."""
    path, conn = migrated_db

    # Pre-insert the sentinel
    conn.execute(
        "INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at) "
        "VALUES ('__sentinel__', '__sentinel__', 'backfill_v1', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    # Insert a job with NULL locations_raw so we can detect if an UPDATE runs
    direct_conn = sqlite3.connect(path)
    _insert_job(direct_conn, "canary", location="Boston")
    direct_conn.execute("UPDATE jobs SET locations_raw = NULL WHERE dedup_key = 'canary'")
    direct_conn.commit()
    direct_conn.close()

    _run_backfill_synchronously(path, {})

    # locations_raw should still be NULL — no backfill ran
    verify = sqlite3.connect(path)
    row = verify.execute("SELECT locations_raw FROM jobs WHERE dedup_key = 'canary'").fetchone()
    verify.close()
    assert row[0] is None, "locations_raw should remain NULL when sentinel prevents re-run"


# ---------------------------------------------------------------------------
# run_data_backfills_once — locations_raw backfill
# ---------------------------------------------------------------------------


def test_data_backfills_populates_locations_raw(migrated_db):
    """run_data_backfills_once copies location into locations_raw for NULL rows."""
    path, conn = migrated_db

    _insert_job(conn, "needs-backfill", location="San Francisco, CA")
    conn.execute("UPDATE jobs SET locations_raw = NULL WHERE dedup_key = 'needs-backfill'")
    _insert_job(conn, "already-filled", location="Remote")
    conn.execute("UPDATE jobs SET locations_raw = '[\"Remote\"]' WHERE dedup_key = 'already-filled'")
    conn.commit()
    conn.close()

    _run_backfill_synchronously(path, {})

    verify = sqlite3.connect(path)
    row = verify.execute(
        "SELECT locations_raw FROM jobs WHERE dedup_key = 'needs-backfill'"
    ).fetchone()
    row2 = verify.execute(
        "SELECT locations_raw FROM jobs WHERE dedup_key = 'already-filled'"
    ).fetchone()
    verify.close()

    assert row[0] is not None, "locations_raw should be populated after backfill"
    assert "San Francisco" in row[0]
    assert row2[0] == '["Remote"]', "Pre-filled row should not be overwritten"


# ---------------------------------------------------------------------------
# run_data_backfills_once — posted_date backfill
# ---------------------------------------------------------------------------


def test_data_backfills_populates_posted_date(migrated_db):
    """run_data_backfills_once copies first_seen into posted_date for NULL rows."""
    path, conn = migrated_db

    _insert_job(conn, "no-posted-date", first_seen="2026-02-01T00:00:00")
    conn.execute("UPDATE jobs SET posted_date = NULL WHERE dedup_key = 'no-posted-date'")
    conn.commit()
    conn.close()

    _run_backfill_synchronously(path, {})

    verify = sqlite3.connect(path)
    row = verify.execute(
        "SELECT posted_date FROM jobs WHERE dedup_key = 'no-posted-date'"
    ).fetchone()
    verify.close()

    assert row[0] == "2026-02-01T00:00:00", "posted_date should be set from first_seen"


# ---------------------------------------------------------------------------
# run_data_backfills_once — sentinel is inserted after completion
# ---------------------------------------------------------------------------


def test_data_backfills_inserts_sentinel(migrated_db):
    """run_data_backfills_once inserts backfill_v1 sentinel in merge_log on first run."""
    path, conn = migrated_db
    conn.close()

    _run_backfill_synchronously(path, {})

    verify = sqlite3.connect(path)
    row = verify.execute(
        "SELECT id FROM merge_log WHERE merge_source = 'backfill_v1' LIMIT 1"
    ).fetchone()
    verify.close()

    assert row is not None, "backfill_v1 sentinel should exist after first run"


# ---------------------------------------------------------------------------
# run_data_backfills_once — idempotency: second run does not duplicate sentinel
# ---------------------------------------------------------------------------


def test_data_backfills_idempotent(migrated_db):
    """Running run_data_backfills_once twice leaves exactly one sentinel row."""
    path, conn = migrated_db
    conn.close()

    _run_backfill_synchronously(path, {})
    _run_backfill_synchronously(path, {})

    verify = sqlite3.connect(path)
    count = verify.execute(
        "SELECT COUNT(*) FROM merge_log WHERE merge_source = 'backfill_v1'"
    ).fetchone()[0]
    verify.close()

    assert count == 1, f"Expected exactly 1 sentinel but found {count}"


# ---------------------------------------------------------------------------
# run_data_backfills_once — SerpAPI enrichment skipped without key
# ---------------------------------------------------------------------------


def test_data_backfills_skips_serpapi_enrichment_without_key(migrated_db):
    """run_data_backfills_once skips SerpAPI enrichment when no serpapi api_key is configured."""
    path, conn = migrated_db
    conn.close()

    with patch("job_finder.web.data_enricher.run_enrichment_backfill") as mock_enrich:
        _run_backfill_synchronously(path, {"sources": {}})

    mock_enrich.assert_not_called()


# ---------------------------------------------------------------------------
# run_data_backfills_once — SerpAPI enrichment called when key present
# ---------------------------------------------------------------------------


def test_data_backfills_calls_serpapi_enrichment_when_key_present(migrated_db):
    """run_data_backfills_once calls run_enrichment_backfill when serpapi api_key is set."""
    path, conn = migrated_db
    conn.close()

    config = {"sources": {"serpapi": {"api_key": "test-serpapi-key"}}}

    # Patch at the import point inside startup_backfills._run()
    # The function does: from job_finder.web.data_enricher import run_enrichment_backfill
    with patch("job_finder.web.data_enricher.run_enrichment_backfill", return_value=0) as mock_enrich:
        _run_backfill_synchronously(path, config)

    mock_enrich.assert_called_once()
    call_kwargs = mock_enrich.call_args
    # First positional arg should be the db_path
    assert call_kwargs[0][0] == path
    # Second positional arg should be the serpapi key
    assert call_kwargs[0][1] == "test-serpapi-key"
