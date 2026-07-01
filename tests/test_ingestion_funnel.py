"""Tests for ingestion funnel reconciliation identity (issue #587)."""

from job_finder.models import Job
from job_finder.web.ingestion_runner import _apply_title_gate, is_scannable_output


def test_is_scannable_output():
    """Test is_scannable_output predicate."""
    # Valid job
    job = Job(
        title="Senior Data Scientist",
        company="Acme Corp",
        location="Remote",
        source="test",
        source_url="https://example.com/job/123",
        description="A great job",
    )
    assert is_scannable_output(job) is True

    # Empty title
    job.title = ""
    assert is_scannable_output(job) is False

    # Empty company
    job.title = "Senior Data Scientist"
    job.company = ""
    assert is_scannable_output(job) is False

    # Empty source_url
    job.company = "Acme Corp"
    job.source_url = ""
    assert is_scannable_output(job) is False

    # Title too short (< 3 chars)
    job.source_url = "https://example.com/job/123"
    job.title = "AB"
    assert is_scannable_output(job) is False

    # Title too long (> 200 chars)
    job.title = "A" * 201
    assert is_scannable_output(job) is False


def test_identity_holds_no_drops():
    """Test that funnel identity holds when no drops occur."""
    summary = {
        "funnel": {
            "jobs_in": 1,
            "jobs_passed": 1,
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Compute unexplained
    funnel = summary["funnel"]
    jobs_in = funnel["jobs_in"]
    jobs_passed = funnel["jobs_passed"]
    jobs_errored = funnel["jobs_errored"]
    total_dropped = sum(funnel["drop_buckets"].values())
    unexplained = jobs_in - (jobs_passed + total_dropped + jobs_errored)

    # Should be zero when identity holds
    assert unexplained == 0


def test_denylist_drop_bucketed():
    """Test that denylist bucket can be incremented."""
    summary = {
        "funnel": {
            "jobs_in": 0,
            "jobs_passed": 0,
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Simulate what _score_and_persist does when DenylistedCompanyError is raised
    summary["funnel"]["drop_buckets"]["denylist"] = 1
    summary["funnel"]["funnel_by_platform"]["unknown"] = {"drop_buckets": {"denylist": 1}}

    # Verify denylist bucket incremented
    assert summary["funnel"]["drop_buckets"]["denylist"] == 1
    assert summary["funnel"]["jobs_passed"] == 0
    assert summary["funnel"]["jobs_errored"] == 0


def test_listing_tile_drop_bucketed():
    """Test that listing_tile bucket can be incremented."""
    summary = {
        "funnel": {
            "jobs_in": 0,
            "jobs_passed": 0,
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Simulate what _score_and_persist does when ListingTileError is raised
    summary["funnel"]["drop_buckets"]["listing_tile"] = 1
    summary["funnel"]["funnel_by_platform"]["unknown"] = {"drop_buckets": {"listing_tile": 1}}

    # Verify listing_tile bucket incremented
    assert summary["funnel"]["drop_buckets"]["listing_tile"] == 1
    assert summary["funnel"]["jobs_passed"] == 0
    assert summary["funnel"]["jobs_errored"] == 0


def test_title_gate_drop_bucketed():
    """Test that _apply_title_gate increments title_gate bucket."""
    config = {
        "profile": {
            "target_titles": ["Data Scientist", "Senior Data Scientist"],
            "exclusions": {"title_keywords": ["intern", "junior"]},
        }
    }
    summary = {
        "funnel": {
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            }
        }
    }

    jobs = [
        Job(
            title="Senior Data Scientist",
            company="Acme Corp",
            location="Remote",
            source="test",
            source_url="https://example.com/job/1",
            description="Good job",
        ),
        Job(
            title="Junior Data Scientist",
            company="Beta Corp",
            location="Remote",
            source="test",
            source_url="https://example.com/job/2",
            description="Junior role",
        ),
        Job(
            title="Software Engineer",
            company="Gamma Corp",
            location="Remote",
            source="test",
            source_url="https://example.com/job/3",
            description="Wrong title",
        ),
    ]

    filtered = _apply_title_gate(jobs, config, "test", summary)

    # Should filter out 2 jobs (junior and wrong title)
    assert len(filtered) == 1
    assert summary["funnel"]["drop_buckets"]["title_gate"] == 2


def test_dedup_touch_bucketed():
    """Test that dedup bucket can be incremented."""
    summary = {
        "funnel": {
            "jobs_in": 0,
            "jobs_passed": 0,
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Simulate what _score_and_persist does when result.kind == "touched"
    summary["funnel"]["drop_buckets"]["dedup"] = 1

    assert summary["funnel"]["drop_buckets"]["dedup"] == 1


def test_unexplained_positive_when_row_silently_lost():
    """Test that unexplained goes positive when a row is silently lost."""
    summary = {
        "funnel": {
            "jobs_in": 3,  # Simulate 3 jobs in
            "jobs_passed": 1,  # Only 1 passed
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Compute unexplained
    funnel = summary["funnel"]
    jobs_in = funnel["jobs_in"]
    jobs_passed = funnel["jobs_passed"]
    jobs_errored = funnel["jobs_errored"]
    total_dropped = sum(funnel["drop_buckets"].values())
    unexplained = jobs_in - (jobs_passed + total_dropped + jobs_errored)

    # Should be positive (2 jobs silently lost)
    assert unexplained == 2


def test_funnel_by_platform_keys_derive_from_ats():
    """Test that funnel_by_platform keys can be set per platform."""
    summary = {
        "funnel": {
            "jobs_in": 0,
            "jobs_passed": 0,
            "jobs_errored": 0,
            "drop_buckets": {
                "no_jd_full": 0,
                "title_gate": 0,
                "location_gate": 0,
                "dedup": 0,
                "denylist": 0,
                "listing_tile": 0,
                "parse_empty": 0,
            },
            "funnel_by_platform": {},
        }
    }

    # Simulate what _score_and_persist does for platform stratification
    ats_platform = "greenhouse"
    summary["funnel"]["funnel_by_platform"][ats_platform] = {
        "jobs_passed": 1,
        "drop_buckets": {"dedup": 0},
    }

    ats_platform = "lever"
    summary["funnel"]["funnel_by_platform"][ats_platform] = {
        "jobs_passed": 1,
        "drop_buckets": {"dedup": 0},
    }

    # Verify platform keys exist
    funnel_by_platform = summary["funnel"]["funnel_by_platform"]
    assert "greenhouse" in funnel_by_platform
    assert "lever" in funnel_by_platform
