"""Touch-path folded into upsert_job (Phase 47.09 / D-15).

The former `_touch_existing_job` raw-UPDATE bypass is gone; its lightweight
re-sighting logic is now an internal branch of upsert_job, surfaced as
``UpsertResult.kind == "touched"``:

  - A re-ingest that adds only a new source (no canonical change) → "touched":
    last_seen refreshed, sources set-union'd, scoring + unresolved_reasons left
    untouched (so an /admin/review approval survives subsequent ingestion).
  - A re-ingest with a new canonical field (e.g. salary) → "updated".
  - An identical re-ingest → "unchanged".
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
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


def _make_job(
    *, source: str = "lever", url: str = "https://ex.co/1", salary_min: int | None = None
) -> Job:
    j = Job(
        title="Staff Engineer",
        company="AcmeCo",
        location="Remote",
        source=source,
        source_url=url,
        description="x" * 250,
    )
    j.score = 50.0
    if salary_min is not None:
        j.salary_min = salary_min
    return j


def _upsert(conn: sqlite3.Connection, job: Job):
    """Convert Job->ParsedJob before calling upsert_job (shim removed Phase 48.07)."""
    parsed = ParsedJob.from_job(job)
    return upsert_job(conn, parsed)


def _read(conn: sqlite3.Connection, dedup_key: str, col: str):
    return conn.execute(f"SELECT {col} FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()[0]


def test_new_source_only_is_touched(conn: sqlite3.Connection):
    r1 = _upsert(conn, _make_job())
    assert r1.kind == "inserted"
    last_seen_before = _read(conn, r1.dedup_key, "last_seen")

    # Re-ingest from a different feed: no canonical change, new source.
    r2 = _upsert(conn, _make_job(source="greenhouse", url="https://ex.co/2"))
    assert r2.kind == "touched"
    assert r2.dedup_key == r1.dedup_key

    sources = _read(conn, r1.dedup_key, "sources")
    assert "lever" in sources and "greenhouse" in sources  # set-union'd
    assert _read(conn, r1.dedup_key, "last_seen") >= last_seen_before


def test_identical_reingest_is_unchanged(conn: sqlite3.Connection):
    r1 = _upsert(conn, _make_job())
    assert r1.kind == "inserted"
    r2 = _upsert(conn, _make_job())
    assert r2.kind == "unchanged"


def test_new_salary_is_updated(conn: sqlite3.Connection):
    r1 = _upsert(conn, _make_job())
    assert r1.kind == "inserted"
    r2 = _upsert(conn, _make_job(salary_min=180_000))
    assert r2.kind == "updated"
    assert _read(conn, r1.dedup_key, "salary_min") == 180_000


def test_touch_preserves_unresolved_reasons(conn: sqlite3.Connection):
    """A reviewer's /admin/review state survives a subsequent touch (§8.4)."""
    r1 = _upsert(conn, _make_job())
    dedup_key = r1.dedup_key

    # Simulate a flagged-then-reviewed row: set reason codes directly.
    conn.execute(
        "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
        ('["title_metadata_blob"]', dedup_key),
    )
    conn.commit()

    # Re-ingest from a new feed → touched. Must NOT clobber unresolved_reasons.
    r2 = _upsert(conn, _make_job(source="greenhouse", url="https://ex.co/2"))
    assert r2.kind == "touched"
    assert _read(conn, dedup_key, "unresolved_reasons") == '["title_metadata_blob"]'


def test_approved_state_survives_touch(conn: sqlite3.Connection):
    """An approved row (unresolved_reasons cleared to '[]') stays cleared."""
    r1 = _upsert(conn, _make_job())
    dedup_key = r1.dedup_key
    # Flagged, then approved (cleared) via /admin/review.
    conn.execute("UPDATE jobs SET unresolved_reasons = '[]' WHERE dedup_key = ?", (dedup_key,))
    conn.commit()
    r2 = _upsert(conn, _make_job(source="ashby", url="https://ex.co/3"))
    assert r2.kind == "touched"
    assert _read(conn, dedup_key, "unresolved_reasons") == "[]"
