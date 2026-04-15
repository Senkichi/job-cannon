"""Tests for SmartRecruiters ATS scanner: URL detection, probing, and scanning."""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Tests: SmartRecruiters URL detection
# ---------------------------------------------------------------------------


class TestSmartRecruitersUrlDetection:
    """Tests for SmartRecruiters URL pattern recognition."""

    def test_jobs_url_returns_smartrecruiters_and_slug(self):
        """jobs.smartrecruiters.com/{slug}/... returns ('smartrecruiters', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.smartrecruiters.com/LinkedIn3/744000115714244-staff-data-scientist"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "LinkedIn3"

    def test_careers_url_returns_smartrecruiters_and_slug(self):
        """careers.smartrecruiters.com/{slug}/... returns ('smartrecruiters', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://careers.smartrecruiters.com/AbbVie/positions"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "AbbVie"

    def test_api_url_returns_smartrecruiters_and_slug(self):
        """API URL returns ('smartrecruiters', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://api.smartrecruiters.com/v1/companies/Visa/postings"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "Visa"

    def test_case_insensitive(self):
        """URL detection is case-insensitive."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://JOBS.SMARTRECRUITERS.COM/MyCompany/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"

    def test_non_smartrecruiters_url_not_matched(self):
        """Non-SmartRecruiters URLs are not matched."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://www.smartrecruiters.com/about"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None


# ---------------------------------------------------------------------------
# Tests: _probe_smartrecruiters
# ---------------------------------------------------------------------------


class TestProbeSmartRecruiters:
    """Tests for the SmartRecruiters probe function."""

    @patch("job_finder.web.ats_prober.requests.get")
    def test_probe_returns_true_when_jobs_found(self, mock_get):
        """Returns True when API returns 200 with totalFound > 0."""
        from job_finder.web.ats_prober import _probe_smartrecruiters
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 851, "content": [{"name": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_smartrecruiters("Visa") is True

    @patch("job_finder.web.ats_prober.requests.get")
    def test_probe_returns_false_when_zero_found(self, mock_get):
        """Returns False when API returns 200 but totalFound = 0."""
        from job_finder.web.ats_prober import _probe_smartrecruiters
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = mock_resp
        assert _probe_smartrecruiters("EmptyCompany") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_probe_returns_false_on_404(self, mock_get):
        """Returns False when API returns 404."""
        from job_finder.web.ats_prober import _probe_smartrecruiters
        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_smartrecruiters("nonexistent") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_probe_returns_false_on_exception(self, mock_get):
        """Returns False on connection error."""
        from job_finder.web.ats_prober import _probe_smartrecruiters
        mock_get.side_effect = Exception("connection refused")
        assert _probe_smartrecruiters("Visa") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_probe_sends_accept_json_header(self, mock_get):
        """Probe sends Accept: application/json header."""
        from job_finder.web.ats_prober import _probe_smartrecruiters
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 1, "content": []}
        mock_get.return_value = mock_resp
        _probe_smartrecruiters("Visa")
        _, kwargs = mock_get.call_args
        assert kwargs.get("headers", {}).get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# Tests: scan_smartrecruiters
# ---------------------------------------------------------------------------


class TestScanSmartRecruiters:
    """Tests for the SmartRecruiters job scanner."""

    def _make_posting(self, title, city="Austin", region="TX", country="US", posting_id="12345"):
        return {
            "id": posting_id,
            "name": title,
            "location": {"city": city, "region": region, "country": country},
            "company": {"identifier": "TestCo", "name": "Test Company"},
        }

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_returns_matched_jobs(self, mock_get):
        """scan_smartrecruiters returns jobs matching target titles."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 2,
            "content": [
                self._make_posting("Senior Data Scientist", posting_id="111"),
                self._make_posting("Retail Associate", posting_id="222"),
            ],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "SmartRecruiters"
        assert results[0]["location"] == "Austin, TX, US"
        assert "TestCo" in results[0]["source_url"]
        assert "111" in results[0]["source_url"]

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_applies_exclusions(self, mock_get):
        """Filters out jobs matching exclusion keywords."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 1,
            "content": [self._make_posting("Junior Data Scientist")],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], ["junior"])
        assert len(results) == 0

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_handles_empty_response(self, mock_get):
        """Returns empty list when no postings."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_handles_http_error(self, mock_get):
        """Returns empty list on non-200 status."""
        from job_finder.web.ats_platforms import scan_smartrecruiters
        mock_get.return_value = MagicMock(status_code=500)
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_paginates(self, mock_get):
        """Fetches multiple pages when totalFound > page_size."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "totalFound": 150,
            "content": [self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100)],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "totalFound": 150,
            "content": [self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100, 150)],
        }
        mock_get.side_effect = [page1, page2]

        results = scan_smartrecruiters("TestCo", ["data analyst"], [])
        assert len(results) == 150
        assert mock_get.call_count == 2

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_request_exception(self, mock_get):
        """Returns empty list on request exception."""
        from job_finder.web.ats_platforms import scan_smartrecruiters
        mock_get.side_effect = Exception("network error")
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_location_assembly(self, mock_get):
        """Assembles location from city, region, country fields."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 1,
            "content": [{
                "id": "999",
                "name": "Data Scientist",
                "location": {"city": "San Francisco", "region": "CA", "country": "US"},
            }],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results[0]["location"] == "San Francisco, CA, US"
