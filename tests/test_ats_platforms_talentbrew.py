"""Tests for TalentBrew (by Radancy) ATS platform scanner."""

from job_finder.web.ats_platforms._platforms_talentbrew import SCANNER


def test_scanner_contract():
    """Test that the scanner has the required contract."""
    assert SCANNER.name == "talentbrew"
    assert SCANNER.company_source == "TalentBrew"
    assert callable(SCANNER.fetch_postings)
    assert callable(SCANNER.title_of)
    assert callable(SCANNER.posting_to_job)


def test_scanner_title_of():
    """Test title extraction from posting dict."""
    posting = {"title": "Software Engineer", "source_url": "http://example.com/job/1"}
    assert SCANNER.title_of(posting) == "Software Engineer"


def test_scanner_posting_to_job():
    """Test conversion to canonical job dict."""
    posting = {
        "title": "Software Engineer",
        "source_url": "https://careers.ford.com/job/12345/Software-Engineer",
        "source_id": "12345",
        "location": "Dearborn, Michigan",
        "description": "",  # Empty since we don't fetch detail pages
    }
    job = SCANNER.posting_to_job(posting, "careers.ford.com")
    assert job["title"] == "Software Engineer"
    assert job["company_source"] == "TalentBrew"
    assert job["location"] == "Dearborn, Michigan"
    assert job["source_url"] == "https://careers.ford.com/job/12345/Software-Engineer"
    assert job["source_id"] == "12345"
    assert job["description"] == ""
    assert job["jd_full"] == ""
