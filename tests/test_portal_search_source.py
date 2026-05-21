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


# ---------------------------------------------------------------------------
# Stage 2 — free portal fetchers (Jobicy, YC, USAJobs, Adzuna, Jooble)
# ---------------------------------------------------------------------------


class TestFetchJobicy:
    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_jobicy_basic(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "jobTitle": "Senior Data Scientist",
                    "companyName": "RemoteCo",
                    "jobGeo": "Worldwide",
                    "url": "https://jobicy.com/j/1",
                    "annualSalaryMin": 150000,
                    "annualSalaryMax": 210000,
                    "jobDescription": "Senior data scientist role",
                    "jobExcerpt": "Data science team",
                },
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jobicy

        jobs = _fetch_jobicy(["Data Scientist"])

        assert len(jobs) == 1
        assert jobs[0].source == "portal_jobicy"
        assert jobs[0].title == "Senior Data Scientist"
        assert jobs[0].salary_min == 150000

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_jobicy_keyword_filter(self, mock_get):
        """Listings whose text doesn't match any keyword are dropped."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "jobTitle": "Frontend Engineer",
                    "companyName": "WebCo",
                    "jobDescription": "React work",
                    "url": "https://jobicy.com/j/2",
                },
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jobicy

        jobs = _fetch_jobicy(["Data Scientist"])
        assert jobs == []

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_jobicy_failure_returns_empty(self, mock_get):
        mock_get.side_effect = ConnectionError("boom")

        from job_finder.sources.portal_search_source import _fetch_jobicy

        assert _fetch_jobicy(["Engineer"]) == []


class TestFetchYcWorkatastartup:
    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_yc_basic(self, _mock_sleep, mock_get):
        # Minimal Inertia data-page payload
        payload = (
            '{"props":{"jobs":[{"id":1234,"title":"Staff Data Scientist",'
            '"companyName":"YCAlphaCo","companySlug":"alphaco",'
            '"location":"San Francisco","salary":"$170K - $220K",'
            '"companyOneLiner":"YC-backed analytics startup"}]}}'
        )
        # data-page must be HTML-escaped — emulate the real page shape
        html_body = f'<div id="root" data-page="{payload.replace(chr(34), "&quot;")}"></div>'

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        jobs = _fetch_yc_workatastartup(["Data Scientist"])
        assert len(jobs) == 1
        assert jobs[0].source == "portal_yc_workatastartup"
        assert jobs[0].company == "YCAlphaCo"
        assert jobs[0].salary_min == 170000
        assert "alphaco" in jobs[0].source_url and "1234" in jobs[0].source_url

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_yc_missing_data_page(self, _mock_sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>no inertia data here</body></html>"
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        assert _fetch_yc_workatastartup(["Engineer"]) == []

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_yc_failure_returns_empty(self, mock_get):
        mock_get.side_effect = ConnectionError("boom")

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        assert _fetch_yc_workatastartup(["Engineer"]) == []


class TestFetchUsajobs:
    def test_usajobs_missing_creds_short_circuits(self):
        from job_finder.sources.portal_search_source import _fetch_usajobs

        # No network call should happen — caller's guard returns [] immediately.
        assert _fetch_usajobs(["Analyst"], user_agent_email="", authorization_key="") == []
        assert _fetch_usajobs(["Analyst"], user_agent_email="me@x", authorization_key="") == []

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_usajobs_basic(self, _mock_sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "SearchResult": {
                "SearchResultItems": [
                    {
                        "MatchedObjectDescriptor": {
                            "PositionTitle": "Operations Research Analyst",
                            "OrganizationName": "Department of Defense",
                            "PositionURI": "https://usajobs.gov/Job/1",
                            "PositionLocation": [{"LocationName": "Washington, DC"}],
                            "PositionRemuneration": [
                                {"MinimumRange": "120000", "MaximumRange": "180000"}
                            ],
                            "UserArea": {"Details": {"JobSummary": "Analyst role"}},
                        }
                    }
                ]
            }
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_usajobs

        jobs = _fetch_usajobs(
            ["Analyst"], user_agent_email="me@x.com", authorization_key="key123"
        )
        assert len(jobs) == 1
        assert jobs[0].source == "portal_usajobs"
        assert jobs[0].company == "Department of Defense"
        assert jobs[0].salary_min == 120000
        # Confirm required headers were sent.
        sent_headers = mock_get.call_args.kwargs["headers"]
        assert sent_headers["User-Agent"] == "me@x.com"
        assert sent_headers["Authorization-Key"] == "key123"

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_usajobs_failure_returns_empty(self, mock_get):
        mock_get.side_effect = ConnectionError("boom")

        from job_finder.sources.portal_search_source import _fetch_usajobs

        assert (
            _fetch_usajobs(["Analyst"], user_agent_email="me@x", authorization_key="k")
            == []
        )


class TestFetchAdzuna:
    def test_adzuna_missing_creds_short_circuits(self):
        from job_finder.sources.portal_search_source import _fetch_adzuna

        assert _fetch_adzuna(["Engineer"], app_id="", app_key="") == []
        assert _fetch_adzuna(["Engineer"], app_id="id", app_key="") == []

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_adzuna_basic(self, _mock_sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Lead Data Analyst",
                    "company": {"display_name": "BigCo"},
                    "location": {"display_name": "Austin, TX"},
                    "redirect_url": "https://adzuna.com/j/1",
                    "salary_min": 130000.0,
                    "salary_max": 170000.0,
                    "description": "Lead analytics work",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_adzuna

        jobs = _fetch_adzuna(["Lead Analyst"], app_id="id", app_key="k", country="us")
        assert len(jobs) == 1
        assert jobs[0].source == "portal_adzuna"
        assert jobs[0].company == "BigCo"
        assert jobs[0].location == "Austin, TX"
        assert jobs[0].salary_min == 130000

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_adzuna_dedup_across_keywords(self, _mock_sleep, mock_get):
        """Same company+title across keywords should only appear once."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Lead Analyst",
                    "company": {"display_name": "Acme"},
                    "redirect_url": "https://adzuna.com/j/same",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_adzuna

        jobs = _fetch_adzuna(["kw1", "kw2"], app_id="id", app_key="k")
        assert len(jobs) == 1

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_adzuna_failure_returns_empty(self, mock_get):
        mock_get.side_effect = ConnectionError("boom")

        from job_finder.sources.portal_search_source import _fetch_adzuna

        assert _fetch_adzuna(["Engineer"], app_id="id", app_key="k") == []


class TestFetchJooble:
    def test_jooble_missing_key_short_circuits(self):
        from job_finder.sources.portal_search_source import _fetch_jooble

        assert _fetch_jooble(["Engineer"], api_key="") == []

    @patch("job_finder.sources.portal_search_source.requests.post")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_jooble_basic(self, _mock_sleep, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "totalCount": 1,
            "jobs": [
                {
                    "title": "Senior Data Scientist",
                    "company": "Joubli Co",
                    "location": "Remote",
                    "link": "https://jooble.org/j/1",
                    "salary": "$140K - $190K",
                    "snippet": "Senior DS role",
                }
            ],
        }
        mock_post.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jooble

        jobs = _fetch_jooble(["Data Scientist"], api_key="abc")
        assert len(jobs) == 1
        assert jobs[0].source == "portal_jooble"
        assert jobs[0].salary_min == 140000
        # POST body carries the keyword
        body = mock_post.call_args.kwargs["json"]
        assert body["keywords"] == "Data Scientist"

    @patch("job_finder.sources.portal_search_source.requests.post")
    def test_jooble_failure_returns_empty(self, mock_post):
        mock_post.side_effect = ConnectionError("boom")

        from job_finder.sources.portal_search_source import _fetch_jooble

        assert _fetch_jooble(["Engineer"], api_key="abc") == []


# ---------------------------------------------------------------------------
# Stage 2 — fetch_all_portals config-gated dispatch
# ---------------------------------------------------------------------------


class TestFetchAllPortalsStage2:
    @patch("job_finder.sources.portal_search_source._fetch_jooble")
    @patch("job_finder.sources.portal_search_source._fetch_adzuna")
    @patch("job_finder.sources.portal_search_source._fetch_usajobs")
    @patch("job_finder.sources.portal_search_source._fetch_yc_workatastartup")
    @patch("job_finder.sources.portal_search_source._fetch_jobicy")
    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_stage2_portals_disabled_by_default(
        self,
        _rok,
        _rem,
        _him,
        mock_jobicy,
        mock_yc,
        mock_usajobs,
        mock_adzuna,
        mock_jooble,
    ):
        """When portal_config is omitted, the Stage 2 fetchers are NOT called."""
        fetch_all_portals(["Engineer"], dataforseo_source=None)
        mock_jobicy.assert_not_called()
        mock_yc.assert_not_called()
        mock_usajobs.assert_not_called()
        mock_adzuna.assert_not_called()
        mock_jooble.assert_not_called()

    @patch("job_finder.sources.portal_search_source._fetch_jooble", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_adzuna", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_usajobs", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_yc_workatastartup", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_jobicy", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_stage2_portals_enabled_via_config(
        self,
        _rok,
        _rem,
        _him,
        mock_jobicy,
        mock_yc,
        mock_usajobs,
        mock_adzuna,
        mock_jooble,
    ):
        """Enabling a portal in portal_config wires it into the dispatch loop."""
        portal_config = {
            "jobicy": {"enabled": True},
            "yc_workatastartup": {"enabled": True},
            "usajobs": {
                "enabled": True,
                "user_agent_email": "me@x.com",
                "authorization_key": "k",
            },
            "adzuna": {"enabled": True, "app_id": "id", "app_key": "k", "country": "gb"},
            "jooble": {"enabled": True, "api_key": "abc"},
        }
        fetch_all_portals(
            ["Engineer"], dataforseo_source=None, portal_config=portal_config
        )

        mock_jobicy.assert_called_once()
        mock_yc.assert_called_once()
        mock_usajobs.assert_called_once_with(
            ["Engineer"], user_agent_email="me@x.com", authorization_key="k"
        )
        mock_adzuna.assert_called_once_with(
            ["Engineer"], app_id="id", app_key="k", country="gb"
        )
        mock_jooble.assert_called_once_with(["Engineer"], api_key="abc")
