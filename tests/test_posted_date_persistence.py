"""posted_date plumbing: upsert_job writes Job.posted_date to the DB column.

Covers four cases from issue #42 / Phase 46.02:

  Test 1 — INSERT with non-NULL posted_date lands in the column.
  Test 2 — INSERT with posted_date=None writes NULL (no synthesis from first_seen).
  Test 3 — UPDATE re-ingest with None does NOT overwrite existing non-NULL value
            (COALESCE protection, parity with workplace_type defence).
  Test 4 — UPDATE re-ingest with a value DOES overwrite existing NULL.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115
    tmp.close()
    path = Path(tmp.name)
    try:
        run_migrations(str(path))
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        yield c
        c.close()
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DT_A = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
_DT_B = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)


def _make_job(title: str, posted_date: datetime | None) -> Job:
    return Job(
        title=title,
        company="TestCo",
        location="Remote",
        source="lever",
        source_url=f"https://example.com/j/{title}",
        description="x" * 250,
        posted_date=posted_date,
    )


def _read_pd(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    row = conn.execute("SELECT posted_date FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    assert row is not None, f"No job found with dedup_key={dedup_key!r}"
    return row["posted_date"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPostedDatePersistence:
    def test_insert_with_posted_date_lands_in_column(self, conn: sqlite3.Connection):
        """Test 1: INSERT with a non-NULL posted_date stores it in the DB."""
        job = _make_job("Senior Eng", posted_date=_DT_A)
        upsert_job(conn, job)

        pd = _read_pd(conn, job.dedup_key)
        # datetime.isoformat() with UTC tzinfo produces '+00:00' form
        assert pd == _DT_A.isoformat()

    def test_insert_with_none_posted_date_stores_null(self, conn: sqlite3.Connection):
        """Test 2: INSERT with posted_date=None stores NULL — no synthesis from first_seen."""
        job = _make_job("Staff Eng", posted_date=None)
        upsert_job(conn, job)

        pd = _read_pd(conn, job.dedup_key)
        assert pd is None

    def test_update_none_does_not_overwrite_existing_value(self, conn: sqlite3.Connection):
        """Test 3: Re-ingest with posted_date=None preserves existing non-NULL value (COALESCE)."""
        # First ingest: sets posted_date
        job_first = _make_job("Principal Eng", posted_date=_DT_A)
        upsert_job(conn, job_first)
        assert _read_pd(conn, job_first.dedup_key) == _DT_A.isoformat()

        # Second ingest: same dedup_key, posted_date=None
        job_second = _make_job("Principal Eng", posted_date=None)
        assert job_second.dedup_key == job_first.dedup_key  # same job
        upsert_job(conn, job_second)

        # DB value must be unchanged
        pd = _read_pd(conn, job_first.dedup_key)
        assert pd == _DT_A.isoformat()

    def test_update_value_overwrites_existing_null(self, conn: sqlite3.Connection):
        """Test 4: Re-ingest with posted_date updates a previously-NULL row."""
        # First ingest: no posted_date
        job_first = _make_job("Director Eng", posted_date=None)
        upsert_job(conn, job_first)
        assert _read_pd(conn, job_first.dedup_key) is None

        # Second ingest: now we have a date
        job_second = _make_job("Director Eng", posted_date=_DT_B)
        assert job_second.dedup_key == job_first.dedup_key  # same job
        upsert_job(conn, job_second)

        pd = _read_pd(conn, job_first.dedup_key)
        assert pd == _DT_B.isoformat()

    def test_ats_api_string_posted_date_coerced_and_persisted(self, conn: sqlite3.Connection):
        """Regression #108: ATS-API string posted_date must be coerced and persisted.

        Lever/Greenhouse/Ashby/SmartRecruiters emit posted_date as an ISO-8601
        string (e.g. datetime(...).isoformat()). Job.__post_init__ must coerce it
        to datetime so upsert_job can call .isoformat() without AttributeError.
        """
        job = Job(
            title="ATS API Engineer",
            company="GreenhouseCo",
            location="Remote",
            source="Greenhouse",
            source_url="https://boards.greenhouse.io/test/jobs/1",
            description="x" * 250,
            # Exactly the shape that Lever, Greenhouse, Ashby emit: ISO string not datetime
            posted_date="2026-06-01T00:00:00+00:00",
        )
        # __post_init__ must have coerced this to a datetime
        assert isinstance(job.posted_date, datetime), (
            "Job.__post_init__ must coerce posted_date strings to datetime; "
            f"got {type(job.posted_date)!r} instead"
        )

        # Must not raise: AttributeError: 'str' object has no attribute 'isoformat'
        result = upsert_job(conn, job)
        assert result.kind == "inserted"

        pd = _read_pd(conn, job.dedup_key)
        assert pd is not None
        assert pd.startswith("2026-06-01")

    def test_ats_api_unparseable_posted_date_falls_back_to_none(self, conn: sqlite3.Connection):
        """Regression #108: An unparseable posted_date string degrades to None, not an error."""
        job = Job(
            title="Bad Date Engineer",
            company="BadDateCo",
            location="Remote",
            source="Greenhouse",
            source_url="https://boards.greenhouse.io/test/jobs/2",
            description="x" * 250,
            posted_date="not-a-date",  # garbage input — should silently become None
        )
        assert job.posted_date is None

        # Job must still persist; the bad date is simply dropped
        result = upsert_job(conn, job)
        assert result.kind == "inserted"
        assert _read_pd(conn, job.dedup_key) is None
