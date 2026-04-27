"""Tests for SmartRecruiters ATS scanner: URL detection, probing, and scanning."""

from unittest.mock import MagicMock, patch

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


@patch("job_finder.web.ats_platforms._fetch_smartrecruiters_description", return_value="")
class TestScanSmartRecruiters:
    """Tests for the SmartRecruiters job scanner.

    Class-level patch disables the per-job detail fetch so list-endpoint
    behavior stays focused and test run hermetic. A separate class
    (TestFetchSmartRecruitersDescription) covers the detail fetch itself.
    """

    def _make_posting(self, title, city="Austin", region="TX", country="US", posting_id="12345"):
        return {
            "id": posting_id,
            "name": title,
            "location": {"city": city, "region": region, "country": country},
            "company": {"identifier": "TestCo", "name": "Test Company"},
        }

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_returns_matched_jobs(self, mock_get, _mock_detail):
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
    def test_scan_applies_exclusions(self, mock_get, _mock_detail):
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
    def test_scan_handles_empty_response(self, mock_get, _mock_detail):
        """Returns empty list when no postings."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_handles_http_error(self, mock_get, _mock_detail):
        """Returns empty list on non-200 status."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_get.return_value = MagicMock(status_code=500)
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_paginates(self, mock_get, _mock_detail):
        """Fetches multiple pages when totalFound > page_size."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "totalFound": 150,
            "content": [
                self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100)
            ],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "totalFound": 150,
            "content": [
                self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100, 150)
            ],
        }
        mock_get.side_effect = [page1, page2]

        results = scan_smartrecruiters("TestCo", ["data analyst"], [])
        assert len(results) == 150
        assert mock_get.call_count == 2

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_request_exception(self, mock_get, _mock_detail):
        """Returns empty list on request exception."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_get.side_effect = Exception("network error")
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_location_assembly(self, mock_get, _mock_detail):
        """Assembles location from city, region, country fields."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 1,
            "content": [
                {
                    "id": "999",
                    "name": "Data Scientist",
                    "location": {"city": "San Francisco", "region": "CA", "country": "US"},
                }
            ],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results[0]["location"] == "San Francisco, CA, US"


# ---------------------------------------------------------------------------
# Tests: _fetch_smartrecruiters_description (per-job detail fetch)
# ---------------------------------------------------------------------------


class TestFetchSmartRecruitersDescription:
    """Tests for the SmartRecruiters per-job detail fetcher."""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_and_strips_html_description(self, mock_get):
        """Returns concatenated sections, HTML-stripped."""
        from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "<p>Build <b>great</b> products.</p>"},
                    "qualifications": {"text": "5+ years of Python experience."},
                }
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_smartrecruiters_description("TestCo", "999")
        assert "Build" in text
        assert "great" in text
        assert "5+ years" in text
        assert "<b>" not in text

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_all_four_sections(self, mock_get):
        """All four known sections (company, job, qualifications, additional) are included."""
        from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "companyDescription": {"text": "We are TestCo."},
                    "jobDescription": {"text": "Write great code."},
                    "qualifications": {"text": "Python required."},
                    "additionalInformation": {"text": "Remote OK."},
                }
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_smartrecruiters_description("TestCo", "999")
        assert "We are TestCo" in text
        assert "Write great code" in text
        assert "Python required" in text
        assert "Remote OK" in text

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_404_returns_empty_string(self, mock_get):
        """Detail 404 returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

        mock_get.return_value = MagicMock(status_code=404)
        assert _fetch_smartrecruiters_description("TestCo", "DNE") == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_network_exception_returns_empty_string(self, mock_get):
        """Network error returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

        mock_get.side_effect = Exception("timeout")
        assert _fetch_smartrecruiters_description("TestCo", "999") == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_missing_jobAd_returns_empty_string(self, mock_get):
        """Response without jobAd.sections returns empty string."""
        from job_finder.web.ats_platforms import _fetch_smartrecruiters_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"unrelated": "shape"}
        mock_get.return_value = mock_resp
        assert _fetch_smartrecruiters_description("TestCo", "999") == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_scan_smartrecruiters_populates_description_from_detail(self, mock_get):
        """End-to-end: scan_smartrecruiters calls detail endpoint and sets description."""
        from job_finder.web.ats_platforms import scan_smartrecruiters

        list_resp = MagicMock(status_code=200)
        list_resp.json.return_value = {
            "totalFound": 1,
            "content": [
                {
                    "id": "abc-123",
                    "name": "Senior Data Scientist",
                    "location": {"city": "SF", "region": "CA", "country": "US"},
                }
            ],
        }
        detail_resp = MagicMock(status_code=200)
        detail_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "Role details here."},
                    "qualifications": {"text": "Must know Python."},
                }
            }
        }
        mock_get.side_effect = [list_resp, detail_resp]

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert len(results) == 1
        assert "Role details here" in results[0]["description"]
        assert "Must know Python" in results[0]["description"]
        # Second call is the detail fetch
        detail_call_url = mock_get.call_args_list[1][0][0]
        assert (
            detail_call_url
            == "https://api.smartrecruiters.com/v1/companies/TestCo/postings/abc-123"
        )
