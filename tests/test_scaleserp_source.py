"""Tests for scaleserp_source.py — ScaleSerp Google Jobs API integration.

ScaleSerp is SerpAPI-compatible. These tests verify:
1. The subclass uses the correct BASE_URL and source_name
2. The parsing logic (inherited from SerpAPISource) works with ScaleSerp
3. HTTP requests go to the correct endpoint
"""

from unittest.mock import MagicMock, patch

import pytest

from job_finder.sources.scaleserp_source import ScaleSerpSource
from job_finder.sources.serpapi_source import SerpAPISource


_TEST_API_KEY = "test-scaleserp-key"  # noqa: S105 -- test placeholder


SAMPLE_SCALESERP_RESULT = {
    "title": "Staff Data Scientist",
    "company_name": "Acme Health",
    "location": "San Francisco, CA",
    "job_highlights": [
        {
            "title": "Qualifications",
            "items": ["7+ years experience", "PhD or Masters preferred"],
        }
    ],
    "detected_extensions": {
        "posted_at": "2 days ago",
        "schedule_type": "Full-time",
        "salary": "$200K\u2013$280K a year",
    },
    "apply_options": [
        {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/789"},
    ],
    "job_id": "abc123def456",
    "share_link": "https://www.google.com/search?ibp=htl;jobs#fpstate=tldetail",
}


class TestScaleSerpSourceConfig:
    def test_base_url_is_scaleserp(self):
        assert ScaleSerpSource.BASE_URL == "https://api.scaleserp.com/search"

    def test_base_url_differs_from_serpapi(self):
        assert ScaleSerpSource.BASE_URL != SerpAPISource.BASE_URL

    def test_source_name_is_scaleserp(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        assert source.source_name == "scaleserp"

    def test_api_key_stored(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        assert source.api_key == _TEST_API_KEY


class TestScaleSerpParsing:
    def test_parse_result_source_is_scaleserp(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        job = source._parse_result(SAMPLE_SCALESERP_RESULT)

        assert job is not None
        assert job.source == "scaleserp"

    def test_parse_result_extracts_fields(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        job = source._parse_result(SAMPLE_SCALESERP_RESULT)

        assert job is not None
        assert job.title == "Staff Data Scientist"
        assert job.company == "Acme Health"
        assert job.location == "San Francisco, CA"
        assert job.source_url == "https://www.linkedin.com/jobs/view/789"
        assert job.source_id == "abc123def456"

    def test_parse_result_salary(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        job = source._parse_result(SAMPLE_SCALESERP_RESULT)

        assert job is not None
        assert job.salary_min == 200000
        assert job.salary_max == 280000

    def test_parse_result_missing_title_returns_none(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        import copy
        result = copy.deepcopy(SAMPLE_SCALESERP_RESULT)
        result["title"] = ""
        assert source._parse_result(result) is None

    def test_parse_result_missing_company_returns_none(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)
        import copy
        result = copy.deepcopy(SAMPLE_SCALESERP_RESULT)
        result["company_name"] = ""
        assert source._parse_result(result) is None


class TestScaleSerpHTTPRequests:
    def test_fetch_jobs_requests_correct_url(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": [SAMPLE_SCALESERP_RESULT]}

        with patch("requests.get", return_value=mock_response) as mock_get:
            jobs = source.fetch_jobs([{"query": "Staff Data Scientist", "location": "SF"}])

        assert mock_get.call_count == 1
        call_url = mock_get.call_args[0][0]
        assert call_url == "https://api.scaleserp.com/search"

    def test_fetch_jobs_uses_google_jobs_engine(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": [SAMPLE_SCALESERP_RESULT]}

        with patch("requests.get", return_value=mock_response) as mock_get:
            source.fetch_jobs([{"query": "Data Scientist", "location": "Remote"}])

        params = mock_get.call_args[1]["params"]
        assert params["engine"] == "google_jobs"
        assert params["api_key"] == _TEST_API_KEY

    def test_fetch_jobs_returns_scaleserp_sourced_jobs(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": [SAMPLE_SCALESERP_RESULT]}

        with patch("requests.get", return_value=mock_response):
            jobs = source.fetch_jobs([{"query": "Staff Data Scientist", "location": "SF"}])

        assert len(jobs) == 1
        assert jobs[0].source == "scaleserp"

    def test_fetch_jobs_http_error_returns_empty(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("403 Forbidden")

        with patch("requests.get", return_value=mock_response):
            jobs = source.fetch_jobs([{"query": "Data Scientist", "location": "SF"}])

        assert jobs == []

    def test_fetch_jobs_multiple_queries(self):
        source = ScaleSerpSource(api_key=_TEST_API_KEY)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": [SAMPLE_SCALESERP_RESULT]}

        with patch("requests.get", return_value=mock_response) as mock_get:
            jobs = source.fetch_jobs([
                {"query": "Staff Data Scientist", "location": "SF"},
                {"query": "Analytics Manager", "location": "Remote"},
            ])

        assert mock_get.call_count == 2
        assert len(jobs) == 2


class TestSerpAPISourceBackwardCompat:
    """SerpAPISource defaults remain unchanged after adding source_name param."""

    def test_serpapi_source_default_source_name(self):
        source = SerpAPISource(api_key="key")
        assert source.source_name == "serpapi"

    def test_serpapi_source_parse_still_labels_serpapi(self):
        source = SerpAPISource(api_key="key")
        # Minimal valid result
        result = {
            "title": "Data Scientist",
            "company_name": "Corp",
            "location": "Remote",
            "job_highlights": [],
            "detected_extensions": {},
            "apply_options": [],
            "job_id": "x1",
        }
        job = source._parse_result(result)
        assert job is not None
        assert job.source == "serpapi"
