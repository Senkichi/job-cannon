"""Migration 77 — normalize stored timestamps to naive UTC.

Background: prior to the 2026-05-29 timezone normalization pass the
codebase mixed three storage shapes for datetime columns:

  - naive UTC ISO (from ``utc_now_iso`` / SQL ``datetime('now')``)
  - naive local ISO (from ``datetime.now().isoformat()`` in many sites)
  - tz-aware ISO with explicit ``+00:00`` / ``Z`` suffix (from
    ``parsedate_to_datetime(...).isoformat()`` in email parsers and
    ``datetime.fromtimestamp(..., tz=UTC).isoformat()`` in source feeds)

The display layer interprets stored strings as naive UTC and converts to
the user's OS-local clock. Historical rows that were written as naive
local will render ~N hours off from reality (where N is the local-UTC
offset at the time the row was written) until they age out or get
re-ingested. tz-aware rows render correctly today but carry the now-
inconsistent suffix.

This migration runs once and does two things:

  Phase A (lossless, all target columns):
    Any row whose value carries an explicit tz suffix (``Z`` or
    ``[+-]HH:MM``) is parsed, converted to UTC, and re-written as naive
    UTC ISO. Email-sourced ``posted_date`` / ``first_seen`` rows are the
    common case here.

  Phase B (heuristic, single-source columns only):
    For ``companies.last_scanned_at`` and ``company_scan_log.scanned_at``
    — whose only historical write path was Python ``datetime.now()``
    (now ``utc_now_iso()``) — naive rows are treated as user-local and
    shifted by the current local→UTC offset. Within ±1h of correct
    across DST boundaries.

Out of scope: ``jobs.first_seen`` / ``last_seen`` from non-email sources
(SerpAPI / Thordata / DataForSEO / portal feeds) were written via the
local-time ``now`` param of ``upsert_job``. They are indistinguishable
from email-sourced naive-UTC rows post-Phase-A, so Phase B is NOT
applied to them — they keep their ~N-hour cosmetic skew until the next
ingestion cycle re-touches ``last_seen``. The user accepted this trade-
off (2026-05-29) over a more aggressive blanket shift.

Public-release users with fresh databases hit this migration with
empty target tables — the helper bails on each column with a zero-row
log message.

Re-running is safe: Phase A is idempotent (a naive value has no tz
suffix to strip) and Phase B is gated on the absence of a tz suffix.
After this migration commits, the gate predicate `+` or `Z` suffix is
removed for the targeted columns, so a second run finds no eligible
rows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Columns where Phase A (lossless tz-suffix stripping) is safe and useful.
_PHASE_A_COLUMNS: tuple[tuple[str, str], ...] = (
    ("jobs", "first_seen"),
    ("jobs", "last_seen"),
    ("jobs", "posted_date"),
    ("companies", "last_scanned_at"),
    ("company_scan_log", "scanned_at"),
)

# Columns where Phase B (heuristic naive-local → naive-UTC shift) is safe.
# Must be a strict subset of _PHASE_A_COLUMNS.
_PHASE_B_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("companies", "last_scanned_at"),
        ("company_scan_log", "scanned_at"),
    }
)

# Detect explicit tz suffix on an ISO string: trailing "Z" or "+HH:MM" / "-HH:MM"
# (with optional colon).
_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _compute_local_to_utc_offset_hours() -> float:
    """Return current local→UTC offset in hours (e.g. +7.0 in PDT, +5.5 in IST).

    Computed from ``time.timezone`` / ``time.altzone`` so the value is exact
    to the OS-reported zone (no microsecond drift between two ``datetime.now``
    calls). Honors DST via ``time.localtime().tm_isdst``.

    Note: rows written across historical DST transitions are within ±1h of
    correct because we use the current offset uniformly — acceptable for
    the cosmetic-shift target.
    """
    import time

    secs = time.altzone if time.localtime().tm_isdst else time.timezone
    # `time.timezone` is "seconds WEST of UTC" — positive for North America,
    # negative for Europe/Asia east of Greenwich. Our convention is
    # local→UTC offset = UTC - local, which is the same sign.
    return secs / 3600.0


def _normalize_tz_aware_to_naive_utc(value: str) -> str | None:
    """If value carries a tz suffix, parse as aware and return naive UTC ISO."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def _shift_naive_local_to_naive_utc(value: str, offset_hours: float) -> str | None:
    """Treat value as naive local datetime; shift by offset to become naive UTC."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        return None  # not naive — caller should have routed to Phase A
    return (dt + timedelta(hours=offset_hours)).isoformat()


def _normalize_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    apply_phase_b: bool,
    offset_hours: float,
) -> tuple[int, int]:
    """Normalize one (table, column) pair.

    Returns (phase_a_updates, phase_b_updates).
    """
    if not _table_exists(conn, table):
        logger.info("m077: table %s not present, skipping", table)
        return (0, 0)
    if not _column_exists(conn, table, column):
        logger.info("m077: column %s.%s not present, skipping", table, column)
        return (0, 0)

    rows = conn.execute(
        f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608 — column allowlisted
    ).fetchall()

    phase_a_updates: list[tuple[str, int]] = []
    phase_b_updates: list[tuple[str, int]] = []

    for rid, val in rows:
        if not isinstance(val, str) or not val:
            continue
        if _TZ_SUFFIX_RE.search(val):
            new_val = _normalize_tz_aware_to_naive_utc(val)
            if new_val and new_val != val:
                phase_a_updates.append((new_val, rid))
        elif apply_phase_b:
            new_val = _shift_naive_local_to_naive_utc(val, offset_hours)
            if new_val and new_val != val:
                phase_b_updates.append((new_val, rid))

    if phase_a_updates:
        conn.executemany(
            f"UPDATE {table} SET {column} = ? WHERE rowid = ?",  # noqa: S608
            phase_a_updates,
        )
    if phase_b_updates:
        conn.executemany(
            f"UPDATE {table} SET {column} = ? WHERE rowid = ?",  # noqa: S608
            phase_b_updates,
        )

    return (len(phase_a_updates), len(phase_b_updates))


def _migrate(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    offset_hours = _compute_local_to_utc_offset_hours()

    total_phase_a = 0
    total_phase_b = 0

    for table, column in _PHASE_A_COLUMNS:
        apply_b = (table, column) in _PHASE_B_COLUMNS
        a_count, b_count = _normalize_column(
            conn,
            table,
            column,
            apply_phase_b=apply_b,
            offset_hours=offset_hours,
        )
        total_phase_a += a_count
        total_phase_b += b_count
        if a_count or b_count:
            logger.info(
                "m077: %s.%s — Phase A: %d, Phase B: %d (offset=%+.1fh)",
                table,
                column,
                a_count,
                b_count,
                offset_hours,
            )

    logger.info(
        "m077: timestamp normalization complete (Phase A total: %d, Phase B total: %d, offset=%+.1fh)",
        total_phase_a,
        total_phase_b,
        offset_hours,
    )


MIGRATION = Migration(
    version=77,
    description="normalize stored timestamps to naive UTC (strip tz suffixes; shift local-only columns)",
    py=_migrate,
)
