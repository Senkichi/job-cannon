"""Tests for Migration 77: stored-timestamp normalization to naive UTC.

Covers:
  - Phase A (tz-aware suffix stripped) across all five target columns.
  - Phase B (naive-local shift) on the two single-source columns only.
  - Idempotency: re-running the migration on already-normalized data is a no-op.
  - Public-release scenario: empty target tables — migration logs zero rows
    and commits without error.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from job_finder.web.migrations.m077_normalize_timestamps_to_utc import (
    _TZ_SUFFIX_RE,
    MIGRATION,
    _compute_local_to_utc_offset_hours,
    _normalize_tz_aware_to_naive_utc,
    _shift_naive_local_to_naive_utc,
)
from job_finder.web.migrations.types import MigrationContext

# ---------------------------------------------------------------------------
# Unit tests for the helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_has_tz",
    [
        ("2026-05-29T15:00:00+00:00", True),
        ("2026-05-29T15:00:00Z", True),
        ("2026-05-29T15:00:00-08:00", True),
        ("2026-05-29T15:00:00+0000", True),  # compact form
        ("2026-05-29T15:00:00", False),
        ("2026-05-29T15:00:00.123456", False),
        ("not an iso string", False),
    ],
)
def test_tz_suffix_detection(value, expected_has_tz):
    assert bool(_TZ_SUFFIX_RE.search(value)) is expected_has_tz


def test_phase_a_strips_utc_offset():
    assert _normalize_tz_aware_to_naive_utc("2026-05-29T15:00:00+00:00") == "2026-05-29T15:00:00"


def test_phase_a_converts_negative_offset_to_utc():
    # -08:00 means local clock = UTC - 8h, so UTC = local + 8h
    assert _normalize_tz_aware_to_naive_utc("2026-05-29T15:00:00-08:00") == "2026-05-29T23:00:00"


def test_phase_a_handles_z_suffix():
    assert _normalize_tz_aware_to_naive_utc("2026-05-29T15:00:00Z") == "2026-05-29T15:00:00"


def test_phase_a_returns_none_for_naive():
    """A naive string has nothing to strip — Phase A must skip it."""
    assert _normalize_tz_aware_to_naive_utc("2026-05-29T15:00:00") is None


def test_phase_b_shifts_naive_local_forward():
    assert _shift_naive_local_to_naive_utc("2026-05-29T08:00:00", 8.0) == "2026-05-29T16:00:00"


def test_phase_b_returns_none_for_aware():
    """Phase B must not double-process a tz-aware string."""
    assert _shift_naive_local_to_naive_utc("2026-05-29T15:00:00+00:00", 8.0) is None


def test_phase_b_handles_negative_offset():
    """A positive UTC offset (e.g. UTC+9 Japan) shifts local backwards to UTC."""
    assert _shift_naive_local_to_naive_utc("2026-05-29T15:00:00", -9.0) == "2026-05-29T06:00:00"


# ---------------------------------------------------------------------------
# Migration integration tests
# ---------------------------------------------------------------------------


def _make_minimal_schema(conn: sqlite3.Connection) -> None:
    """Create the minimal table set the migration touches."""
    conn.executescript(
        """
        CREATE TABLE jobs (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT UNIQUE,
            first_seen TEXT,
            last_seen TEXT,
            posted_date TEXT
        );
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY,
            name TEXT,
            last_scanned_at TEXT
        );
        CREATE TABLE company_scan_log (
            id INTEGER PRIMARY KEY,
            scanned_at TEXT
        );
        """
    )


def _make_ctx(tmp_path, conn: sqlite3.Connection) -> MigrationContext:
    return MigrationContext(
        conn=conn,
        db_path=str(tmp_path / "test.db"),
        user_data_root=str(tmp_path),
        initial_version=76,
    )


def test_migration_phase_a_across_all_columns(tmp_path):
    conn = sqlite3.connect(":memory:")
    _make_minimal_schema(conn)

    # Seed one tz-aware row per target column
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, last_seen, posted_date) VALUES (?, ?, ?, ?)",
        (
            "j1",
            "2026-05-29T15:00:00+00:00",
            "2026-05-29T16:00:00Z",
            "2026-05-29T17:00:00-08:00",
        ),
    )
    conn.execute(
        "INSERT INTO companies (id, name, last_scanned_at) VALUES (?, ?, ?)",
        (1, "Acme", "2026-05-29T15:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO company_scan_log (id, scanned_at) VALUES (?, ?)",
        (1, "2026-05-29T15:00:00Z"),
    )
    conn.commit()

    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]

    row = conn.execute(
        "SELECT first_seen, last_seen, posted_date FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row[0] == "2026-05-29T15:00:00"
    assert row[1] == "2026-05-29T16:00:00"
    assert row[2] == "2026-05-30T01:00:00"  # -08:00 → +8h forward

    # companies.last_scanned_at also gets Phase B applied — but since the
    # input was tz-aware, Phase A fires first and Phase B sees the now-
    # normalized value as naive. To prevent double-shift we use a
    # single-pass-per-row loop in the migration that picks exactly one
    # of Phase A or Phase B based on the *original* value's shape.
    assert (
        conn.execute("SELECT last_scanned_at FROM companies WHERE id=1").fetchone()[0]
        == "2026-05-29T15:00:00"
    )
    assert (
        conn.execute("SELECT scanned_at FROM company_scan_log WHERE id=1").fetchone()[0]
        == "2026-05-29T15:00:00"
    )


def test_migration_phase_b_only_on_designated_columns(tmp_path):
    """Phase B (naive shift) applies to companies.last_scanned_at and
    company_scan_log.scanned_at, NOT to jobs.first_seen/last_seen/posted_date."""
    conn = sqlite3.connect(":memory:")
    _make_minimal_schema(conn)

    # Seed naive rows on every target column with the same value.
    naive = "2026-05-29T08:00:00"
    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen, last_seen, posted_date) VALUES (?, ?, ?, ?)",
        ("j1", naive, naive, naive),
    )
    conn.execute(
        "INSERT INTO companies (id, name, last_scanned_at) VALUES (?, ?, ?)",
        (1, "Acme", naive),
    )
    conn.execute(
        "INSERT INTO company_scan_log (id, scanned_at) VALUES (?, ?)",
        (1, naive),
    )
    conn.commit()

    # Compute the actual offset the migration will use, so we can predict
    # the expected post-Phase-B value without coupling to the test host's
    # specific timezone. Use the same helper the migration uses so DST
    # decisions match exactly.
    offset = _compute_local_to_utc_offset_hours()
    shifted_expected = (datetime.fromisoformat(naive) + timedelta(hours=offset)).isoformat()

    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]

    # Phase B targets shifted
    assert (
        conn.execute("SELECT last_scanned_at FROM companies WHERE id=1").fetchone()[0]
        == shifted_expected
    )
    assert (
        conn.execute("SELECT scanned_at FROM company_scan_log WHERE id=1").fetchone()[0]
        == shifted_expected
    )

    # Non-Phase-B columns kept their naive values (no shift)
    row = conn.execute(
        "SELECT first_seen, last_seen, posted_date FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row[0] == naive
    assert row[1] == naive
    assert row[2] == naive


def test_migration_idempotent_on_already_normalized_data(tmp_path):
    """Running m076 twice in a row leaves naive-UTC rows untouched the second time."""
    conn = sqlite3.connect(":memory:")
    _make_minimal_schema(conn)

    conn.execute(
        "INSERT INTO jobs (dedup_key, first_seen) VALUES (?, ?)",
        ("j1", "2026-05-29T15:00:00+00:00"),
    )
    conn.commit()

    # First run: Phase A converts the tz-aware row
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    after_first = conn.execute("SELECT first_seen FROM jobs WHERE dedup_key='j1'").fetchone()[0]
    assert after_first == "2026-05-29T15:00:00"

    # Second run: Phase A finds no tz-aware row. Because jobs.first_seen is
    # NOT in Phase B, Phase B doesn't shift it either. Stable.
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
    after_second = conn.execute("SELECT first_seen FROM jobs WHERE dedup_key='j1'").fetchone()[0]
    assert after_second == after_first


def test_migration_empty_tables_is_noop(tmp_path):
    """Public-release users with fresh DBs hit this migration on empty tables."""
    conn = sqlite3.connect(":memory:")
    _make_minimal_schema(conn)

    # No rows inserted.
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM company_scan_log").fetchone()[0] == 0


def test_migration_skips_missing_tables(tmp_path):
    """If a target table doesn't exist (e.g. pre-bootstrap), migration logs and skips."""
    conn = sqlite3.connect(":memory:")
    # Create only the jobs table; companies and company_scan_log absent.
    conn.execute(
        "CREATE TABLE jobs (rowid INTEGER PRIMARY KEY, first_seen TEXT, last_seen TEXT, posted_date TEXT)"
    )
    conn.commit()

    # Should not raise.
    MIGRATION.py(_make_ctx(tmp_path, conn))  # type: ignore[misc]
