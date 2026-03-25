"""Tests for serpapi_source.py — SerpAPI Google Jobs response format audit.

# AUDIT 2026-03-15: SerpAPI Google Jobs response fields verified against current API schema.
# Fields: title, company_name, location, job_highlights, detected_extensions.salary,
# apply_options, share_link, job_id. All field mappings confirmed current.
"""

from unittest.mock import MagicMock, patch

import pytest

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

def _make_source() -> SerpAPISource:
    return SerpAPISource(api_key="test-key-does-not-matter")  # nosec B106 -- dummy placeholder, not a real credential


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
        assert job.source_id == "eyJqb2JfdGl0bGUiOiJTZW5pb3IgRGF0YSBTY2llbnRpc3QiLCJjb21wYW55X25hbWUiOiJBY21lIENvcnAifQ=="

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
        """fetch_jobs runs one HTTP request per query and combines results."""
        source = _make_source()

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
            jobs = source.fetch_jobs(queries)

        assert mock_get.call_count == 2
        assert len(jobs) == 2

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
