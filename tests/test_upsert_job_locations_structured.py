"""upsert_job with locations_structured kwarg + m066 column writes.

SPEC: ``.planning/SPEC-location-parsing.md`` §Ingestion Boundary Changes.

Covers:
- Layer 1 (kwarg provided): structured list written verbatim;
  denormalized cols (workplace_type, primary_country_code) derived
  from index 0.
- Layer 2 (kwarg=None): parse_locations(job.location) auto-derives.
- Legacy columns (location, locations_raw) untouched — back-compat.
- UPDATE branch overwrites the m066 cols (last-seen wins for structured).
- Empty list / parse failure → 3 cols NULL (not crashes).
"""

from __future__ import annotations

import json
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


def _select_loc_cols(conn: sqlite3.Connection, dedup_key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT location, locations_raw, locations_structured, "
        "workplace_type, primary_country_code FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    assert row is not None, f"job {dedup_key!r} not found"
    return row


def _make_job(
    *, location: str = "San Francisco, CA", title: str = "Senior Eng", company: str = "TestCo"
) -> Job:
    return Job(
        title=title,
        company=company,
        location=location,
        source="lever",
        source_url="https://example.com/j/1",
        description="x" * 250,
    )


def _to_parsed(
    job: Job,
    *,
    locations_structured: list[JobLocation] | None = None,
) -> ParsedJob:
    """Convert a Job to ParsedJob, optionally embedding structured locations."""
    sm = (
        {"locations_structured": locations_structured}
        if locations_structured is not None
        else None
    )
    return ParsedJob.from_job(job, source_meta=sm)  # type: ignore[return-value]


# ---------- Layer-1: kwarg provided ----------


def test_layer1_provided_list_written_verbatim_and_denormalized(conn: sqlite3.Connection):
    structured = [
        JobLocation(
            city="San Francisco",
            region="California",
            region_code="CA",
            country="United States",
            country_code="US",
            workplace_type="HYBRID",
            raw="San Francisco, CA",
            unresolved=False,
        )
    ]
    job = _make_job(location="San Francisco, CA")
    result = upsert_job(conn, _to_parsed(job, locations_structured=structured))

    assert result.kind == "inserted"
    row = _select_loc_cols(conn, job.dedup_key)
    decoded = json.loads(row["locations_structured"])
    assert len(decoded) == 1
    assert decoded[0]["city"] == "San Francisco"
    assert decoded[0]["country_code"] == "US"
    assert row["workplace_type"] == "HYBRID"
    assert row["primary_country_code"] == "US"


def test_layer1_first_entry_drives_denormalized_cols(conn: sqlite3.Connection):
    """Multi-location: workplace_type + primary_country_code = locations[0].* per SPEC."""
    structured = [
        JobLocation(
            city="Toronto",
            region=None,
            region_code="ON",
            country=None,
            country_code="CA",
            workplace_type="REMOTE",
            raw="Toronto, ON",
            unresolved=False,
        ),
        JobLocation(
            city="Paris",
            region=None,
            region_code=None,
            country=None,
            country_code="FR",
            workplace_type="HYBRID",
            raw="Paris, FR",
            unresolved=False,
        ),
    ]
    job = _make_job(location="Toronto, ON / Paris, FR")
    upsert_job(conn, _to_parsed(job, locations_structured=structured))
    row = _select_loc_cols(conn, job.dedup_key)
    assert row["workplace_type"] == "REMOTE"  # first
    assert row["primary_country_code"] == "CA"  # first


# ---------- Layer-2: kwarg=None auto-derives ----------


def test_layer2_default_parses_job_location(conn: sqlite3.Connection):
    """No kwarg → parse_locations(job.location) populates the m066 cols."""
    job = _make_job(location="Bengaluru, India")
    upsert_job(conn, _to_parsed(job))  # no kwarg

    row = _select_loc_cols(conn, job.dedup_key)
    assert row["locations_structured"] is not None
    decoded = json.loads(row["locations_structured"])
    assert decoded[0]["city"] == "Bengaluru"
    assert decoded[0]["country_code"] == "IN"
    assert row["primary_country_code"] == "IN"


def test_layer2_empty_location_string_writes_nulls(conn: sqlite3.Connection):
    """Empty/placeholder input → parse_locations returns []; locations_structured
    + primary_country_code stay NULL but workplace_type defaults to
    'UNSPECIFIED' (per m072 contract — column must always be populated)."""
    job = _make_job(location="")
    upsert_job(conn, _to_parsed(job))
    row = _select_loc_cols(conn, job.dedup_key)
    assert row["locations_structured"] is None
    assert row["workplace_type"] == "UNSPECIFIED"
    assert row["primary_country_code"] is None


def test_layer2_multiple_locations_dropped_placeholder_writes_nulls(conn: sqlite3.Connection):
    """SPEC: 'Multiple Locations' → parser drops; cols stay NULL."""
    job = _make_job(location="Multiple Locations")
    upsert_job(conn, _to_parsed(job))
    row = _select_loc_cols(conn, job.dedup_key)
    assert row["locations_structured"] is None


# ---------- Legacy columns untouched ----------


def test_legacy_location_and_locations_raw_preserved_with_kwarg(conn: sqlite3.Connection):
    """SPEC: 'Keep the existing string columns intact for back-compat.'"""
    structured = [
        JobLocation(
            city="Madrid",
            region=None,
            region_code=None,
            country="Spain",
            country_code="ES",
            workplace_type="ONSITE",
            raw="Madrid, Spain",
            unresolved=False,
        )
    ]
    job = _make_job(location="Madrid, ES / Remote")  # legacy string differs from raw
    upsert_job(conn, _to_parsed(job, locations_structured=structured))
    row = _select_loc_cols(conn, job.dedup_key)
    # location column still mirrors the legacy split, NOT [loc.raw, ...]
    assert "Madrid" in row["location"]
    assert "Remote" in row["location"]
    assert "Remote" in row["locations_raw"]


# ---------- UPDATE branch: last-seen wins ----------


def test_update_branch_overwrites_structured_cols(conn: sqlite3.Connection):
    """Second upsert with different structured → overwrites the 3 m066 cols."""
    job = _make_job(location="Tokyo, Japan")
    first = [
        JobLocation(
            city="Tokyo",
            region=None,
            region_code=None,
            country="Japan",
            country_code="JP",
            workplace_type="ONSITE",
            raw="Tokyo, Japan",
            unresolved=False,
        )
    ]
    upsert_job(conn, _to_parsed(job, locations_structured=first))
    row_before = _select_loc_cols(conn, job.dedup_key)
    assert row_before["primary_country_code"] == "JP"
    assert row_before["workplace_type"] == "ONSITE"

    # Second ingestion with REMOTE workplace_type
    second = [
        JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code="US",
            workplace_type="REMOTE",
            raw="Remote - US",
            unresolved=False,
        )
    ]
    result = upsert_job(conn, _to_parsed(job, locations_structured=second))
    assert result.kind != "inserted"  # update branch
    row_after = _select_loc_cols(conn, job.dedup_key)
    assert row_after["primary_country_code"] == "US"
    assert row_after["workplace_type"] == "REMOTE"


def test_update_branch_layer2_fallback_when_kwarg_none(conn: sqlite3.Connection):
    """UPDATE without kwarg also runs Layer 2 — symmetric with INSERT."""
    job = _make_job(location="Berlin, Germany")
    upsert_job(conn, _to_parsed(job))  # insert via Layer 2
    row_before = _select_loc_cols(conn, job.dedup_key)
    assert row_before["primary_country_code"] == "DE"

    # Update via Layer 2 with a new location string
    job2 = _make_job(location="Madrid, Spain")
    upsert_job(conn, _to_parsed(job2))  # update branch (same dedup_key)
    row_after = _select_loc_cols(conn, job.dedup_key)
    assert row_after["primary_country_code"] == "ES"
