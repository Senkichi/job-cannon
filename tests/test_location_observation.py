"""Tests for the apply_location_observation single-writer funnel (D-5, #386).

Covers:
  - merge_locations_raw pure semantics (Remote/Hybrid first, dedup, immutability).
  - apply_location_observation populates all five canonical columns from an
    observation on an empty-location row, and is idempotent on re-apply.
  - The S4 wipe regression: an enrichment observation survives a subsequent
    crawler re-sighting that carries an empty incoming location — seeded via a
    real upsert_job call with a careers-crawl-shaped ParsedJob (location="").
"""

from __future__ import annotations

import json

from job_finder.db import apply_location_observation, get_job, merge_locations_raw, upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.location_canonical import from_json as locations_from_json


def _careers_crawl_parsed(*, location: str = "", description: str | None = None) -> ParsedJob:
    """A ParsedJob shaped like the careers crawler emits: location="" (#386 S4)."""
    job = Job(
        title="Data Scientist",
        company="EY",
        location=location,
        source="careers_crawl",
        source_url="https://careers.ey.com/jobs/de-data-scientist-vg-w4-cdao0217",
        description=description,
    )
    return ParsedJob.from_job(job)


# ---------------------------------------------------------------------------
# merge_locations_raw — pure helper
# ---------------------------------------------------------------------------


def test_merge_locations_raw_remote_hybrid_first() -> None:
    merged = merge_locations_raw(["New York, NY"], ["Remote"])
    assert merged == ["Remote", "New York, NY"]


def test_merge_locations_raw_case_insensitive_dedup() -> None:
    merged = merge_locations_raw(["Hyderabad"], ["hyderabad", "Bengaluru"])
    assert merged == ["Hyderabad", "Bengaluru"]


def test_merge_locations_raw_does_not_mutate_inputs() -> None:
    existing = ["New York, NY"]
    incoming = ["Remote"]
    merge_locations_raw(existing, incoming)
    assert existing == ["New York, NY"]
    assert incoming == ["Remote"]


def test_merge_locations_raw_drops_empty_entries() -> None:
    assert merge_locations_raw(["", "NYC"], ["", "Austin, TX"]) == ["NYC", "Austin, TX"]


# ---------------------------------------------------------------------------
# apply_location_observation — funnel
# ---------------------------------------------------------------------------


def test_observation_populates_all_five_columns(migrated_db) -> None:
    _path, conn = migrated_db
    # Seed an empty-location row via the real upsert path.
    upsert_job(conn, _careers_crawl_parsed())
    parsed = _careers_crawl_parsed()

    changed = apply_location_observation(conn, parsed.dedup_key, "Hyderabad", source="llm_extract")
    assert changed is True

    row = get_job(conn, parsed.dedup_key)
    assert json.loads(row["locations_raw"]) == ["Hyderabad"]
    assert row["location"] == "Hyderabad"
    structured = locations_from_json(row["locations_structured"])
    assert len(structured) == 1
    assert structured[0].city == "Hyderābād"
    assert structured[0].country_code == "IN"
    # primary_country_code / workplace_type are denormalized filter columns not
    # in JOBS_ALL_COLUMNS — query them directly.
    denorm = conn.execute(
        "SELECT primary_country_code, workplace_type FROM jobs WHERE dedup_key = ?",
        (parsed.dedup_key,),
    ).fetchone()
    assert denorm["primary_country_code"] == "IN"
    # workplace_type stays UNSPECIFIED (no workplace token in "Hyderabad").
    assert denorm["workplace_type"] == "UNSPECIFIED"


def test_observation_is_idempotent(migrated_db) -> None:
    _path, conn = migrated_db
    upsert_job(conn, _careers_crawl_parsed())
    parsed = _careers_crawl_parsed()

    assert apply_location_observation(conn, parsed.dedup_key, "Hyderabad", source="llm_extract")
    before = get_job(conn, parsed.dedup_key)
    # Re-applying the same observation changes nothing.
    assert (
        apply_location_observation(conn, parsed.dedup_key, "Hyderabad", source="llm_extract")
        is False
    )
    after = get_job(conn, parsed.dedup_key)
    assert before["locations_raw"] == after["locations_raw"]
    assert before["location"] == after["location"]
    assert before["locations_structured"] == after["locations_structured"]


def test_observation_missing_row_is_noop(migrated_db) -> None:
    _path, conn = migrated_db
    assert (
        apply_location_observation(conn, "does-not-exist", "Hyderabad", source="llm_extract")
        is False
    )


def test_observation_empty_input_is_noop(migrated_db) -> None:
    _path, conn = migrated_db
    upsert_job(conn, _careers_crawl_parsed())
    parsed = _careers_crawl_parsed()
    assert apply_location_observation(conn, parsed.dedup_key, "", source="llm_extract") is False
    assert apply_location_observation(conn, parsed.dedup_key, "   ", source="llm_extract") is False


def test_s4_wipe_regression_location_survives_resighting(migrated_db) -> None:
    """The S4 wipe: an enrichment observation must survive a later re-sighting.

    Before #386, the enricher wrote the bare ``location`` column with an empty
    ``locations_raw``; the next crawler re-sighting rebuilt ``location`` from
    ``locations_raw=[]`` and reverted it to ''. Routing through the funnel
    populates ``locations_raw`` too, so the re-sighting's set-union merge keeps
    the observed location.
    """
    _path, conn = migrated_db
    parsed = _careers_crawl_parsed()
    upsert_job(conn, parsed)

    # Enrichment observes "Hyderabad" through the funnel.
    assert apply_location_observation(conn, parsed.dedup_key, "Hyderabad", source="llm_extract")
    assert get_job(conn, parsed.dedup_key)["location"] == "Hyderabad"

    # The crawler re-sights the same job with an empty location (real upsert).
    result = upsert_job(conn, _careers_crawl_parsed())
    assert result.kind in {"touched", "unchanged"}

    # Location survives the re-sighting (the regression assertion).
    row = get_job(conn, parsed.dedup_key)
    assert row["location"] == "Hyderabad"
    assert json.loads(row["locations_raw"]) == ["Hyderabad"]
    pcc = conn.execute(
        "SELECT primary_country_code FROM jobs WHERE dedup_key = ?",
        (parsed.dedup_key,),
    ).fetchone()[0]
    assert pcc == "IN"
