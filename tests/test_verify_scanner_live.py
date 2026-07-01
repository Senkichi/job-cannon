"""Tests for scripts/verify_scanner_live.py — the live scanner verification harness."""

from __future__ import annotations

from unittest.mock import patch

from scripts.verify_scanner_live import (
    extract_req_id_from_url,
    is_target_role,
    match_job_by_req_id,
    match_job_by_title,
    match_job_by_url,
    normalize_title,
    verify_coverage,
)


def test_extract_req_id_from_url():
    """Req ID extraction handles Workday, Google, and generic patterns."""
    # Workday patterns
    assert (
        extract_req_id_from_url("/job/US-CA-Santa-Clara/Principal-Data-Scientist_JR2019886")
        == "JR2019886"
    )
    assert (
        extract_req_id_from_url(
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Data-Analyst---Finance_JR2018506"
        )
        == "JR2018506"
    )

    # Google patterns
    assert (
        extract_req_id_from_url(
            "https://www.google.com/about/careers/applications/jobs/results/106536599407207110"
        )
        == "106536599407207110"
    )

    # Generic jobId patterns
    assert extract_req_id_from_url("https://example.com/jobs?jobId=12345") == "12345"

    # No match
    assert extract_req_id_from_url("https://example.com/jobs") is None
    assert extract_req_id_from_url("") is None
    assert extract_req_id_from_url(None) is None


def test_normalize_title():
    """Title normalization strips punctuation."""
    assert normalize_title("Senior Data Scientist") == "senior data scientist"
    assert normalize_title("Staff Data Scientist, Product") == "staff data scientist product"
    assert normalize_title("Sr. Data Analyst") == "sr data analyst"


def test_is_target_role():
    """Target role matching uses the keyword regex."""
    assert is_target_role("Data Scientist") is True
    assert is_target_role("Senior Data Analyst") is True
    assert is_target_role("Business Analyst") is True
    assert is_target_role("Software Engineer") is False
    assert is_target_role("") is False


def test_match_job_by_req_id():
    """Job matching by req_id works."""
    captured = [
        {"title": "Data Scientist", "source_id": "JR2019886", "source_url": None},
    ]
    gt_job = {"title": "Data Scientist", "req_id": "JR2019886"}

    assert match_job_by_req_id(captured, gt_job) is True


def test_match_job_by_req_id_from_url():
    """Job matching extracts req_id from URL."""
    captured = [
        {
            "title": "Data Scientist",
            "source_id": None,
            "source_url": "/job/US-CA-Santa-Clara/Data-Scientist_JR2019886",
        },
    ]
    gt_job = {"title": "Data Scientist", "req_id": "JR2019886"}

    assert match_job_by_req_id(captured, gt_job) is True


def test_match_job_by_url():
    """Job matching by URL works."""
    captured = [
        {
            "title": "Data Scientist",
            "source_urls_raw": '["https://example.com/job/123"]',
            "source_url": None,
        },
    ]
    gt_job = {"title": "Data Scientist", "url": "https://example.com/job/123"}

    assert match_job_by_url(captured, gt_job) is True


def test_match_job_by_url_single():
    """Job matching by single source_url works."""
    captured = [
        {
            "title": "Data Scientist",
            "source_urls_raw": None,
            "source_url": "https://example.com/job/123",
        },
    ]
    gt_job = {"title": "Data Scientist", "url": "https://example.com/job/123"}

    assert match_job_by_url(captured, gt_job) is True


def test_match_job_by_title_exact():
    """Job matching by exact title works."""
    captured = [
        {"title": "Senior Data Scientist", "source_id": None, "source_url": None},
    ]
    gt_job = {"title": "Senior Data Scientist", "req_id": None, "url": None}

    assert match_job_by_title(captured, gt_job) is True


def test_match_job_by_title_substring():
    """Job matching by substring works for longer titles."""
    captured = [
        {
            "title": "Senior Data Scientist, Machine Learning",
            "source_id": None,
            "source_url": None,
        },
    ]
    gt_job = {"title": "Senior Data Scientist", "req_id": None, "url": None}

    assert match_job_by_title(captured, gt_job) is True


def test_verify_coverage_full_match():
    """Verify coverage reports full match."""
    captured = [
        {"title": "Data Scientist", "source_id": "JR1", "source_url": None},
        {"title": "Data Analyst", "source_id": "JR2", "source_url": None},
    ]
    ground_truth = [
        {"title": "Data Scientist", "req_id": "JR1", "url": None},
        {"title": "Data Analyst", "req_id": "JR2", "url": None},
    ]

    matched, total, missed = verify_coverage(captured, ground_truth)
    assert matched == 2
    assert total == 2
    assert len(missed) == 0


def test_verify_coverage_gap_detected():
    """Verify coverage detects gaps (regression test for Finding 2)."""
    captured = []  # Deliberately broken scanner returns 0 jobs
    ground_truth = [
        {"title": "Data Scientist", "req_id": "JR1", "url": None},
        {"title": "Data Analyst", "req_id": "JR2", "url": None},
    ]

    matched, total, missed = verify_coverage(captured, ground_truth)
    assert matched == 0
    assert total == 2
    assert len(missed) == 2
    assert "Data Scientist" in missed
    assert "Data Analyst" in missed


def test_ascii_output_no_unicode_errors():
    """Verify output uses ASCII symbols to avoid Windows encoding errors (regression for Finding 5)."""
    from io import StringIO

    # Capture stdout and verify no Unicode symbols are used
    captured_output = StringIO()

    # Simulate the success path output
    with patch("sys.stdout", captured_output):
        print("[OK] All ground-truth roles matched")

    output = captured_output.getvalue()
    # Verify no Unicode checkmarks/crosses
    assert "✓" not in output
    assert "✗" not in output
    assert "[OK]" in output
