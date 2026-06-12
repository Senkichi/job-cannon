"""Tests for Migration 96 — staleness consistency backfills.

Covers:
- computed_status precedence flip: stale+expired rows display 'expired'
  (verified) instead of 'stale' (clock-inferred).
- Live-evidence refresh: expiry_status='live' with a verdict newer than
  last_seen catches last_seen up and clears is_stale.
- Resurrection: system-archived jobs live-verified within the window go
  back to 'discovered' with an audit event; manual archives, stale
  verdicts, and out-of-window verdicts are preserved.
- Frozen 'expired' on active rows is cleared so Phase B/C re-verify.
- is_stale cleared outside the passive stages.
- Idempotent: a second invocation is a no-op.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m096_staleness_consistency import _backfill
from job_finder.web.migrations.types import MigrationContext


def _iso_days_ago(n: int) -> str:
    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=n)).isoformat()


@pytest.fixture
def migrated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.unlink(path)


def _insert_job(
    conn,
    dedup_key,
    pipeline_status="discovered",
    last_seen=None,
    is_stale=0,
    expiry_status=None,
    expiry_checked_at=None,
):
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, sources, source_urls, source_id,
            first_seen, last_seen, pipeline_status, is_stale, expiry_status, expiry_checked_at)
           VALUES (?, 't', 'c', '', '[]', '[]', '', '2026-01-01T00:00:00', ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            last_seen or _iso_days_ago(40),
            pipeline_status,
            is_stale,
            expiry_status,
            expiry_checked_at,
        ),
    )
    conn.commit()


def _insert_archive_event(conn, dedup_key, source, days_ago=5):
    conn.execute(
        """INSERT INTO pipeline_events
           (job_id, from_status, to_status, timestamp, source, evidence)
           VALUES (?, 'discovered', 'archived', ?, ?, 'test')""",
        (dedup_key, _iso_days_ago(days_ago), source),
    )
    conn.commit()


def _run(path, conn):
    _backfill(MigrationContext(conn=conn, db_path=path, user_data_root="."))
    conn.commit()


class TestPrecedenceFlip:
    def test_stale_and_expired_displays_expired(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "k", is_stale=1, expiry_status="expired")
        got = conn.execute("SELECT computed_status FROM jobs WHERE dedup_key='k'").fetchone()[0]
        assert got == "expired"


class TestLiveEvidenceRefresh:
    def test_newer_live_verdict_refreshes_last_seen(self, migrated_db):
        path, conn = migrated_db
        verdict_at = _iso_days_ago(2)
        _insert_job(
            conn,
            "k",
            last_seen=_iso_days_ago(40),
            is_stale=1,
            expiry_status="live",
            expiry_checked_at=verdict_at,
        )
        _run(path, conn)
        row = conn.execute("SELECT last_seen, is_stale FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["last_seen"] == verdict_at
        assert row["is_stale"] == 0

    def test_older_live_verdict_does_not_regress_last_seen(self, migrated_db):
        path, conn = migrated_db
        recent = _iso_days_ago(1)
        _insert_job(
            conn,
            "k",
            last_seen=recent,
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(10),
        )
        _run(path, conn)
        row = conn.execute("SELECT last_seen FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["last_seen"] == recent


class TestResurrection:
    def test_system_archived_live_verified_job_resurrected(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(3),
        )
        _insert_archive_event(conn, "k", "stale_detector")
        _run(path, conn)

        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["pipeline_status"] == "discovered"
        event = conn.execute(
            "SELECT source, evidence, from_status, to_status FROM pipeline_events "
            "WHERE job_id='k' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        assert event["source"] == "m096_backfill"
        assert event["evidence"] == "live_verified_at_archive_time"
        assert (event["from_status"], event["to_status"]) == ("archived", "discovered")

    def test_manual_archive_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(3),
        )
        _insert_archive_event(conn, "k", "manual")
        _run(path, conn)
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["pipeline_status"] == "archived"

    def test_manual_rearchive_after_system_archive_preserved(self, migrated_db):
        """The LATEST archive event decides: a user re-archiving after a
        system archive is a user decision."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(3),
        )
        _insert_archive_event(conn, "k", "stale_detector", days_ago=10)
        _insert_archive_event(conn, "k", "manual", days_ago=2)
        _run(path, conn)
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["pipeline_status"] == "archived"

    def test_out_of_window_verdict_not_resurrected(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(20),
        )
        _insert_archive_event(conn, "k", "stale_detector")
        _run(path, conn)
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["pipeline_status"] == "archived"

    def test_expired_job_not_resurrected(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="expired",
            expiry_checked_at=_iso_days_ago(3),
        )
        _insert_archive_event(conn, "k", "ats_reconciler")
        _run(path, conn)
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["pipeline_status"] == "archived"

    def test_idempotent(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="live",
            expiry_checked_at=_iso_days_ago(3),
        )
        _insert_archive_event(conn, "k", "stale_detector")
        _run(path, conn)
        _run(path, conn)
        n = conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id='k' AND source='m096_backfill'"
        ).fetchone()[0]
        assert n == 1


class TestFrozenExpiredClear:
    def test_active_row_with_expired_verdict_cleared(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="discovered",
            expiry_status="expired",
            expiry_checked_at=_iso_days_ago(10),
        )
        _run(path, conn)
        row = conn.execute(
            "SELECT expiry_status, expiry_checked_at FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert row["expiry_status"] is None
        assert row["expiry_checked_at"] is None

    def test_archived_row_keeps_expired_verdict(self, migrated_db):
        path, conn = migrated_db
        _insert_job(
            conn,
            "k",
            pipeline_status="archived",
            expiry_status="expired",
            expiry_checked_at=_iso_days_ago(10),
        )
        _run(path, conn)
        row = conn.execute("SELECT expiry_status FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["expiry_status"] == "expired"


class TestNonPassiveStaleClear:
    def test_applied_job_stale_flag_cleared(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "k", pipeline_status="applied", is_stale=1)
        _run(path, conn)
        row = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["is_stale"] == 0

    def test_passive_stale_flag_preserved(self, migrated_db):
        path, conn = migrated_db
        _insert_job(conn, "k", pipeline_status="discovered", is_stale=1)
        _run(path, conn)
        row = conn.execute("SELECT is_stale FROM jobs WHERE dedup_key='k'").fetchone()
        assert row["is_stale"] == 1

    def test_empty_db_noop(self, migrated_db):
        path, conn = migrated_db
        _run(path, conn)  # no rows — must not raise
