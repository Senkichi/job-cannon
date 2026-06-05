"""Tests for ThordataSource — Google Jobs SERP via Thordata API.

Coverage:
- Field mapping (_parse_result)
- Recency filter (max_age_days)
- Salary extraction from extensions[]
- docid extraction from URL fragment
- Posting age parsing (all age string formats)
- fetch_jobs iterates queries and combines results
- _search: POST request with correct headers; returns [] on HTTP error
"""

from unittest.mock import MagicMock, patch

import pytest

from job_finder.sources.thordata_source import ThordataSource

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def source():
    return ThordataSource(api_key="test-key", max_age_days=3)


def _result(
    title="Senior Data Scientist",
    company_name="Acme Corp",
    location="San Francisco, CA",
    link="https://www.google.com/search?gl=us&hl=en&q=DS&udm=8#vhid=vt%3D20/docid%3DABC123%3D%3D&vssid=jobs-detail-viewer",
    extensions=None,
    via="LinkedIn",
):
    """Build a minimal Thordata job_results.jobs[] item."""
    return {
        "title": title,
        "company_name": company_name,
        "location": location,
        "link": link,
        "extensions": extensions if extensions is not None else ["1 day ago", "Full-time"],
        "via": via,
    }


# ---------------------------------------------------------------------------
# Test: _parse_result field mapping
# ---------------------------------------------------------------------------


class TestParseResult:
    def test_extracts_title(self, source):
        job = source._parse_result(_result(title="Staff Data Scientist"))
        assert job is not None
        assert job.title == "Staff Data Scientist"

    def test_extracts_company(self, source):
        job = source._parse_result(_result(company_name="Intuit"))
        assert job is not None
        assert job.company == "Intuit"

    def test_extracts_location(self, source):
        job = source._parse_result(_result(location="Remote"))
        assert job is not None
        assert job.location == "Remote"

    def test_source_is_always_thordata(self, source):
        job = source._parse_result(_result())
        assert job is not None
        assert job.source == "thordata"

    def test_description_is_none(self, source):
        """Thordata never provides description — enrichment fills it."""
        job = source._parse_result(_result())
        assert job is not None
        assert job.description is None

    def test_source_url_is_link(self, source):
        link = "https://www.google.com/search?gl=us&hl=en&q=DS&udm=8#vhid=vt%3D20/docid%3DXYZ%3D%3D&vssid=jobs-detail-viewer"
        job = source._parse_result(_result(link=link))
        assert job is not None
        assert job.source_url == link

    def test_source_id_not_persisted(self, source):
        # The Google-Jobs docid is a search-result token, not a per-job-stable
        # platform ID, so no source_id is persisted (I-11 contract).
        link = "https://www.google.com/search?gl=us&hl=en&q=DS&udm=8#vhid=vt%3D20/docid%3DABC123%3D%3D&vssid=jobs-detail-viewer"
        job = source._parse_result(_result(link=link))
        assert job is not None
        assert not job.source_id

    def test_missing_title_returns_none(self, source):
        result = _result()
        result["title"] = ""
        assert source._parse_result(result) is None

    def test_missing_company_returns_none(self, source):
        result = _result()
        result["company_name"] = ""
        assert source._parse_result(result) is None

    def test_salary_extracted_from_extensions(self, source):
        result = _result(extensions=["1 day ago", "204K–276K a year", "Full-time"])
        job = source._parse_result(result)
        assert job is not None
        assert job.salary_min == 204000
        assert job.salary_max == 276000

    def test_no_salary_when_absent(self, source):
        result = _result(extensions=["1 day ago", "Full-time", "Health insurance"])
        job = source._parse_result(result)
        assert job is not None
        assert job.salary_min is None
        assert job.salary_max is None


# ---------------------------------------------------------------------------
# Test: Recency filter
# ---------------------------------------------------------------------------


class TestRecencyFilter:
    def test_rejects_job_older_than_max_age_days(self, source):
        """Jobs with age > max_age_days are excluded."""
        result = _result(extensions=["29 days ago", "Full-time"])
        assert source._parse_result(result) is None

    def test_accepts_job_within_max_age_days(self, source):
        result = _result(extensions=["1 day ago", "Full-time"])
        assert source._parse_result(result) is not None

    def test_accepts_just_posted(self, source):
        result = _result(extensions=["Just posted", "Full-time"])
        assert source._parse_result(result) is not None

    def test_accepts_today(self, source):
        result = _result(extensions=["Today", "Full-time"])
        assert source._parse_result(result) is not None

    def test_accepts_hours_ago(self, source):
        result = _result(extensions=["5 hours ago", "Full-time"])
        assert source._parse_result(result) is not None

    def test_accepts_no_date_in_extensions(self, source):
        """When no age string is present, job is included (can't determine age)."""
        result = _result(extensions=["Full-time", "Health insurance"])
        assert source._parse_result(result) is not None

    def test_rejects_two_weeks_ago(self, source):
        result = _result(extensions=["2 weeks ago", "Full-time"])
        assert source._parse_result(result) is None

    def test_accepts_exactly_at_max_age(self, source):
        """Job posted exactly max_age_days days ago should be accepted."""
        result = _result(extensions=["3 days ago", "Full-time"])
        assert source._parse_result(result) is not None

    def test_rejects_one_month_ago(self, source):
        result = _result(extensions=["1 month ago", "Full-time"])
        assert source._parse_result(result) is None


# ---------------------------------------------------------------------------
# Test: Posting age parsing
# ---------------------------------------------------------------------------


class TestPostingAgeParsing:
    def test_just_posted_returns_zero(self, source):
        assert source._parse_posting_age(["Just posted"]) == 0

    def test_today_returns_zero(self, source):
        assert source._parse_posting_age(["Today"]) == 0

    def test_hours_ago_returns_zero(self, source):
        assert source._parse_posting_age(["5 hours ago"]) == 0

    def test_one_hour_ago_returns_zero(self, source):
        assert source._parse_posting_age(["1 hour ago"]) == 0

    def test_one_day_ago_returns_one(self, source):
        assert source._parse_posting_age(["1 day ago"]) == 1

    def test_three_days_ago(self, source):
        assert source._parse_posting_age(["3 days ago"]) == 3

    def test_twenty_nine_days_ago(self, source):
        assert source._parse_posting_age(["29 days ago"]) == 29

    def test_two_weeks_ago_returns_fourteen(self, source):
        assert source._parse_posting_age(["2 weeks ago"]) == 14

    def test_one_week_ago_returns_seven(self, source):
        assert source._parse_posting_age(["1 week ago"]) == 7

    def test_one_month_ago_returns_thirty(self, source):
        assert source._parse_posting_age(["1 month ago"]) == 30

    def test_no_date_returns_none(self, source):
        assert source._parse_posting_age(["Full-time", "Health insurance"]) is None

    def test_date_among_other_extensions(self, source):
        """Age string found among unrelated extension values."""
        assert source._parse_posting_age(["Full-time", "2 days ago", "Health insurance"]) == 2

    def test_empty_extensions_returns_none(self, source):
        assert source._parse_posting_age([]) is None


# ---------------------------------------------------------------------------
# Test: Salary extraction from extensions
# ---------------------------------------------------------------------------


class TestSalaryExtraction:
    def test_k_range_with_en_dash(self, source):
        low, high = source._extract_salary_from_extensions(["204K–276K a year"])
        assert low == 204000
        assert high == 276000

    def test_k_range_with_hyphen(self, source):
        low, high = source._extract_salary_from_extensions(["160K-180K a year"])
        assert low == 160000
        assert high == 180000

    def test_dollar_k_range(self, source):
        low, high = source._extract_salary_from_extensions(["$160K–$180K"])
        assert low == 160000
        assert high == 180000

    def test_no_salary_returns_none_none(self, source):
        low, high = source._extract_salary_from_extensions(["Full-time", "Health insurance"])
        assert low is None
        assert high is None

    def test_empty_extensions(self, source):
        low, high = source._extract_salary_from_extensions([])
        assert low is None
        assert high is None

    def test_salary_among_other_extensions(self, source):
        low, high = source._extract_salary_from_extensions(
            ["21 days ago", "204K–276K a year", "Full-time", "Health insurance"]
        )
        assert low == 204000
        assert high == 276000

    def test_comma_formatted_large_numbers(self, source):
        """Comma-formatted numbers like '204,000–276,000 a year' are parsed correctly."""
        low, high = source._extract_salary_from_extensions(["204,000–276,000 a year"])
        assert low == 204000
        assert high == 276000

    def test_10k_range_salary(self, source):
        """$5K–$10K range: high=10 must still be multiplied (>= 10, not > 10)."""
        low, high = source._extract_salary_from_extensions(["5K–10K a year"])
        assert low == 5000
        assert high == 10000

    def test_exactly_10k_high(self, source):
        """Boundary: high=10 with K suffix produces 10000, not 10."""
        low, high = source._extract_salary_from_extensions(["8K–10K a year"])
        assert high == 10000


# ---------------------------------------------------------------------------
# Test: fetch_jobs iterates queries and combines results
# ---------------------------------------------------------------------------


class TestFetchJobs:
    def test_calls_search_per_query(self, source):
        """fetch_jobs calls _search once for each query in the list."""
        queries = [
            {"query": "Data Scientist", "location": "Remote"},
            {"query": "Analytics Manager", "location": "SF"},
        ]
        with patch.object(source, "_search", return_value=[]) as mock_search:
            source.fetch_jobs(queries)
        assert mock_search.call_count == 2
        mock_search.assert_any_call("Data Scientist", "Remote")
        mock_search.assert_any_call("Analytics Manager", "SF")

    def test_combines_results_from_multiple_queries(self, source):
        """Results from multiple queries are combined into one list."""
        from job_finder.models import Job

        def make_job(title):
            return Job(
                title=title,
                company="Co",
                location="Remote",
                source="thordata",
                source_url="https://example.com",
            )

        query_results = [
            [make_job("Job A"), make_job("Job B")],
            [make_job("Job C")],
        ]
        calls = iter(query_results)
        with patch.object(source, "_search", side_effect=lambda q, loc: next(calls)):
            jobs = source.fetch_jobs(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "AM", "location": "NY"},
                ]
            )
        assert len(jobs) == 3

    def test_empty_queries_returns_empty_list(self, source):
        jobs = source.fetch_jobs([])
        assert jobs == []

    def test_missing_location_key_uses_empty_string(self, source):
        """Queries without 'location' key should not raise."""
        with patch.object(source, "_search", return_value=[]) as mock_search:
            source.fetch_jobs([{"query": "Data Scientist"}])
        mock_search.assert_called_once_with("Data Scientist", "")


# ---------------------------------------------------------------------------
# Test: _search — HTTP behavior
# ---------------------------------------------------------------------------


class TestSearch:
    def test_posts_with_bearer_auth(self, source):
        """_search sends Authorization: Bearer header."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"job_results": {"jobs": []}}

        with patch(
            "job_finder.sources.thordata_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._search("Data Scientist", "Remote")

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer test-key"

    def test_returns_empty_list_on_http_error(self, source):
        """HTTP errors are caught and return []."""
        with patch(
            "job_finder.sources.thordata_source.requests.post",
            side_effect=Exception("Connection refused"),
        ):
            jobs = source._search("Data Scientist", "Remote")
        assert jobs == []

    def test_returns_empty_list_on_raise_for_status(self, source):
        """Non-2xx responses are caught and return []."""
        import requests as req

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.HTTPError("403 Forbidden")

        with patch("job_finder.sources.thordata_source.requests.post", return_value=mock_resp):
            jobs = source._search("Data Scientist", "Remote")
        assert jobs == []

    def test_parses_job_results_key(self, source):
        """_search extracts jobs from job_results.jobs[] key."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "job_results": {
                "jobs": [_result(title="DS Role", extensions=["1 day ago", "Full-time"])]
            }
        }

        with patch("job_finder.sources.thordata_source.requests.post", return_value=mock_resp):
            jobs = source._search("Data Scientist", "Remote")

        assert len(jobs) == 1
        assert jobs[0].title == "DS Role"

    def test_skips_old_jobs_during_search(self, source):
        """_search applies the recency filter during parsing."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "job_results": {
                "jobs": [
                    _result(title="Old Job", extensions=["30 days ago"]),
                    _result(title="New Job", extensions=["1 day ago"]),
                ]
            }
        }

        with patch("job_finder.sources.thordata_source.requests.post", return_value=mock_resp):
            jobs = source._search("Data Scientist", "Remote")

        assert len(jobs) == 1
        assert jobs[0].title == "New Job"
