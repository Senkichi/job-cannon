"""Tests for Migration 94 — clear posted_date values that postdate first_seen."""

from __future__ import annotations

import sqlite3

from job_finder.web.migrations.m094_clear_impossible_posted_dates import MIGRATION


def _run(conn: sqlite3.Connection) -> None:
    for stmt in MIGRATION.sql:  # type: ignore[union-attr]
        conn.execute(stmt)


def _make_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE jobs (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dedup_key TEXT UNIQUE, first_seen TEXT, posted_date TEXT)"
    )


def test_clears_posted_date_after_first_seen():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    # The audit's worst real case: first seen Sept 2025, "posted" June 2026.
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2025-09-09T18:10:37", "2026-06-01T14:16:19"),
    )
    _run(conn)
    assert conn.execute("SELECT posted_date FROM jobs WHERE dedup_key='j1'").fetchone()[0] is None


def test_keeps_posted_date_before_first_seen():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2026-06-10T08:00:00", "2026-06-01T14:16:19"),
    )
    _run(conn)
    assert (
        conn.execute("SELECT posted_date FROM jobs WHERE dedup_key='j1'").fetchone()[0]
        == "2026-06-01T14:16:19"
    )


def test_same_day_skew_within_tolerance_is_kept():
    """posted_date a few hours after first_seen (clock skew) is legitimate."""
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2026-06-10T08:00:00", "2026-06-10T20:00:00"),
    )
    _run(conn)
    assert (
        conn.execute("SELECT posted_date FROM jobs WHERE dedup_key='j1'").fetchone()[0]
        == "2026-06-10T20:00:00"
    )


def test_null_posted_date_untouched_and_idempotent():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, NULL)",
        ("j1", "2026-06-10T08:00:00"),
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j2", "2025-09-09T18:10:37", "2026-06-01T14:16:19"),
    )
    _run(conn)
    first = conn.execute("SELECT dedup_key, posted_date FROM jobs ORDER BY dedup_key").fetchall()
    _run(conn)
    second = conn.execute("SELECT dedup_key, posted_date FROM jobs ORDER BY dedup_key").fetchall()
    assert first == second == [("j1", None), ("j2", None)]


def test_first_seen_never_modified():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, posted_date) VALUES (?, ?, ?)",
        ("j1", "2025-09-09T18:10:37", "2026-06-01T14:16:19"),
    )
    _run(conn)
    assert (
        conn.execute("SELECT first_seen FROM jobs WHERE dedup_key='j1'").fetchone()[0]
        == "2025-09-09T18:10:37"
    )
