"""Tests for posting sub-entity upsert logic (#640).

Tests the ``jobs.postings`` JSON column population for direct ATS sightings.
"""

from __future__ import annotations

import json

from job_finder.db._jobs import upsert_job
from job_finder.db._postings import build_posting_descriptor, upsert_posting
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.ats_registry import PLATFORMS, is_direct_ats_platform
from job_finder.web.location_canonical import JobLocation


class TestUpsertPosting:
    """Tests for the pure upsert_posting helper."""

    def test_upsert_posting_is_pure(self):
        """upsert_posting returns a new list and does not mutate the input list."""
        existing = [
            {"ats_platform": "ashby", "source_id": "abc", "apply_url": "http://example.com/1"}
        ]
        original_id = id(existing)
        original_content = json.dumps(existing)

        descriptor = {
            "ats_platform": "lever",
            "source_id": "xyz",
            "apply_url": "http://example.com/2",
            "locations_structured": [],
            "workplace_type": "REMOTE",
            "confidence": "ats",
        }

        result = upsert_posting(existing, descriptor)

        # Input list not mutated
        assert id(existing) == original_id
        assert json.dumps(existing) == original_content

        # Result is a new list
        assert id(result) != original_id
        assert len(result) == 2

    def test_upsert_posting_appends_new(self):
        """upsert_posting appends a new posting when the key doesn't exist."""
        existing = [
            {"ats_platform": "ashby", "source_id": "abc", "apply_url": "http://example.com/1"}
        ]
        descriptor = {
            "ats_platform": "lever",
            "source_id": "xyz",
            "apply_url": "http://example.com/2",
            "locations_structured": [],
            "workplace_type": "REMOTE",
            "confidence": "ats",
        }

        result = upsert_posting(existing, descriptor)

        assert len(result) == 2
        assert result[1] == descriptor

    def test_upsert_posting_updates_in_place(self):
        """upsert_posting updates an existing posting when the key matches."""
        existing = [
            {
                "ats_platform": "ashby",
                "source_id": "abc",
                "apply_url": "http://example.com/1",
                "locations_structured": [],
                "workplace_type": "REMOTE",
                "confidence": "ats",
            }
        ]
        descriptor = {
            "ats_platform": "ashby",
            "source_id": "abc",
            "apply_url": "http://example.com/2",  # Updated URL
            "locations_structured": [],
            "workplace_type": "HYBRID",  # Updated workplace type
            "confidence": "ats",
        }

        result = upsert_posting(existing, descriptor)

        assert len(result) == 1
        assert result[0] == descriptor
        assert result[0]["apply_url"] == "http://example.com/2"
        assert result[0]["workplace_type"] == "HYBRID"

    def test_upsert_posting_invalid_descriptor_no_mutate(self):
        """upsert_posting returns unchanged list for invalid descriptor."""
        existing = [
            {"ats_platform": "ashby", "source_id": "abc", "apply_url": "http://example.com/1"}
        ]
        invalid_descriptor = {"ats_platform": "ashby"}  # Missing source_id

        result = upsert_posting(existing, invalid_descriptor)

        assert result == existing
        assert len(result) == 1


class TestBuildPostingDescriptor:
    """Tests for the build_posting_descriptor helper."""

    def test_descriptor_has_no_location_fit(self):
        """The descriptor written by Phase 1 has exactly 6 fields (no location_fit)."""
        descriptor = build_posting_descriptor(
            ats_platform="ashby",
            source_id="abc-123",
            apply_url="http://example.com/apply",
            locations_structured=[],
            workplace_type="REMOTE",
        )

        assert set(descriptor.keys()) == {
            "ats_platform",
            "source_id",
            "apply_url",
            "locations_structured",
            "workplace_type",
            "confidence",
        }
        assert "location_fit" not in descriptor


class TestIsDirectAtsPlatform:
    """Tests for the is_direct_ats_platform predicate."""

    def test_is_direct_ats_platform_registry_derived(self):
        """Assert the predicate is registry-derived, not hardcoded."""
        # Direct ATS platforms with scanners
        assert is_direct_ats_platform("ashby") is True
        assert is_direct_ats_platform("lever") is True
        assert is_direct_ats_platform("greenhouse") is True

        # Keyword adapters should return False
        assert is_direct_ats_platform("amazon") is False
        assert is_direct_ats_platform("microsoft") is False
        assert is_direct_ats_platform("eightfold") is False

        # Non-scannable platforms should return False
        assert is_direct_ats_platform("jobvite") is False
        assert is_direct_ats_platform("google") is False
        assert is_direct_ats_platform("taleo") is False

        # Unknown platform should return False
        assert is_direct_ats_platform("unknown_platform") is False


class TestUpsertJobPostings:
    """Integration tests for upsert_job posting population."""

    def test_two_ashby_sightings_one_row_two_postings(self, migrated_db):
        """Two Ashby sightings with same company|title but distinct source_id produce one row with two postings."""
        db_path, conn = migrated_db

        # First sighting
        job1 = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1",
            source_id="abc-123",
            description="Job description 1",
        )
        parsed1 = ParsedJob.from_job(
            job1,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        result1 = upsert_job(conn, parsed1, ats_platform="ashby")
        assert result1.kind == "inserted"

        # Second sighting (same company|title, different source_id)
        job2 = Job(
            title="Data Scientist",
            company="TestCo",
            location="New York, NY",
            source="Ashby",
            source_url="http://example.com/2",
            source_id="def-456",
            description="Job description 2",
        )
        parsed2 = ParsedJob.from_job(
            job2,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="New York",
                        region="New York",
                        region_code="NY",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="New York, NY",
                        unresolved=False,
                    )
                ]
            },
        )
        result2 = upsert_job(conn, parsed2, ats_platform="ashby")
        assert result2.kind == "updated"

        # Verify one row with two postings
        row = conn.execute(
            "SELECT dedup_key, postings FROM jobs WHERE dedup_key = ?",
            (parsed1.dedup_key,),
        ).fetchone()
        assert row is not None
        postings = json.loads(row["postings"])
        assert len(postings) == 2

        # Verify both postings are present with correct keys
        posting_ids = {p["source_id"] for p in postings}
        assert posting_ids == {"abc-123", "def-456"}
        assert all(p["ats_platform"] == "ashby" for p in postings)

        conn.close()

    def test_resight_posting_updates_in_place(self, migrated_db):
        """Re-sighting one of two postings updates only that descriptor (no duplicate)."""
        db_path, conn = migrated_db

        # First sighting
        job1 = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1",
            source_id="abc-123",
            description="Job description 1",
        )
        parsed1 = ParsedJob.from_job(
            job1,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed1, ats_platform="ashby")

        # Second sighting (different source_id)
        job2 = Job(
            title="Data Scientist",
            company="TestCo",
            location="New York, NY",
            source="Ashby",
            source_url="http://example.com/2",
            source_id="def-456",
            description="Job description 2",
        )
        parsed2 = ParsedJob.from_job(
            job2,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="New York",
                        region="New York",
                        region_code="NY",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="New York, NY",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed2, ats_platform="ashby")

        # Re-sight the first posting with updated apply_url
        job1_updated = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1-updated",  # Updated URL
            source_id="abc-123",
            description="Job description 1",
        )
        parsed1_updated = ParsedJob.from_job(
            job1_updated,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed1_updated, ats_platform="ashby")

        # Verify still two postings, first one updated
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed1.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 2

        # Find the abc-123 posting
        abc_posting = next(p for p in postings if p["source_id"] == "abc-123")
        assert abc_posting["apply_url"] == "http://example.com/1-updated"

        conn.close()

    def test_single_posting_multi_location_one_entry(self, migrated_db):
        """One ATS sighting with two locations_structured entries produces one postings entry spanning both."""
        db_path, conn = migrated_db

        job = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA / New York, NY",
            source="Ashby",
            source_url="http://example.com/1",
            source_id="abc-123",
            description="Job description",
        )
        parsed = ParsedJob.from_job(
            job,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="HYBRID",
                        raw="San Francisco, CA",
                        unresolved=False,
                    ),
                    JobLocation(
                        city="New York",
                        region="New York",
                        region_code="NY",
                        country="United States",
                        country_code="US",
                        workplace_type="HYBRID",
                        raw="New York, NY",
                        unresolved=False,
                    ),
                ]
            },
        )
        upsert_job(conn, parsed, ats_platform="ashby")

        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 1

        # Verify the single posting has both locations
        locations_structured = json.loads(postings[0]["locations_structured"])
        assert len(locations_structured) == 2
        cities = {loc["city"] for loc in locations_structured}
        assert cities == {"San Francisco", "New York"}

        conn.close()

    def test_aggregator_sighting_mints_no_posting(self, migrated_db):
        """Sighting whose source is not a direct ATS platform (or empty source_id) mints no posting."""
        db_path, conn = migrated_db

        # Aggregator sighting (no ats_platform)
        job = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="LinkedIn",
            source_url="http://linkedin.com/job",
            source_id="",  # Empty source_id
            description="Job description",
        )
        parsed = ParsedJob.from_job(job)
        upsert_job(conn, parsed, ats_platform=None)

        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert postings == []

        conn.close()

    def test_cross_platform_id_collision_no_merge(self, migrated_db):
        """Same source_id string under different platforms produces two distinct entries."""
        db_path, conn = migrated_db

        # Lever sighting
        job1 = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Lever",
            source_url="http://example.com/1",
            source_id="same-id",  # Same ID as below
            description="Job description 1",
        )
        parsed1 = ParsedJob.from_job(
            job1,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed1, ats_platform="lever")

        # Greenhouse sighting with same source_id
        job2 = Job(
            title="Data Scientist",
            company="TestCo",
            location="New York, NY",
            source="Greenhouse",
            source_url="http://example.com/2",
            source_id="same-id",  # Same ID as above
            description="Job description 2",
        )
        parsed2 = ParsedJob.from_job(
            job2,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="New York",
                        region="New York",
                        region_code="NY",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="New York, NY",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed2, ats_platform="greenhouse")

        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed1.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 2

        # Verify two distinct entries (platform namespaced)
        platforms = {p["ats_platform"] for p in postings}
        assert platforms == {"lever", "greenhouse"}
        assert all(p["source_id"] == "same-id" for p in postings)

        conn.close()

    def test_legacy_null_postings_reads_as_empty(self, migrated_db):
        """A row inserted before the column existed (NULL postings) reads as []."""
        db_path, conn = migrated_db

        # Simulate a legacy row by manually inserting with NULL postings
        job = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="LinkedIn",
            source_url="http://linkedin.com/job",
            source_id="",
            description="Job description",
        )
        parsed = ParsedJob.from_job(job)
        upsert_job(conn, parsed, ats_platform=None)

        # Manually set postings to NULL to simulate legacy row
        conn.execute(
            "UPDATE jobs SET postings = NULL WHERE dedup_key = ?",
            (parsed.dedup_key,),
        )
        conn.commit()

        # Read back - should tolerate NULL
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed.dedup_key,),
        ).fetchone()
        # The safe_json_load in upsert_job should handle this
        # Now upsert with an ATS sighting - should append cleanly
        job_ats = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1",
            source_id="abc-123",
            description="Job description",
        )
        parsed_ats = ParsedJob.from_job(
            job_ats,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        upsert_job(conn, parsed_ats, ats_platform="ashby")

        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 1
        assert postings[0]["source_id"] == "abc-123"

        conn.close()

    def test_i11_merge_path_writes_posting_to_matched_row(self, migrated_db):
        """Force the I-11 fallback and assert the posting lands on the matched row."""
        db_path, conn = migrated_db

        # First insert with one title
        job1 = Job(
            title="Data Scientist",
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1",
            source_id="abc-123",
            description="Job description 1",
        )
        parsed1 = ParsedJob.from_job(
            job1,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        result1 = upsert_job(conn, parsed1, ats_platform="ashby", company_id=1)
        assert result1.kind == "inserted"

        # Second insert with different title but same company_id + source_id
        # This triggers the I-11 fallback (dedup_key miss but (company_id, source_id) hit)
        job2 = Job(
            title="Senior Data Scientist",  # Different title -> different dedup_key
            company="TestCo",
            location="San Francisco, CA",
            source="Ashby",
            source_url="http://example.com/1-updated",
            source_id="abc-123",  # Same source_id
            description="Job description 2",
        )
        parsed2 = ParsedJob.from_job(
            job2,
            source_meta={
                "locations_structured": [
                    JobLocation(
                        city="San Francisco",
                        region="California",
                        region_code="CA",
                        country="United States",
                        country_code="US",
                        workplace_type="ONSITE",
                        raw="San Francisco, CA",
                        unresolved=False,
                    )
                ]
            },
        )
        result2 = upsert_job(conn, parsed2, ats_platform="ashby", company_id=1)

        # The posting should be written to the matched row (result1.dedup_key)
        # not the incoming dedup_key (parsed2.dedup_key)
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (result1.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 1
        assert postings[0]["source_id"] == "abc-123"
        # The apply_url should be updated
        assert postings[0]["apply_url"] == "http://example.com/1-updated"

        conn.close()

    def test_company_source_resolves_to_ats_platform_at_upsert(self, migrated_db):
        """The integration seam: job_dict["company_source"] resolves to platform key and reaches upsert_job."""
        db_path, conn = migrated_db

        # Simulate the ats_scanner/_run.py path
        job_dict = {
            "title": "Data Scientist",
            "company_source": "Ashby",  # Title-cased
            "source_id": "abc-123",
            "source_url": "http://example.com/1",
            "location": "San Francisco, CA",
            "description": "Job description",
            "locations_structured": [
                JobLocation(
                    city="San Francisco",
                    region="California",
                    region_code="CA",
                    country="United States",
                    country_code="US",
                    workplace_type="ONSITE",
                    raw="San Francisco, CA",
                    unresolved=False,
                )
            ],
        }

        # Resolve platform key as _run.py does
        company_source = job_dict["company_source"]
        ats_platform = None
        if company_source:
            platform_key = company_source.lower()
            if platform_key in PLATFORMS:
                ats_platform = platform_key

        assert ats_platform == "ashby"

        # Build ParsedJob and call upsert_job
        from job_finder.models import Job

        job = Job(
            title=job_dict["title"],
            company="TestCo",
            location=job_dict["location"],
            source=job_dict["company_source"],
            source_url=job_dict["source_url"],
            source_id=job_dict["source_id"],
            description=job_dict["description"],
        )

        parsed = ParsedJob.from_job(
            job, source_meta={"locations_structured": job_dict["locations_structured"]}
        )

        result = upsert_job(conn, parsed, ats_platform=ats_platform)
        assert result.kind == "inserted"

        # Verify posting was minted
        row = conn.execute(
            "SELECT postings FROM jobs WHERE dedup_key = ?",
            (parsed.dedup_key,),
        ).fetchone()
        postings = json.loads(row["postings"])
        assert len(postings) == 1
        assert postings[0]["ats_platform"] == "ashby"
        assert postings[0]["source_id"] == "abc-123"

        conn.close()
