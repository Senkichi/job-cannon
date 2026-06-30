"""Tests for Phenom ATS platform scanner."""

from job_finder.web.ats_platforms._platforms_phenom import (
    SCANNER,
    _extract_job_id,
    _extract_job_urls,
    _extract_sitemap_urls,
    _extract_title_from_url,
)


def test_extract_job_id():
    """Test job ID extraction from Phenom URLs."""
    assert (
        _extract_job_id("https://careers.conduent.com/us/en/job/23003/Accounting-Analyst")
        == "23003"
    )
    assert _extract_job_id("https://careers.blackbaud.com/us/en/job/12345/Engineer") == "12345"
    assert _extract_job_id("https://example.com/not-a-job") is None


def test_extract_sitemap_urls():
    """Test sitemap URL extraction from sitemap index (captured fixture)."""
    with open("tests/fixtures/phenom_sitemap_index.xml", encoding="utf-8") as f:
        html = f.read()
    urls = _extract_sitemap_urls(html)
    assert len(urls) == 2
    assert "https://careers.conduent.com/us/en/sitemap1.xml" in urls
    assert "https://careers.conduent.com/us/en/sitemap2.xml" in urls


def test_extract_job_urls():
    """Test job URL extraction from sitemap (captured fixture)."""
    with open("tests/fixtures/phenom_sitemap1.xml", encoding="utf-8") as f:
        html = f.read()
    urls = _extract_job_urls(html)
    # Should extract job URLs from the captured sitemap
    assert len(urls) >= 4
    assert (
        "https://careers.conduent.com/us/en/job/22047/Human-Resources-Business-Partner-Analyst-II"
        in urls
    )
    assert (
        "https://careers.conduent.com/us/en/job/23413/Senior-Supervisor-Accounting-Services"
        in urls
    )


def test_extract_title_from_url():
    """Test title extraction from URL slug."""
    # Basic case
    assert (
        _extract_title_from_url(
            "https://careers.conduent.com/us/en/job/22047/Human-Resources-Business-Partner-Analyst-II"
        )
        == "Human Resources Business Partner Analyst Ii"
    )
    # Simple title
    assert (
        _extract_title_from_url("https://careers.example.com/us/en/job/12345/Software-Engineer")
        == "Software Engineer"
    )
    # Empty URL
    assert _extract_title_from_url("") == ""
    # URL without slug
    assert _extract_title_from_url("https://careers.example.com/us/en/job/12345") == ""


def test_scanner_contract():
    """Test that the scanner has the required contract."""
    assert SCANNER.name == "phenom"
    assert SCANNER.company_source == "Phenom"
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
        "source_url": "https://careers.example.com/us/en/job/12345/Software-Engineer",
        "source_id": "12345",
        "location": "",  # Empty since we don't fetch detail pages
        "description": "",  # Empty since we don't fetch detail pages
    }
    job = SCANNER.posting_to_job(posting, "careers.example.com")
    assert job["title"] == "Software Engineer"
    assert job["company_source"] == "Phenom"
    assert job["location"] == ""
    assert job["source_url"] == "https://careers.example.com/us/en/job/12345/Software-Engineer"
    assert job["source_id"] == "12345"
    assert job["description"] == ""
    assert job["jd_full"] == ""
