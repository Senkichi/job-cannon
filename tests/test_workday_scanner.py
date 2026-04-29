"""Tests for Workday ATS scanner: URL detection, probing, and scanning."""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Tests: Workday URL detection in ats_detection.py
# ---------------------------------------------------------------------------


class TestWorkdayUrlDetection:
    """Tests for Workday URL pattern recognition in extract_ats_from_urls."""

    def test_workday_human_url_returns_workday_and_slug(self):
        """Human-facing myworkdayjobs.com URL returns ('workday', 'subdomain/board')."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_human_url_with_en_us_prefix(self):
        """Human URL with en-US locale prefix still extracts correctly."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/some-path"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_api_url_returns_workday_and_slug(self):
        """API URL returns ('workday', 'subdomain/board')."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_case_insensitive(self):
        """Workday URL detection is case-insensitive."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://WALMART.WD5.MYWORKDAYJOBS.COM/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"

    def test_workday_url_does_not_match_non_workday(self):
        """Non-Workday URLs are not matched."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://www.walmart.com/careers"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None


# ---------------------------------------------------------------------------
# Tests: _probe_workday
# ---------------------------------------------------------------------------


class TestProbeWorkday:
    """Tests for the Workday probe function."""

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_true_on_200(self, mock_post):
        """_probe_workday returns True when API returns 200."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        assert _probe_workday("walmart.wd5/WalmartExternal") is True

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_false_on_404(self, mock_post):
        """_probe_workday returns False when API returns 404."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=404)
        assert _probe_workday("invalid/board") is False

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_false_on_exception(self, mock_post):
        """_probe_workday returns False on connection error."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.side_effect = Exception("connection refused")
        assert _probe_workday("walmart.wd5/WalmartExternal") is False

    def test_probe_returns_false_on_invalid_slug(self):
        """_probe_workday returns False for slug without '/'."""
        from job_finder.web.ats_prober import _probe_workday

        assert _probe_workday("no-slash") is False

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_sends_post_request_with_correct_url(self, mock_post):
        """_probe_workday constructs correct API URL from slug."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        _probe_workday("walmart.wd5/WalmartExternal")
        args, kwargs = mock_post.call_args
        assert (
            args[0]
            == "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"
        )


# ---------------------------------------------------------------------------
# Tests: scan_workday
# ---------------------------------------------------------------------------


@patch("job_finder.web.ats_platforms._fetch_workday_description", return_value="")
class TestScanWorkday:
    """Tests for the Workday job scanner.

    The class-level patch disables the per-job detail fetch so these tests
    stay hermetic and focused on list-endpoint behavior. A separate test
    class (TestFetchWorkdayDescription) exercises the detail fetch itself.
    """

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_returns_matched_jobs(self, mock_post, _mock_detail):
        """scan_workday returns jobs matching target titles."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Sunnyvale, CA",
                    "externalPath": "Senior-Data-Scientist_R-12345",
                },
                {
                    "title": "Retail Associate",
                    "locationsText": "Dallas, TX",
                    "externalPath": "Retail-Associate_R-99999",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "Workday"
        assert results[0]["location"] == "Sunnyvale, CA"
        assert "walmart.wd5.myworkdayjobs.com" in results[0]["source_url"]

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_applies_exclusions(self, mock_post, _mock_detail):
        """scan_workday filters out jobs matching exclusion keywords."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Junior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "Junior-DS_R-001",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=["junior"],
        )
        assert len(results) == 0

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_handles_empty_response(self, mock_post, _mock_detail):
        """scan_workday returns empty list when API returns no postings."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"total": 0, "jobPostings": []}
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_handles_http_error(self, mock_post, _mock_detail):
        """scan_workday returns empty list on non-200 status."""
        from job_finder.web.ats_platforms import scan_workday

        mock_post.return_value = MagicMock(status_code=404)
        results = scan_workday(
            "invalid/board",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    def test_scan_rejects_invalid_slug_format(self, _mock_detail):
        """scan_workday returns empty list for slug without '/'."""
        from job_finder.web.ats_platforms import scan_workday

        results = scan_workday("no-slash", ["data scientist"], [])
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_paginates_correctly(self, mock_post, _mock_detail):
        """scan_workday fetches multiple pages when total > page_size."""
        from job_finder.web.ats_platforms import scan_workday

        page1_response = MagicMock(status_code=200)
        page1_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20)
            ],
        }
        page2_response = MagicMock(status_code=200)
        page2_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20, 25)
            ],
        }
        mock_post.side_effect = [page1_response, page2_response]

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 25
        assert mock_post.call_count == 2

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_request_exception_returns_empty(self, mock_post, _mock_detail):
        """scan_workday returns empty list on request exception."""
        from job_finder.web.ats_platforms import scan_workday

        mock_post.side_effect = Exception("network error")
        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_source_url_format(self, mock_post, _mock_detail):
        """scan_workday builds correct source_url from externalPath."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Data-Scientist_R-12345",
                }
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results[0]["source_url"] == (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Data-Scientist_R-12345"
        )


# ---------------------------------------------------------------------------
# Tests: _fetch_workday_description (per-job detail fetch)
# ---------------------------------------------------------------------------


class TestFetchWorkdayDescription:
    """Tests for the Workday per-job detail fetcher.

    The Workday CXS list endpoint returns titles only; the full HTML
    description lives at a separate per-job URL. These tests cover the
    detail-fetch behavior directly.
    """

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_and_strips_html_description(self, mock_get):
        """_fetch_workday_description returns plain-text JD from HTML."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "<p>Design and build <b>scalable</b> data pipelines.</p>"
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert "Design and build" in text
        assert "scalable" in text
        assert "<b>" not in text  # HTML was stripped

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_plain_text_description_unchanged(self, mock_get):
        """Non-HTML descriptions pass through without stripping."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {"jobDescription": "Plain text description here."}
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == "Plain text description here."

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_404_returns_empty_string(self, mock_get):
        """Detail endpoint 404 returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_get.return_value = MagicMock(status_code=404)
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DNE")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_network_exception_returns_empty_string(self, mock_get):
        """Network error returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_get.side_effect = Exception("timeout")
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_missing_jobPostingInfo_returns_empty_string(self, mock_get):
        """Response without jobPostingInfo key returns empty string."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"other": "shape"}
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_workday_populates_description_from_detail(self, mock_post, mock_get):
        """End-to-end: scan_workday calls detail endpoint and populates description."""
        from job_finder.web.ats_platforms import scan_workday

        # List endpoint returns one matching job
        list_resp = MagicMock(status_code=200)
        list_resp.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Senior-DS_R-1",
                }
            ],
        }
        mock_post.return_value = list_resp

        # Detail endpoint returns the JD
        detail_resp = MagicMock(status_code=200)
        detail_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "Full job description with details about the role."
            }
        }
        mock_get.return_value = detail_resp

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert "Full job description" in results[0]["description"]
        # Detail URL hit correctly
        args, _ = mock_get.call_args
        assert args[0] == (
            "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/"
            "WalmartExternal/job/Senior-DS_R-1"
        )
