"""Tests for Migration 93 + the upsert naive-UTC boundary (#361).

m077 normalized once but the write path kept serializing tz-aware
datetimes. Covers:
  - m093 strips tz suffixes from jobs.posted_date / first_seen (lossless).
  - m093 leaves naive rows untouched (no heuristic shift); idempotent.
  - to_naive_utc_iso: aware → naive UTC; naive passes through.
  - upsert_job: a tz-aware ParsedJob.posted_date is stored naive UTC,
    including the INSERT branch's first_seen seeding.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from job_finder.json_utils import to_naive_utc_iso
from job_finder.web.migrations.m093_renormalize_date_columns_to_utc import (
    MIGRATION,
)
from job_finder.web.migrations.types import MigrationContext

# ---------------------------------------------------------------------------
# to_naive_utc_iso (the boundary helper)
# ---------------------------------------------------------------------------


def test_helper_converts_aware_to_naive_utc():
    et = timezone(timedelta(hours=-4))
    dt = datetime(2026, 6, 9, 15, 35, 43, tzinfo=et)
    assert to_naive_utc_iso(dt) == "2026-06-09T19:35:43"


def test_helper_strips_utc_suffix():
    dt = datetime(2026, 6, 9, 15, 35, 43, tzinfo=UTC)
    assert to_naive_utc_iso(dt) == "2026-06-09T15:35:43"


def test_helper_passes_naive_through_unchanged():
    dt = datetime(2026, 6, 9, 15, 35, 43)
    assert to_naive_utc_iso(dt) == "2026-06-09T15:35:43"


# ---------------------------------------------------------------------------
# m093 migration
# ---------------------------------------------------------------------------


def _make_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE jobs (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dedup_key TEXT UNIQUE, first_seen TEXT, last_seen TEXT, posted_date TEXT)"
    )


def _make_ctx(tmp_path, conn: sqlite3.Connection) -> MigrationContext:
    return MigrationContext(
        conn=conn,
        db_path=str(tmp_path / "test.db"),
        user_data_root=str(tmp_path),
        initial_version=92,
    )


def test_m093_strips_offsets_from_posted_date_and_first_seen(tmp_path):
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2026-03-09T20:59:13+00:00", "2026-06-09T15:35:43-04:00"),
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j2", "2026-03-09T20:59:13", "2026-06-09T15:35:43Z"),
    )
    conn.commit()

    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]

    r1 = conn.execute("SELECT first_seen, posted_date FROM jobs WHERE dedup_key='j1'").fetchone()
    assert r1[0] == "2026-03-09T20:59:13"
    assert r1[1] == "2026-06-09T19:35:43"  # -04:00 → +4h forward

    r2 = conn.execute("SELECT first_seen, posted_date FROM jobs WHERE dedup_key='j2'").fetchone()
    assert r2[0] == "2026-03-09T20:59:13"  # naive untouched
    assert r2[1] == "2026-06-09T15:35:43"  # Z stripped


def test_m093_idempotent_and_noop_on_naive(tmp_path):
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2026-03-09T20:59:13", "2026-06-09T15:35:43-04:00"),
    )
    conn.commit()

    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    first = conn.execute("SELECT first_seen, posted_date FROM jobs").fetchone()
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    second = conn.execute("SELECT first_seen, posted_date FROM jobs").fetchone()
    assert first == second == ("2026-03-09T20:59:13", "2026-06-09T19:35:43")


def test_m093_empty_table_is_noop(tmp_path):
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_m093_skips_unparseable_values(tmp_path):
    """Garbage that happens to match the suffix regex is left untouched."""
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, posted_date) VALUES (?, ?)",
        ("j1", "not-a-date+05:00"),
    )
    conn.commit()
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    assert conn.execute("SELECT posted_date FROM jobs").fetchone()[0] == "not-a-date+05:00"


# ---------------------------------------------------------------------------
# upsert boundary: aware datetimes never reach storage with a suffix
# ---------------------------------------------------------------------------


def test_upsert_stores_aware_posted_date_as_naive_utc(tmp_path):
    from job_finder.db import upsert_job
    from job_finder.models import Job
    from job_finder.parsed_job import ParsedJob
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "upsert.db")
    run_migrations(db_path)

    et = timezone(timedelta(hours=-4))
    job = Job(
        title="Engineer",
        company="Acme",
        location="Austin, TX",
        source="Greenhouse",
        source_url="https://example.com/1",
        posted_date=datetime(2026, 6, 9, 15, 35, 43, tzinfo=et),
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        upsert_job(conn, ParsedJob.from_job(job))
        row = conn.execute("SELECT posted_date, first_seen FROM jobs").fetchone()
        assert row["posted_date"] == "2026-06-09T19:35:43"
        # INSERT branch seeds first_seen from posted_date — must be naive too.
        assert row["first_seen"] == "2026-06-09T19:35:43"
    finally:
        conn.close()
