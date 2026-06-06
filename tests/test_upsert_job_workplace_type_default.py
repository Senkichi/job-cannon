"""upsert_job workplace_type default + UPDATE no-downgrade behavior.

INSERT path defaults workplace_type to 'UNSPECIFIED' when location
parsing yields no structured locations (rather than NULL). UPDATE path
uses COALESCE+NULLIF so that an 'UNSPECIFIED' from a re-ingestion
cannot downgrade a real value (REMOTE / HYBRID / ONSITE) that an
earlier scan extracted, but a real value DOES overwrite an existing
'UNSPECIFIED' as ingestion learns more.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.db_migrate import run_migrations
from job_finder.web.location_canonical import JobLocation


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — explicit close+unlink to share path with sqlite3.connect
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


def _make_job(*, title: str, location: str = "") -> Job:
    return Job(
        title=title,
        company="TestCo",
        location=location,
        source="lever",
        source_url=f"https://example.com/j/{title}",
        description="x" * 250,
    )


def _to_parsed(job: Job, *, locations_structured: list[JobLocation] | None = None) -> ParsedJob:
    """Convert a Job to ParsedJob for upsert_job (Phase 48.07 contract)."""
    sm = (
        {"locations_structured": locations_structured}
        if locations_structured is not None
        else None
    )
    return ParsedJob.from_job(job, source_meta=sm)  # type: ignore[return-value]


def _read_wt(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    r = conn.execute(
        "SELECT workplace_type FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return r["workplace_type"]


class TestInsertDefaults:
    def test_insert_without_location_defaults_unspecified(self, conn: sqlite3.Connection):
        # location='' → location_parser returns []; INSERT must still
        # write 'UNSPECIFIED' (not NULL) so consumers can rely on the
        # column being populated.
        upsert_job(conn, _to_parsed(_make_job(title="a", location="")))
        assert _read_wt(conn, "testco|a") == "UNSPECIFIED"

    def test_insert_with_explicit_remote_kept(self, conn: sqlite3.Connection):
        # Layer-1 path: caller passes locations_structured directly.
        loc = JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type="REMOTE",
            raw="Remote",
            unresolved=False,
        )
        upsert_job(conn, _to_parsed(_make_job(title="b"), locations_structured=[loc]))
        assert _read_wt(conn, "testco|b") == "REMOTE"


class TestUpdateNoDowngrade:
    def test_remote_not_downgraded_by_unspecified_reingest(self, conn: sqlite3.Connection):
        # First ingest: REMOTE locked in.
        loc = JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type="REMOTE",
            raw="Remote",
            unresolved=False,
        )
        upsert_job(conn, _to_parsed(_make_job(title="c"), locations_structured=[loc]))
        assert _read_wt(conn, "testco|c") == "REMOTE"
        # Re-ingest from a source that has no location info — the parser
        # returns [], so workplace_type_col is 'UNSPECIFIED'. UPDATE must
        # not downgrade.
        upsert_job(conn, _to_parsed(_make_job(title="c", location="")))
        assert _read_wt(conn, "testco|c") == "REMOTE"

    def test_unspecified_upgrades_to_real_value_on_better_data(self, conn: sqlite3.Connection):
        # First ingest: no location → 'UNSPECIFIED'.
        upsert_job(conn, _to_parsed(_make_job(title="d", location="")))
        assert _read_wt(conn, "testco|d") == "UNSPECIFIED"
        # Re-ingest with structured REMOTE — real value upgrades the
        # UNSPECIFIED placeholder.
        loc = JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type="REMOTE",
            raw="Remote",
            unresolved=False,
        )
        upsert_job(conn, _to_parsed(_make_job(title="d"), locations_structured=[loc]))
        assert _read_wt(conn, "testco|d") == "REMOTE"
