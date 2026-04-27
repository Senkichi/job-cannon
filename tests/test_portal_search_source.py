"""Tests for portal-targeted job discovery — free APIs + SERP fallback."""

from unittest.mock import MagicMock, patch

from job_finder.models import Job
from job_finder.sources.portal_search_source import (
    SERP_PORTALS,
    _detect_portal_from_url,
    _parse_salary_string,
    _safe_int,
    fetch_all_portals,
    fetch_serp_portals,
)


def _make_job(title="Engineer", company="Acme", url="https://example.com/1"):
    return Job(
        title=title,
        company=company,
        location="Remote",
        source="test",
        source_url=url,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_safe_int(self):
        assert _safe_int(100000) == 100000
        assert _safe_int("50000") == 50000
        assert _safe_int(None) is None
        assert _safe_int("bad") is None

    def test_parse_salary_string(self):
        assert _parse_salary_string("$150K - $200K") == (150000, 200000)
        assert _parse_salary_string("$150,000 - $200,000") == (150000, 200000)
        assert _parse_salary_string("") == (None, None)
        assert _parse_salary_string("negotiable") == (None, None)

    def test_detect_portal_from_url(self):
        portals = [{"domain": "lever.co", "name": "lever"}]
        assert _detect_portal_from_url("https://jobs.lever.co/acme/123", portals) == "lever"
        assert _detect_portal_from_url("https://unknown.com/job", portals) is None


# ---------------------------------------------------------------------------
# Free API fetchers
# ---------------------------------------------------------------------------


class TestFreeAPIs:
    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_remoteok_basic(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {},  # metadata row
            {
                "position": "Staff Engineer",
                "company": "Acme",
                "location": "Remote",
                "apply_url": "https://remoteok.com/1",
                "description": "Looking for a staff engineer",
                "tags": ["python"],
                "salary_min": 150000,
                "salary_max": 200000,
            },
            {
                "position": "Junior Designer",
                "company": "DesignCo",
                "location": "Remote",
                "apply_url": "https://remoteok.com/2",
                "description": "UI design role",
                "tags": ["figma"],
            },
        ]
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remoteok

        jobs = _fetch_remoteok(["Staff Engineer"])

        assert len(jobs) == 1
        assert jobs[0].source == "portal_remoteok"
        assert jobs[0].title == "Staff Engineer"
        assert jobs[0].salary_min == 150000

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_remoteok_failure_returns_empty(self, mock_get):
        mock_get.side_effect = ConnectionError("timeout")

        from job_finder.sources.portal_search_source import _fetch_remoteok

        jobs = _fetch_remoteok(["Engineer"])
        assert jobs == []

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_remotive_basic(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "ML Platform Engineer",
                    "company_name": "DataCo",
                    "url": "https://remotive.com/1",
                    "candidate_required_location": "Worldwide",
                    "description": "ML platform work",
                    "tags": ["python", "ml"],
                    "salary": "$180K - $220K",
                },
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remotive

        jobs = _fetch_remotive(["ML Platform"])

        assert len(jobs) == 1
        assert jobs[0].source == "portal_remotive"
        assert jobs[0].salary_min == 180000

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_himalayas_basic(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Data Infrastructure Lead",
                    "companyName": "InfraCo",
                    "applicationLink": "https://himalayas.app/j/1",
                    "location": "Remote",
                    "minSalary": 160000,
                    "maxSalary": 210000,
                    "description": "Infrastructure role",
                },
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["Data Infrastructure"])

        assert len(jobs) == 1
        assert jobs[0].source == "portal_himalayas"
        assert jobs[0].salary_min == 160000

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_himalayas_dedup_across_keywords(self, mock_get):
        """Same job returned for two keywords should be deduped."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Staff Engineer",
                    "companyName": "Acme",
                    "applicationLink": "https://himalayas.app/j/same",
                    "description": "Staff data infrastructure engineer",
                },
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["Staff Engineer", "Data Infrastructure"])

        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# SERP portal search
# ---------------------------------------------------------------------------


class TestFetchSerpPortals:
    def test_basic_batch(self):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [
            _make_job(url="https://wellfound.com/j/1"),
        ]

        jobs = fetch_serp_portals(["Engineer"], mock_dfse)
        assert len(jobs) == 1
        assert jobs[0].source == "portal_wellfound"
        mock_dfse.fetch_jobs.assert_called_once()

    def test_max_queries_cap(self):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = []

        fetch_serp_portals(["kw1", "kw2", "kw3"], mock_dfse, max_queries=5)
        call_args = mock_dfse.fetch_jobs.call_args[0][0]
        assert len(call_args) <= 5

    def test_url_dedup(self):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [
            _make_job(url="https://same.com/1"),
            _make_job(title="Other", url="https://same.com/1"),
        ]

        jobs = fetch_serp_portals(["Engineer"], mock_dfse)
        assert len(jobs) == 1

    def test_failure_returns_empty(self):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.side_effect = ConnectionError("boom")

        jobs = fetch_serp_portals(["Engineer"], mock_dfse)
        assert jobs == []


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


class TestFetchAllPortals:
    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_free_only_no_dataforseo(self, mock_rok, mock_rem, mock_him):
        mock_rok.return_value = [_make_job(url="https://remoteok.com/1")]
        jobs = fetch_all_portals(["Engineer"], dataforseo_source=None)

        assert len(jobs) == 1
        assert jobs[0].source_url == "https://remoteok.com/1"

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_with_dataforseo(self, mock_rok, mock_rem, mock_him):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/1")]

        jobs = fetch_all_portals(["Engineer"], dataforseo_source=mock_dfse)
        assert len(jobs) == 1
        mock_dfse.fetch_jobs.assert_called_once()

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_cross_portal_dedup(self, mock_rok, mock_rem, mock_him):
        """Same URL from free API and SERP should be deduped."""
        mock_rok.return_value = [_make_job(url="https://example.com/same")]
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [_make_job(url="https://example.com/same")]

        jobs = fetch_all_portals(["Engineer"], dataforseo_source=mock_dfse)
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Ingestion runner integration
# ---------------------------------------------------------------------------


class TestFetchPortalSearchIntegration:
    def test_disabled_returns_empty(self):
        from job_finder.web.ingestion_runner import _fetch_portal_search

        summary = {}
        result = _fetch_portal_search({"sources": {"portal_search": {"enabled": False}}}, summary)
        assert result == []

    def test_no_keywords_returns_empty(self):
        from job_finder.web.ingestion_runner import _fetch_portal_search

        summary = {}
        result = _fetch_portal_search(
            {"sources": {"portal_search": {"enabled": True, "keywords": []}}},
            summary,
        )
        assert result == []


class TestSerpPortalsConstant:
    def test_has_entries(self):
        assert len(SERP_PORTALS) >= 8
        for portal in SERP_PORTALS:
            assert "domain" in portal
            assert "name" in portal

    def test_free_portals_not_in_serp_list(self):
        """RemoteOK, Remotive, Himalayas should NOT be in SERP_PORTALS."""
        names = {p["name"] for p in SERP_PORTALS}
        assert "remoteok" not in names
        assert "remotive" not in names
        assert "himalayas" not in names
