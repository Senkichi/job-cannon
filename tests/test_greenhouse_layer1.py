"""Tests for Greenhouse Layer-1 emission: source_id, posted_date, JobLocation, salary.

Phase 48.03 — Greenhouse Layer-1: emit JobLocation + source_id + posted_date,
resolve cents/dollars ambiguity (D-07 / F-02 / F-04 / F-06).

Three documented salary fixtures from the issue:
  1. Annual salary in cents (6_500_000 → $65,000)
  2. Hourly $64 (the "64-64" documented case) — stored as 64, NOT 6_400
  3. Missing interval AND value > 1_000 — stored as-is for Phase 49.02 flagging
"""

from __future__ import annotations

from job_finder.web.ats_platforms._platforms_greenhouse import (
    _posting_to_job,
    _resolve_salary,
    _to_canonical,
)
from job_finder.web.location_canonical import JobLocation

# ---------------------------------------------------------------------------
# _resolve_salary: pure unit tests
# ---------------------------------------------------------------------------


class TestResolveSalary:
    def test_annual_cents_large_value_divides(self):
        """interval='year' AND value > 1_000 → divide by 100 (cents to dollars)."""
        assert _resolve_salary(6_500_000, "year") == 65_000

    def test_hourly_small_value_stored_as_is(self):
        """interval='hour' → value is already dollars; no division applied."""
        assert _resolve_salary(64, "hour") == 64

    def test_annual_small_value_not_divided(self):
        """interval='year' but value ≤ 1_000 → treat as dollars (not cents)."""
        assert _resolve_salary(64, "year") == 64

    def test_no_interval_large_value_stored_as_is(self):
        """Missing interval AND value > 1_000 → store as-is (no blind division).

        Phase 49.02 unit-tagging will flag these suspect rows.
        """
        assert _resolve_salary(150_000, None) == 150_000

    def test_no_interval_small_value_stored_as_is(self):
        assert _resolve_salary(500, None) == 500

    def test_none_value_returns_none_regardless_of_interval(self):
        assert _resolve_salary(None, "year") is None
        assert _resolve_salary(None, "hour") is None
        assert _resolve_salary(None, None) is None


# ---------------------------------------------------------------------------
# _posting_to_job: three documented salary fixtures (acceptance criteria)
# ---------------------------------------------------------------------------


class TestSalaryFixtures:
    """The three concrete salary cases documented in the Phase 48.03 issue."""

    def test_fixture1_annual_cents_encoded_salary(self):
        """Fixture 1: cents-encoded annual salary.

        6_500_000 cents + interval="year" → $65,000.
        8_500_000 cents + interval="year" → $85,000.
        """
        posting = {
            "id": 12345,
            "title": "Software Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "location": {"name": "New York, NY"},
            "updated_at": "2024-01-15T10:30:00Z",
            "content": "Build great software.",
            "pay_input_ranges": [
                {
                    "min_cents": 6_500_000,
                    "max_cents": 8_500_000,
                    "unit": "year",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 65_000
        assert result["salary_max"] == 85_000

    def test_fixture2_hourly_dollar_salary_not_divided(self):
        """Fixture 2: the documented 64-64 case (hourly $64).

        min_cents=64 with unit="hour" → salary stored as 64, NOT 6_400.
        """
        posting = {
            "id": 67890,
            "title": "Data Analyst",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/67890",
            "location": {"name": "Remote"},
            "updated_at": "2024-02-01T09:00:00Z",
            "content": "Analyze data.",
            "pay_input_ranges": [
                {
                    "min_cents": 64,
                    "max_cents": 64,
                    "unit": "hour",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 64, (
            "Hourly $64 must NOT be divided by 100 — it is already in dollars"
        )
        assert result["salary_max"] == 64

    def test_fixture3_missing_interval_large_value_stored_as_is(self):
        """Fixture 3: no unit/interval field AND value > 1_000.

        Value is stored as-is (no division). Phase 49.02 surfaces the
        ambiguity via the unresolved salary_period flagging.
        """
        posting = {
            "id": 99999,
            "title": "Product Manager",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/99999",
            "location": {"name": "San Francisco, CA"},
            "updated_at": "2024-03-01T08:00:00Z",
            "content": "Drive product strategy.",
            "pay_input_ranges": [
                {
                    "min_cents": 150_000,
                    "max_cents": 200_000,
                    # deliberately omit "unit" / "interval"
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 150_000, (
            "Without interval signal, value must NOT be divided — "
            "store as-is for Phase 49.02 flagging"
        )
        assert result["salary_max"] == 200_000


# ---------------------------------------------------------------------------
# source_id emission (F-04)
# ---------------------------------------------------------------------------


class TestSourceId:
    def test_source_id_extracted_as_string(self):
        """Top-level `id` integer → source_id string."""
        posting = {
            "id": 4647776006,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] == "4647776006"

    def test_source_id_none_when_id_absent(self):
        """Missing `id` → source_id is None (not empty string)."""
        posting = {
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_none(self):
        posting = {
            "id": None,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] is None


# ---------------------------------------------------------------------------
# posted_date emission (F-02)
# ---------------------------------------------------------------------------


class TestPostedDate:
    def test_posted_date_from_first_published(self):
        """first_published ISO-8601 string → posted_date (#360)."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "first_published": "2024-01-10T08:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] == "2024-01-10T08:00:00Z"

    def test_updated_at_is_ignored(self):
        """updated_at is last-modified, never used for posted_date (#360)."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "updated_at": "2024-02-01T00:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] is None

    def test_first_published_wins_over_updated_at(self):
        """When both are present, first_published wins (#360)."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "updated_at": "2024-02-01T00:00:00Z",
            "first_published": "2024-01-01T00:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] == "2024-01-01T00:00:00Z"

    def test_posted_date_none_when_first_published_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] is None


# ---------------------------------------------------------------------------
# JobLocation / locations_structured emission
# ---------------------------------------------------------------------------


class TestJobLocationEmission:
    def test_locations_structured_present_in_result(self):
        """_posting_to_job must include locations_structured key."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert "locations_structured" in result

    def test_locations_structured_is_list(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Seattle, WA"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert isinstance(result["locations_structured"], list)

    def test_locations_structured_entries_are_joblocation(self):
        """When parse_locations succeeds, entries are JobLocation instances."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        locs = result["locations_structured"]
        if locs:  # non-empty parse result
            assert all(isinstance(loc, JobLocation) for loc in locs)

    def test_locations_structured_empty_when_location_dict_empty(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_location_key_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_location_is_none(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": None,
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_to_canonical_remote_location(self):
        """'Remote' string → at least one entry with workplace_type REMOTE."""
        locs = _to_canonical({"location": {"name": "Remote"}})
        assert isinstance(locs, list)
        if locs:
            assert any(loc.workplace_type == "REMOTE" for loc in locs)

    def test_to_canonical_missing_location_returns_empty(self):
        assert _to_canonical({}) == []
        assert _to_canonical({"location": None}) == []
        assert _to_canonical({"location": {"name": ""}}) == []


# ---------------------------------------------------------------------------
# Interval field alias: "interval" key as well as "unit"
# ---------------------------------------------------------------------------


class TestIntervalFieldAlias:
    """Some Greenhouse responses use "interval" instead of "unit"."""

    def test_interval_key_treated_same_as_unit_for_annual_cents(self):
        posting = {
            "id": 1,
            "title": "Eng",
            "location": {"name": "SF"},
            "absolute_url": "https://example.com",
            "pay_input_ranges": [
                {
                    "min_cents": 10_000_000,
                    "max_cents": 15_000_000,
                    "interval": "year",  # uses "interval", not "unit"
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 100_000
        assert result["salary_max"] == 150_000

    def test_interval_key_hourly_not_divided(self):
        posting = {
            "id": 1,
            "title": "Eng",
            "location": {"name": "SF"},
            "absolute_url": "https://example.com",
            "pay_input_ranges": [
                {
                    "min_cents": 45,
                    "max_cents": 55,
                    "interval": "hour",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 45
        assert result["salary_max"] == 55
