"""upsert_job picks up locations_structured from the ParsedJob + m066 column writes.

SPEC: ``.planning/SPEC-location-parsing.md`` §Ingestion Boundary Changes.

Covers:
- Layer 1 (structured supplied via ParsedJob.from_job source_meta): written
  verbatim; denormalized cols (workplace_type, primary_country_code) derived
  from index 0.
- Layer 2 (parsed.locations_structured empty): parse_locations(job.location)
  auto-derives.
- Legacy columns (location, locations_raw) untouched — back-compat.
- UPDATE branch unions the m066 cols (issue #639: merge_locations_structured).
  Denormalized cols derive from merged set's index 0 (first-seen location).
- Empty list / parse failure → 3 cols NULL (not crashes).

Phase 48.07 update: the former ``upsert_job(..., locations_structured=...)``
kwarg is gone; structured locations are carried into ParsedJob.from_job
via the ``source_meta`` parameter and read off ``parsed.locations_structured``
inside upsert_job. Same coverage, different boundary.

Issue #639 update: UPDATE branch now unions locations_structured instead of
overwriting. The merge uses merge_locations_structured which dedups by
(country_code, region_code, city, workplace_type) and preserves first-seen order.
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
from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob
from job_finder.web.db_migrate import run_migrations
from job_finder.web.location_canonical import from_json as locations_from_json
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


def _make_parsed(
    *,
    location: str = "San Francisco, CA",
    title: str = "Senior Eng",
    company: str = "TestCo",
    locations_structured: list[JobLocation] | None = None,
) -> ParsedJob | UnresolvedParsedJob:
    """Construct a ParsedJob via from_job — the post-48.07 caller boundary.

    structured locations ride in through ``source_meta`` rather than as a
    second upsert_job kwarg.
    """
    job = Job(
        title=title,
        company=company,
        location=location,
        source="lever",
        source_url="https://example.com/j/1",
        description="x" * 250,
    )
    source_meta: dict | None = None
    if locations_structured is not None:
        source_meta = {"locations_structured": locations_structured}
    return ParsedJob.from_job(job, source_meta=source_meta)


# ---------- Layer-1: structured supplied via source_meta ----------


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
    parsed = _make_parsed(location="San Francisco, CA", locations_structured=structured)
    result = upsert_job(conn, parsed)

    assert result.kind == "inserted"
    row = _select_loc_cols(conn, parsed.dedup_key)
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
    parsed = _make_parsed(location="Toronto, ON / Paris, FR", locations_structured=structured)
    upsert_job(conn, parsed)
    row = _select_loc_cols(conn, parsed.dedup_key)
    assert row["workplace_type"] == "REMOTE"  # first
    assert row["primary_country_code"] == "CA"  # first


# ---------- Layer-2: no structured supplied — upsert auto-derives ----------


def test_layer2_default_parses_job_location(conn: sqlite3.Connection):
    """No structured supplied → parse_locations(parsed.location) populates m066 cols."""
    parsed = _make_parsed(location="Bengaluru, India")
    upsert_job(conn, parsed)

    row = _select_loc_cols(conn, parsed.dedup_key)
    assert row["locations_structured"] is not None
    decoded = json.loads(row["locations_structured"])
    assert decoded[0]["city"] == "Bengaluru"
    assert decoded[0]["country_code"] == "IN"
    assert row["primary_country_code"] == "IN"


def test_layer2_empty_location_string_writes_nulls(conn: sqlite3.Connection):
    """Empty/placeholder input → parse_locations returns []; locations_structured
    + primary_country_code stay NULL but workplace_type defaults to
    'UNSPECIFIED' (per m072 contract — column must always be populated)."""
    parsed = _make_parsed(location="")
    upsert_job(conn, parsed)
    row = _select_loc_cols(conn, parsed.dedup_key)
    assert row["locations_structured"] is None
    assert row["workplace_type"] == "UNSPECIFIED"
    assert row["primary_country_code"] is None


def test_layer2_multiple_locations_dropped_placeholder_writes_nulls(conn: sqlite3.Connection):
    """SPEC: 'Multiple Locations' → parser drops; cols stay NULL."""
    parsed = _make_parsed(location="Multiple Locations")
    upsert_job(conn, parsed)
    row = _select_loc_cols(conn, parsed.dedup_key)
    assert row["locations_structured"] is None


# ---------- Legacy columns untouched ----------


def test_legacy_location_and_locations_raw_preserved_with_structured(conn: sqlite3.Connection):
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
    parsed = _make_parsed(
        location="Madrid, ES / Remote",  # legacy string differs from raw
        locations_structured=structured,
    )
    upsert_job(conn, parsed)
    row = _select_loc_cols(conn, parsed.dedup_key)
    # location column still mirrors the legacy split, NOT [loc.raw, ...]
    assert "Madrid" in row["location"]
    assert "Remote" in row["location"]
    assert "Remote" in row["locations_raw"]


# ---------- UPDATE branch: union merge (issue #639) ----------


def test_upsert_update_branch_unions_locations_structured(conn: sqlite3.Connection):
    """Insert with NYC, upsert same company|title with SF → stored locations_structured decodes to both NYC and SF (the Brigit fix)."""
    nyc = [
        JobLocation(
            city="New York",
            region="New York",
            region_code="NY",
            country="United States",
            country_code="US",
            workplace_type="HYBRID",
            raw="New York, NY",
            unresolved=False,
        )
    ]
    parsed_first = _make_parsed(
        company="Brigit", title="Lead Data Scientist", locations_structured=nyc
    )
    result_first = upsert_job(conn, parsed_first)
    assert result_first.kind == "inserted"

    sf = [
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
    parsed_second = _make_parsed(
        company="Brigit", title="Lead Data Scientist", locations_structured=sf
    )
    result_second = upsert_job(conn, parsed_second)
    assert result_second.kind == "updated"

    row = _select_loc_cols(conn, parsed_first.dedup_key)
    structured = locations_from_json(row["locations_structured"])
    assert len(structured) == 2
    city_names = {loc.city for loc in structured}
    assert city_names == {"New York", "San Francisco"}


def test_upsert_update_branch_recomputes_denormalized_from_merged(
    conn: sqlite3.Connection,
):
    """After the union above, workplace_type / primary_country_code reflect the merged set's index 0 (not the incoming-only value)."""
    nyc = [
        JobLocation(
            city="New York",
            region="New York",
            region_code="NY",
            country="United States",
            country_code="US",
            workplace_type="HYBRID",
            raw="New York, NY",
            unresolved=False,
        )
    ]
    parsed_first = _make_parsed(
        company="TestCo", title="Senior Eng", locations_structured=nyc
    )
    upsert_job(conn, parsed_first)

    sf = [
        JobLocation(
            city="San Francisco",
            region="California",
            region_code="CA",
            country="United States",
            country_code="US",
            workplace_type="REMOTE",
            raw="San Francisco, CA",
            unresolved=False,
        )
    ]
    parsed_second = _make_parsed(
        company="TestCo", title="Senior Eng", locations_structured=sf
    )
    upsert_job(conn, parsed_second)

    row = _select_loc_cols(conn, parsed_first.dedup_key)
    # Denormalized cols derive from merged set's index 0 (NYC, the first-seen location)
    assert row["workplace_type"] == "HYBRID"
    assert row["primary_country_code"] == "US"
    structured = locations_from_json(row["locations_structured"])
    assert len(structured) == 2


def test_upsert_update_branch_idempotent_on_same_structured(
    conn: sqlite3.Connection,
):
    """Re-sighting the identical structured location does not add a duplicate entry and the structured merge alone does not report "updated"."""
    loc = [
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
    parsed = _make_parsed(company="TestCo", title="Senior Eng", locations_structured=loc)
    result_first = upsert_job(conn, parsed)
    assert result_first.kind == "inserted"

    result_second = upsert_job(conn, parsed)
    # No canonical change from structured merge alone (same location re-sighted)
    assert result_second.kind in {"touched", "unchanged"}

    row = _select_loc_cols(conn, parsed.dedup_key)
    structured = locations_from_json(row["locations_structured"])
    assert len(structured) == 1


# ---------- UPDATE branch: legacy overwrite tests (updated for union merge) ----------


def test_update_branch_unions_structured_cols(conn: sqlite3.Connection):
    """Second upsert with different structured → unions the locations (not overwrite). Denormalized cols derive from merged index 0."""
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
    parsed_first = _make_parsed(location="Tokyo, Japan", locations_structured=first)
    upsert_job(conn, parsed_first)
    row_before = _select_loc_cols(conn, parsed_first.dedup_key)
    assert row_before["primary_country_code"] == "JP"
    assert row_before["workplace_type"] == "ONSITE"

    # Second ingestion with REMOTE workplace_type (different location)
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
    parsed_second = _make_parsed(location="Tokyo, Japan", locations_structured=second)
    result = upsert_job(conn, parsed_second)
    assert result.kind == "updated"  # canonical changed (new location added)
    row_after = _select_loc_cols(conn, parsed_first.dedup_key)

    # Under union-merge, both locations are present
    structured = locations_from_json(row_after["locations_structured"])
    assert len(structured) == 2
    country_codes = {loc.country_code for loc in structured}
    assert country_codes == {"JP", "US"}

    # Denormalized cols derive from merged set's index 0 (Tokyo, the first-seen location)
    assert row_after["primary_country_code"] == "JP"
    assert row_after["workplace_type"] == "ONSITE"


def test_update_branch_layer2_fallback_when_structured_empty(conn: sqlite3.Connection):
    """UPDATE without structured also runs Layer 2 — symmetric with INSERT. Under union-merge, both locations are present."""
    parsed_first = _make_parsed(location="Berlin, Germany")
    upsert_job(conn, parsed_first)  # insert via Layer 2
    row_before = _select_loc_cols(conn, parsed_first.dedup_key)
    assert row_before["primary_country_code"] == "DE"

    # Update via Layer 2 with a new location string
    parsed_second = _make_parsed(location="Madrid, Spain")
    upsert_job(conn, parsed_second)  # update branch (same dedup_key)
    row_after = _select_loc_cols(conn, parsed_first.dedup_key)

    # Under union-merge with recompute-from-merged-index-0, the merged set is [DE, ES]
    # so index-0 stays DE (first-seen location)
    structured = locations_from_json(row_after["locations_structured"])
    assert len(structured) == 2
    country_codes = {loc.country_code for loc in structured}
    assert country_codes == {"DE", "ES"}
    assert row_after["primary_country_code"] == "DE"  # First-seen location stays primary
