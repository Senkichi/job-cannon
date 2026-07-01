"""Tests for scripts/marquee_audit.py — the marquee coverage audit tool.

Verifies the improved matcher logic and company name mapping.
"""

from __future__ import annotations

import sqlite3

from scripts.marquee_audit import (
    audit_company,
    extract_req_id_from_url,
    gt_name_to_company,
    is_target_role,
    match_role,
    normalize_title,
    render_report,
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


def test_match_role_consumed_tracking():
    """Role matching tracks consumed jobs to prevent double-counting (regression for Finding 4)."""
    gt_role1 = {"title": "Data Scientist", "req_id": "JR1", "url": None}
    gt_role2 = {"title": "Data Analyst", "req_id": "JR2", "url": None}
    our_jobs = [
        {"title": "Data Scientist", "source_id": "JR1", "source_urls_raw": "[]"},
    ]

    consumed = set()
    result1 = match_role(gt_role1, our_jobs, consumed)
    assert result1.matched is True
    assert len(consumed) == 1

    # Second GT role should not match the already-consumed job
    result2 = match_role(gt_role2, our_jobs, consumed)
    assert result2.matched is False


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


def test_match_role_by_url_req_id_extraction():
    """Role matching extracts req_id from job URLs and compares to GT req_id."""
    gt_role = {
        "title": "Senior Data Scientist",
        "req_id": "JR2019886",
        "url": "https://nvidia.wd5.myworkdayjobs.com/job/US-CA-Santa-Clara/Senior-Data-Scientist_JR2019886",
    }
    # Job URL has location segment, GT URL doesn't - but req_id should match
    our_jobs = [
        {
            "title": "Senior Data Scientist",
            "source_id": None,
            "source_urls_raw": '["https://nvidia.wd5.myworkdayjobs.com/job/Senior-Data-Scientist_JR2019886"]',
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


def test_match_role_intern_disqualifier():
    """Role matching disqualifies intern vs non-intern mismatches (regression for Finding 4)."""
    gt_role = {"title": "Senior Data Scientist, Ads Ranking", "req_id": None, "url": None}
    our_jobs = [
        {
            "title": "Data Scientist, Ads Ranking - Intern",
            "source_id": None,
            "source_urls_raw": "[]",
        },
    ]

    result = match_role(gt_role, our_jobs)
    assert result.matched is False  # Intern should not match non-intern GT role


def test_match_role_fuzzy_threshold():
    """Role matching uses fuzzy threshold instead of substring containment (regression for Finding 4)."""
    gt_role = {"title": "Data Scientist", "req_id": None, "url": None}
    our_jobs = [
        {
            "title": "Senior Data Scientist, Machine Learning, Ads Ranking",
            "source_id": None,
            "source_urls_raw": "[]",
        },
    ]

    result = match_role(gt_role, our_jobs)
    # Should match via fuzzy threshold (token_set_ratio >= 70)
    assert result.matched is True
    assert result.match_method == "title_fuzzy"


def test_gt_name_to_company_exact_match():
    """Company mapping prefers exact match."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT)")
    conn.execute("INSERT INTO companies (name_raw) VALUES ('Intel')")
    conn.execute("INSERT INTO companies (name_raw) VALUES ('Intelsio')")
    conn.commit()

    result = gt_name_to_company(conn, "Intel")
    assert len(result.company_ids) == 1
    assert result.note == "exact match on 'Intel'"


def test_gt_name_to_company_ambiguous_prefix():
    """Company mapping flags ambiguity when multiple companies share a prefix (regression for Finding 3)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT)")
    # Use a GT name that doesn't have an exact match but has prefix matches
    conn.execute("INSERT INTO companies (name_raw) VALUES ('Salesforce Inc')")
    conn.execute("INSERT INTO companies (name_raw) VALUES ('SalesForce-ad')")
    conn.commit()

    result = gt_name_to_company(conn, "Salesforce")
    # Should flag ambiguity rather than silently pooling
    assert len(result.company_ids) == 2
    assert "AMBIGUOUS" in result.note
    assert "Salesforce Inc" in result.note
    assert "SalesForce-ad" in result.note


def test_gt_name_to_company_not_found():
    """Company mapping returns empty list when not found."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT)")
    conn.commit()

    result = gt_name_to_company(conn, "NonExistent Company")
    assert len(result.company_ids) == 0
    assert result.note == "NOT in companies"


def test_audit_company_verdict_branches():
    """audit_company produces correct verdicts for different scenarios (regression for Finding 6)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE companies (id INTEGER PRIMARY KEY, name_raw TEXT, ats_platform TEXT)"
    )
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, source_id TEXT, source_urls_raw TEXT, company_id INTEGER)"
    )
    conn.execute("INSERT INTO companies (name_raw, ats_platform) VALUES ('TestCorp', 'workday')")
    conn.execute(
        "INSERT INTO jobs (title, source_id, source_urls_raw, company_id) VALUES ('Data Scientist', 'JR1', '[]', 1)"
    )
    conn.execute(
        "INSERT INTO jobs (title, source_id, source_urls_raw, company_id) VALUES ('Software Engineer', 'JR2', '[]', 1)"
    )
    conn.commit()

    gt_entry = {
        "company": "TestCorp",
        "role_count": 10,
        "roles": [
            {"title": "Data Scientist", "req_id": "JR1", "url": None},
        ],
        "confidence": "high",
    }

    result = audit_company(conn, gt_entry)
    assert result.company == "TestCorp"
    assert result.gt_role_count == 10
    assert result.our_target_count == 1
    assert result.sample_matched == 1
    assert result.platform == "workday"
    assert "fully covered" in result.verdict


def test_render_report():
    """render_report produces human-readable output (regression for Finding 6)."""
    from scripts.marquee_audit import AuditResult

    results = [
        AuditResult(
            company="TestCorp",
            gt_role_count=10,
            our_target_count=5,
            sample_size=2,
            sample_matched=1,
            platform="workday",
            verdict="partial: 1 sample role not found",
            confidence="high",
            match_methods={"req_id": 1, "none": 1},
            missed_details=["Data Analyst"],
        )
    ]

    report = render_report(results)
    assert "TestCorp" in report
    assert "10" in report
    assert "5" in report
    assert "1/2" in report
    assert "workday" in report
    assert "Data Analyst" in report
