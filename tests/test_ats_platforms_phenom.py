"""Tests for Phenom ATS platform scanner."""

import pytest
from job_finder.web.ats_platforms._platforms_phenom import (
    _extract_job_id,
    _extract_job_urls,
    _extract_sitemap_urls,
    _extract_posting_from_html,
    SCANNER,
)


def test_extract_job_id():
    """Test job ID extraction from Phenom URLs."""
    assert _extract_job_id("https://careers.conduent.com/us/en/job/23003/Accounting-Analyst") == "23003"
    assert _extract_job_id("https://careers.blackbaud.com/us/en/job/12345/Engineer") == "12345"
    assert _extract_job_id("https://example.com/not-a-job") is None


def test_extract_sitemap_urls():
    """Test sitemap URL extraction from sitemap index."""
    html = """
    <sitemapindex>
        <sitemap><loc>https://careers.conduent.com/us/en/sitemap1.xml</loc></sitemap>
        <sitemap><loc>https://careers.conduent.com/us/en/sitemap2.xml</loc></sitemap>
    </sitemapindex>
    """
    urls = _extract_sitemap_urls(html)
    assert len(urls) == 2
    assert "https://careers.conduent.com/us/en/sitemap1.xml" in urls
    assert "https://careers.conduent.com/us/en/sitemap2.xml" in urls


def test_extract_job_urls():
    """Test job URL extraction from sitemap."""
    html = """
    <urlset>
        <url><loc>https://careers.conduent.com/us/en/job/23003/Accounting-Analyst</loc></url>
        <url><loc>https://careers.conduent.com/us/en/page/about</loc></url>
        <url><loc>https://careers.conduent.com/us/en/job/12345/Engineer</loc></url>
    </urlset>
    """
    urls = _extract_job_urls(html)
    assert len(urls) == 2
    assert "https://careers.conduent.com/us/en/job/23003/Accounting-Analyst" in urls
    assert "https://careers.conduent.com/us/en/job/12345/Engineer" in urls


def test_extract_posting_from_html():
    """Test job data extraction from HTML."""
    html = """
    <html>
    <head>
        <meta property="og:title" content="Software Engineer | Remote | Full Time">
        <meta name="description" content="Apply for Software Engineer job with Company in San Francisco, CA at Company">
        <meta name="keywords" content="Software Engineer, San Francisco, CA, Engineering">
    </head>
    <body>
        <h1>Software Engineer</h1>
    </body>
    </html>
    """
    posting = _extract_posting_from_html(html, "https://careers.example.com/us/en/job/12345/Software-Engineer")
    assert posting is not None
    assert posting["title"] == "Software Engineer"
    assert "San Francisco, CA" in posting["location"]
    assert posting["source_id"] == "12345"
    assert posting["source_url"] == "https://careers.example.com/us/en/job/12345/Software-Engineer"


def test_extract_posting_title_bleed_protection():
    """Test that location glued to title is removed (PR #539 regression)."""
    html = """
    <html>
    <head>
        <meta property="og:title" content="Data Scientist in New York, NY">
        <meta name="description" content="Apply for Data Scientist job">
    </head>
    </html>
    """
    posting = _extract_posting_from_html(html, "https://careers.example.com/us/en/job/12345/Data-Scientist")
    assert posting is not None
    assert posting["title"] == "Data Scientist"
    assert "New York" not in posting["title"]


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
        "location": "San Francisco, CA",
        "description": "Job description",
    }
    job = SCANNER.posting_to_job(posting, "careers.example.com")
    assert job["title"] == "Software Engineer"
    assert job["company_source"] == "Phenom"
    assert job["location"] == "San Francisco, CA"
    assert job["source_url"] == "https://careers.example.com/us/en/job/12345/Software-Engineer"
    assert job["source_id"] == "12345"
    assert job["description"] == "Job description"
    assert job["jd_full"] == "Job description"
