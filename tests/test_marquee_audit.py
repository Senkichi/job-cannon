"""Tests for scripts/marquee_audit.py — the marquee coverage audit tool.

Verifies the improved matcher logic and company name mapping.
"""

from __future__ import annotations

import sqlite3

from scripts.marquee_audit import (
    extract_req_id_from_url,
    gt_name_to_company,
    is_target_role,
    match_role,
    normalize_title,
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
    assert (
        extract_req_id_from_url(
            "/job/US-CA-Santa-Clara/Manager--GPU-Accelerated-Data-Analytics_JR2011456-1"
        )
        == "JR2011456-1"
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
    """Title normalization strips punctuation and seniority tokens."""
    assert normalize_title("Senior Data Scientist") == "data scientist"
    assert normalize_title("Staff Data Scientist, Product") == "data scientist product"
    assert (
        normalize_title("Principal Data Scientist - Cloud Gaming") == "data scientist cloud gaming"
    )  # "Principal" is a seniority token
    assert normalize_title("Sr. Data Analyst") == "data analyst"
    assert (
        normalize_title("Manager, GPU Accelerated Data Analytics")
        == "manager gpu accelerated data analytics"
    )


def test_is_target_role():
    """Target role matching uses the keyword regex."""
    assert is_target_role("Data Scientist") is True
    assert is_target_role("Senior Data Analyst") is True
    assert is_target_role("Business Analyst") is True
    assert is_target_role("Research Scientist") is True
    assert is_target_role("Machine Learning Engineer") is True
    assert is_target_role("Software Engineer") is False
    assert is_target_role("Product Manager") is False
    assert is_target_role("") is False


def test_match_role_by_req_id():
    """Role matching prefers req_id over other methods."""
    gt_role = {
        "title": "Data Scientist",
        "req_id": "JR2019886",
        "url": "https://example.com/job/JR2019886",
    }
    our_jobs = [
        {"title": "Data Scientist", "source_id": "JR2019886", "source_urls_raw": "[]"},
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is True
    assert result.match_method == "req_id"
    assert result.our_title == "Data Scientist"


def test_match_role_by_req_id_in_path():
    """Role matching extracts req_id from source_id path."""
    gt_role = {
        "title": "Data Scientist",
        "req_id": "JR2019886",
        "url": "https://example.com/job/JR2019886",
    }
    our_jobs = [
        {
            "title": "Data Scientist",
            "source_id": "/job/US-CA-Santa-Clara/Data-Scientist_JR2019886",
            "source_urls_raw": "[]",
        },
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is True
    assert result.match_method == "req_id"


def test_match_role_by_url():
    """Role matching falls back to URL match when req_id fails."""
    gt_role = {
        "title": "Data Scientist",
        "req_id": "DIFFERENT",
        "url": "https://example.com/job/123",
    }
    our_jobs = [
        {
            "title": "Data Scientist",
            "source_id": None,
            "source_urls_raw": '["https://example.com/job/123"]',
        },
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is True
    assert result.match_method == "url"


def test_match_role_by_title_fuzzy():
    """Role matching falls back to fuzzy title match when req_id/URL fail."""
    gt_role = {"title": "Senior Data Scientist", "req_id": None, "url": None}
    our_jobs = [
        {"title": "Senior Data Scientist", "source_id": None, "source_urls_raw": "[]"},
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is True
    assert result.match_method == "title_fuzzy"


def test_match_role_no_match():
    """Role matching returns no match when all methods fail."""
    gt_role = {"title": "Software Engineer", "req_id": None, "url": None}
    our_jobs = [
        {"title": "Data Scientist", "source_id": None, "source_urls_raw": "[]"},
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is False
    assert result.match_method == "none"


def test_gt_name_to_company_intel_special_case():
    """Intel mapping avoids matching Intelsio."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT)")
    conn.execute("INSERT INTO companies (name_raw) VALUES ('Intel')")
    conn.execute("INSERT INTO companies (name_raw) VALUES ('Intelsio')")
    conn.commit()

    result = gt_name_to_company(conn, "Intel")
    assert len(result.company_ids) == 1
    assert result.note == "exact 'Intel' only (avoid Intelsio)"


def test_gt_name_to_company_not_found():
    """Company mapping returns empty list when not found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT)")
    conn.commit()

    result = gt_name_to_company(conn, "NonExistent Company")
    assert len(result.company_ids) == 0
    assert result.note == "NOT in companies"
