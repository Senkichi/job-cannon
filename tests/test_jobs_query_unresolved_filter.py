"""get_filtered_jobs unresolved filter (Phase 47.06).

A row is "unresolved" if it carries non-empty unresolved_reasons (m078 column)
or any structured location is flagged unresolved=true. The default ``hide``
keeps such rows out of the standard listing; ``only`` surfaces them for
/admin/review; ``all`` applies no filter. The json_valid guard means a
malformed/empty locations_structured is treated as resolved, not a crash.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator

import pytest

from job_finder.db import get_filtered_jobs
from job_finder.web.db_migrate import run_migrations


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        yield c
        c.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def _insert(
    conn: sqlite3.Connection,
    key: str,
    *,
    unresolved_reasons: str = "[]",
    locations_structured: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, "
        "last_seen, pipeline_status, unresolved_reasons, locations_structured) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            "Engineer",
            "Co",
            "Remote",
            "2026-01-01",
            "2026-01-01",
            "new",
            unresolved_reasons,
            locations_structured,
        ),
    )
    conn.commit()


def _keys(rows: list[dict]) -> list[str]:
    return sorted(r["dedup_key"] for r in rows)


@pytest.fixture()
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    _insert(conn, "resolved")  # clean: '[]' reasons, NULL locations
    _insert(conn, "resolved_loc", locations_structured='[{"city": "NYC", "unresolved": false}]')
    _insert(conn, "by_reason", unresolved_reasons='["title_metadata_blob"]')
    _insert(conn, "by_location", locations_structured='[{"city": "NYC", "unresolved": true}]')
    return conn


def test_default_hides_unresolved(seeded: sqlite3.Connection):
    # No unresolved kwarg → default "hide".
    rows = get_filtered_jobs(seeded)
    assert _keys(rows) == ["resolved", "resolved_loc"]


def test_only_returns_unresolved(seeded: sqlite3.Connection):
    rows = get_filtered_jobs(seeded, unresolved="only")
    assert _keys(rows) == ["by_location", "by_reason"]


def test_all_returns_everything(seeded: sqlite3.Connection):
    rows = get_filtered_jobs(seeded, unresolved="all")
    assert _keys(rows) == ["by_location", "by_reason", "resolved", "resolved_loc"]


def test_unknown_value_falls_back_to_hide(seeded: sqlite3.Connection):
    # Defensive: a garbage param must not surface unresolved rows.
    rows = get_filtered_jobs(seeded, unresolved="garbage")
    assert _keys(rows) == ["resolved", "resolved_loc"]


def test_malformed_locations_treated_as_resolved(conn: sqlite3.Connection):
    # '' is not valid JSON; json_valid guard must keep it out of "only" and
    # in "hide" rather than raising "malformed JSON".
    _insert(conn, "empty_locs", locations_structured="")
    assert _keys(get_filtered_jobs(conn)) == ["empty_locs"]
    assert get_filtered_jobs(conn, unresolved="only") == []
