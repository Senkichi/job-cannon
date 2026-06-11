"""Migration 93 — re-normalize jobs.posted_date / first_seen to naive UTC.

m077 (2026-05-29) stripped tz suffixes from then-existing rows, but the
write path kept serializing whatever tzinfo the source carried:
``Job.__post_init__`` preserves tzinfo from ``fromisoformat()`` and
``upsert_job`` called ``.isoformat()`` on it directly. Every ingest since
re-introduced tz-aware strings — at audit time (2026-06-11) 1,432 of 1,848
non-NULL ``posted_date`` values carried a suffix (raw Greenhouse ``-04:00``
offsets among them), plus 278 ``first_seen`` rows via the INSERT branch's
``first_seen = pd_str`` seeding.

The companion code change (#361) adds the missing boundary enforcement in
``upsert_job`` (``to_naive_utc_iso``), so this migration is the last
backfill — unlike m077 it cannot be invalidated by subsequent ingests.

Same Phase-A semantics as m077: lossless — only rows with an explicit tz
suffix are parsed as aware, converted to UTC, and re-written naive. Naive
rows are untouched (no heuristic local-shift), so re-running is a no-op.
Fresh public-release databases hit this with zero eligible rows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("jobs", "posted_date"),
    ("jobs", "first_seen"),
)

# Trailing "Z" or "+HH:MM" / "-HH:MM" (with optional colon) — same predicate
# as m077 Phase A.
_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _to_naive_utc(value: str) -> str | None:
    """Parse a tz-suffixed ISO string and return naive UTC ISO, else None."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def _migrate(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    for table, column in _COLUMNS:
        rows = conn.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for rid, val in rows:
            if not isinstance(val, str) or not _TZ_SUFFIX_RE.search(val):
                continue
            new_val = _to_naive_utc(val)
            if new_val and new_val != val:
                updates.append((new_val, rid))

        if updates:
            conn.executemany(
                f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                updates,
            )
        logger.info("m093: %s.%s — normalized %d tz-aware rows", table, column, len(updates))


MIGRATION = Migration(
    version=93,
    description="re-normalize jobs.posted_date / first_seen to naive UTC (post-m077 write-path leak)",
    py=_migrate,
)
