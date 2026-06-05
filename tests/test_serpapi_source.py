"""Tests for serpapi_source.py — SerpAPI Google Jobs response format audit.

# AUDIT 2026-03-15: SerpAPI Google Jobs response fields verified against current API schema.
# Fields: title, company_name, location, job_highlights, detected_extensions.salary,
# apply_options, share_link, job_id. All field mappings confirmed current.
"""

from unittest.mock import MagicMock, patch

from job_finder.sources.serpapi_source import SerpAPISource

# ---------------------------------------------------------------------------
# Shared sample response matching current SerpAPI Google Jobs API schema
# ---------------------------------------------------------------------------

SAMPLE_SERPAPI_RESULT = {
    "title": "Senior Data Scientist",
    "company_name": "Acme Corp",
    "location": "San Francisco, CA",
    "via": "via LinkedIn",
    "description": "We are looking for a Senior Data Scientist...",
    "job_highlights": [
        {
            "title": "Qualifications",
            "items": [
                "5+ years experience in data science",
                "PhD or Masters in relevant field",
            ],
        },
        {
            "title": "Responsibilities",
            "items": [
                "Lead ML model development",
                "Mentor junior data scientists",
            ],
        },
    ],
    "detected_extensions": {
        "posted_at": "3 days ago",
        "schedule_type": "Full-time",
        "salary": "$150K\u2013$200K a year",
    },
    "apply_options": [
        {
            "title": "LinkedIn",
            "link": "https://www.linkedin.com/jobs/view/123456",
        },
        {
            "title": "Indeed",
            "link": "https://www.indeed.com/viewjob?jk=abc123",
        },
    ],
    "job_id": "eyJqb2JfdGl0bGUiOiJTZW5pb3IgRGF0YSBTY2llbnRpc3QiLCJjb21wYW55X25hbWUiOiJBY21lIENvcnAifQ==",
    "share_link": "https://www.google.com/search?q=senior+data+scientist+acme&ibp=htl;jobs#fpstate=tldetail&htivrt=jobs&htidocid=job123",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_API_KEY = "test-key-does-not-matter"


def _make_source() -> SerpAPISource:
    return SerpAPISource(api_key=_TEST_API_KEY)


def _result_with(**overrides) -> dict:
    """Return a copy of SAMPLE_SERPAPI_RESULT with the given keys overridden."""
    import copy

    result = copy.deepcopy(SAMPLE_SERPAPI_RESULT)
    result.update(overrides)
    return result


def _result_with_salary(salary_str: str) -> dict:
    """Return result with a specific salary string in detected_extensions."""
    import copy

    result = copy.deepcopy(SAMPLE_SERPAPI_RESULT)
    result["detected_extensions"]["salary"] = salary_str
    return result


# ---------------------------------------------------------------------------
# Tests: TestSerpAPIFormatAudit
# ---------------------------------------------------------------------------


class TestSerpAPIFormatAudit:
    # AUDIT 2026-03-15: SerpAPI Google Jobs response fields verified against current API schema.

    def test_parse_result_extracts_all_fields(self):
        """Full SAMPLE_SERPAPI_RESULT maps to correct Job fields."""
        source = _make_source()
        job = source._parse_result(SAMPLE_SERPAPI_RESULT)

        assert job is not None
        assert job.title == "Senior Data Scientist"
        assert job.company == "Acme Corp"
        assert job.location == "San Francisco, CA"
        assert job.source == "serpapi"
        assert job.source_url == "https://www.linkedin.com/jobs/view/123456"
        # SerpAPI job_id is a search-result token, not a per-job-stable platform
        # ID, so no source_id is persisted (I-11 contract).
        assert not job.source_id

    def test_parse_result_salary_k_dash_format(self):
        """Salary '$150K\u2013$200K a year' (en-dash) parses to 150000/200000."""
        source = _make_source()
        # SAMPLE_SERPAPI_RESULT already has this format with the en-dash (\u2013)
        job = source._parse_result(SAMPLE_SERPAPI_RESULT)

        assert job is not None
        assert job.salary_min == 150000
        assert job.salary_max == 200000

    def test_parse_result_salary_comma_format(self):
        """Salary '$150,000-$200,000 a year' (comma, hyphen) parses to 150000/200000."""
        source = _make_source()
        result = _result_with_salary("$150,000-$200,000 a year")
        job = source._parse_result(result)

        assert job is not None
        assert job.salary_min == 150000
        assert job.salary_max == 200000

    def test_parse_result_no_salary(self):
        """Result with no salary key in detected_extensions yields None salary fields."""
        source = _make_source()
        import copy

        result = copy.deepcopy(SAMPLE_SERPAPI_RESULT)
        del result["detected_extensions"]["salary"]
        job = source._parse_result(result)

        assert job is not None
        assert job.salary_min is None
        assert job.salary_max is None

    def test_parse_result_description_from_highlights(self):
        """job_highlights items are joined by newlines to form description."""
        source = _make_source()
        job = source._parse_result(SAMPLE_SERPAPI_RESULT)

        expected = (
            "5+ years experience in data science\n"
            "PhD or Masters in relevant field\n"
            "Lead ML model development\n"
            "Mentor junior data scientists"
        )
        assert job is not None
        assert job.description == expected

    def test_parse_result_no_apply_options_uses_share_link(self):
        """Empty apply_options falls back to share_link for source_url."""
        source = _make_source()
        result = _result_with(apply_options=[])
        job = source._parse_result(result)

        assert job is not None
        assert job.source_url == SAMPLE_SERPAPI_RESULT["share_link"]

    def test_parse_result_no_apply_options_no_share_link_uses_job_id(self):
        """No apply_options and no share_link falls back to job_id for source_url."""
        source = _make_source()
        import copy

        result = copy.deepcopy(SAMPLE_SERPAPI_RESULT)
        result["apply_options"] = []
        del result["share_link"]
        job = source._parse_result(result)

        assert job is not None
        assert job.source_url == SAMPLE_SERPAPI_RESULT["job_id"]

    def test_parse_result_missing_title_returns_none(self):
        """Result with empty title string returns None (skipped)."""
        source = _make_source()
        result = _result_with(title="")
        job = source._parse_result(result)

        assert job is None

    def test_parse_result_missing_company_returns_none(self):
        """Result with empty company_name string returns None (skipped)."""
        source = _make_source()
        result = _result_with(company_name="")
        job = source._parse_result(result)

        assert job is None

    def test_fetch_jobs_calls_search_per_query(self):
        """fetch_jobs runs HTTP requests per query (1 page each) and combines results."""
        source = _make_source()

        # Single result per page → no pagination (< PAGE_SIZE results)
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "jobs_results": [SAMPLE_SERPAPI_RESULT],
        }

        with patch("requests.get", return_value=mock_response) as mock_get:
            queries = [
                {"query": "data scientist", "location": "San Francisco"},
                {"query": "machine learning engineer", "location": "New York"},
            ]
            jobs = source.fetch_jobs(queries, delay=0)

        # 1 page per query (partial page stops pagination)
        assert mock_get.call_count == 2
        assert len(jobs) == 2

    def test_fetch_jobs_sleeps_between_queries(self):
        """fetch_jobs sleeps between consecutive queries, not before the first."""
        source = _make_source()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": []}

        with patch("requests.get", return_value=mock_response):
            with patch("job_finder.sources.serpapi_source.time.sleep") as mock_sleep:
                queries = [
                    {"query": "q1", "location": ""},
                    {"query": "q2", "location": ""},
                    {"query": "q3", "location": ""},
                ]
                source.fetch_jobs(queries, delay=1.5)

        # 3 queries → 2 inter-query sleeps (empty results = no intra-page sleeps)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(1.5)

    def test_fetch_jobs_no_sleep_for_single_query(self):
        """fetch_jobs does not sleep when there is only one query."""
        source = _make_source()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"jobs_results": []}

        with patch("requests.get", return_value=mock_response):
            with patch("job_finder.sources.serpapi_source.time.sleep") as mock_sleep:
                source.fetch_jobs([{"query": "only one", "location": ""}], delay=1.0)

        assert mock_sleep.call_count == 0

    def test_salary_en_dash_specifically_handled(self):
        """Salary regex correctly handles en-dash (U+2013) distinct from hyphen (U+002D)."""
        source = _make_source()
        # Test with explicit en-dash in salary string
        result = _result_with_salary("$120K\u2013$180K a year")
        job = source._parse_result(result)

        assert job is not None
        assert job.salary_min == 120000
        assert job.salary_max == 180000

    def test_parse_result_no_job_highlights_yields_none_description(self):
        """Result with no job_highlights yields None description (not empty string)."""
        source = _make_source()
        result = _result_with(job_highlights=[])
        job = source._parse_result(result)

        assert job is not None
        assert job.description is None

    def test_parse_result_source_is_always_serpapi(self):
        """source field is always 'serpapi' regardless of via field."""
        source = _make_source()
        result = _result_with(via="via Indeed")
        job = source._parse_result(result)

        assert job is not None
        assert job.source == "serpapi"


class TestSerpAPIPagination:
    """Tests for multi-page result fetching via the start parameter."""

    def test_paginates_when_full_page_returned(self):
        """Fetches multiple pages when each returns a full page of results."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=3)

        # Build 10 distinct results per page (full page triggers next fetch)
        def _make_page(n):
            return [_result_with(job_id=f"job-p{n}-{i}") for i in range(10)]

        call_count = 0

        def _mock_get(*args, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            if call_count < 3:
                resp.json.return_value = {"jobs_results": _make_page(call_count)}
            else:
                resp.json.return_value = {"jobs_results": []}
            call_count += 1
            return resp

        with patch("requests.get", side_effect=_mock_get) as mock_get:
            with patch("job_finder.sources.serpapi_source.time.sleep"):
                jobs = source._search("data scientist", "SF")

        # 3 full pages → 3 requests (max_pages=3 stops further pagination)
        assert mock_get.call_count == 3
        assert len(jobs) == 30

    def test_stops_on_partial_page(self):
        """Stops paginating when a page returns fewer than 10 results."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=5)

        call_count = 0

        def _mock_get(*args, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            if call_count == 0:
                resp.json.return_value = {
                    "jobs_results": [_result_with(job_id=f"job-{i}") for i in range(10)]
                }
            elif call_count == 1:
                # Partial page — only 3 results
                resp.json.return_value = {
                    "jobs_results": [_result_with(job_id=f"job-p2-{i}") for i in range(3)]
                }
            else:
                resp.json.return_value = {"jobs_results": []}
            call_count += 1
            return resp

        with patch("requests.get", side_effect=_mock_get) as mock_get:
            with patch("job_finder.sources.serpapi_source.time.sleep"):
                jobs = source._search("data scientist", "SF")

        # Full page + partial page → stops after 2 requests
        assert mock_get.call_count == 2
        assert len(jobs) == 13

    def test_stops_on_empty_page(self):
        """Stops paginating when a page returns zero results."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=5)

        call_count = 0

        def _mock_get(*args, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            if call_count == 0:
                resp.json.return_value = {
                    "jobs_results": [_result_with(job_id=f"job-{i}") for i in range(10)]
                }
            else:
                resp.json.return_value = {"jobs_results": []}
            call_count += 1
            return resp

        with patch("requests.get", side_effect=_mock_get) as mock_get:
            with patch("job_finder.sources.serpapi_source.time.sleep"):
                jobs = source._search("data scientist", "SF")

        assert mock_get.call_count == 2
        assert len(jobs) == 10

    def test_passes_start_parameter(self):
        """Each page request includes the correct start offset."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=3)

        call_count = 0

        def _mock_get(url, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            if call_count < 2:
                resp.json.return_value = {
                    "jobs_results": [
                        _result_with(job_id=f"job-{call_count}-{i}") for i in range(10)
                    ]
                }
            else:
                resp.json.return_value = {"jobs_results": []}
            call_count += 1
            return resp

        with patch("requests.get", side_effect=_mock_get) as mock_get:
            with patch("job_finder.sources.serpapi_source.time.sleep"):
                source._search("data scientist", "SF")

        # Verify start param: page 0 → start=0, page 1 → start=10, page 2 → start=20
        starts = [call.kwargs["params"]["start"] for call in mock_get.call_args_list]
        assert starts == [0, 10, 20]

    def test_max_pages_one_disables_pagination(self):
        """max_pages=1 fetches only the first page (backward-compatible behavior)."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=1)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "jobs_results": [_result_with(job_id=f"job-{i}") for i in range(10)]
        }

        with patch("requests.get", return_value=mock_response) as mock_get:
            jobs = source._search("data scientist", "SF")

        assert mock_get.call_count == 1
        assert len(jobs) == 10

    def test_http_error_mid_pagination_returns_partial(self):
        """HTTP error on page 2 returns results from page 1."""
        source = SerpAPISource(api_key=_TEST_API_KEY, max_pages=5)

        call_count = 0

        def _mock_get(*args, **kwargs):
            nonlocal call_count
            resp = MagicMock()
            if call_count == 0:
                resp.raise_for_status.return_value = None
                resp.json.return_value = {
                    "jobs_results": [_result_with(job_id=f"job-{i}") for i in range(10)]
                }
            else:
                resp.raise_for_status.side_effect = Exception("429 Rate Limited")
            call_count += 1
            return resp

        with patch("requests.get", side_effect=_mock_get):
            with patch("job_finder.sources.serpapi_source.time.sleep"):
                jobs = source._search("data scientist", "SF")

        assert len(jobs) == 10
