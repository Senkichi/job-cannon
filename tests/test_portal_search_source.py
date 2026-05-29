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
# Stage 7.6: title-gate at portal boundary
# ---------------------------------------------------------------------------


class TestFetchAllPortalsTitleGate:
    """Stage 7.6 — `_title_matches` gate applied at fetch_all_portals boundary.

    Closes the documented Stage-0 architectural gap where portal_search's
    upstream ``q=`` full-text matching let non-title-matching rows through
    into the DB and the scorer. The fix mirrors the inline per-job filter
    that every ats_platforms.scan_* function already enforces.
    """

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_filters_off_target_titles(self, mock_rok, mock_rem, mock_him):
        """Off-target titles dropped; on-target retained."""
        mock_rok.return_value = [
            _make_job(title="IT Service Lead", url="https://x/1"),
            _make_job(title="Senior Data Scientist", url="https://x/2"),
            _make_job(title="Compliance Lead", url="https://x/3"),
        ]
        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            target_titles=["Senior Data Scientist"],
        )
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Data Scientist"

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_none_passes_all(self, mock_rok, mock_rem, mock_him):
        """target_titles=None preserves legacy behavior (no filter)."""
        mock_rok.return_value = [
            _make_job(title="IT Service Lead", url="https://x/1"),
            _make_job(title="Senior Data Scientist", url="https://x/2"),
        ]
        jobs = fetch_all_portals(["Engineer"], dataforseo_source=None, target_titles=None)
        assert len(jobs) == 2

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_empty_list_passes_all(self, mock_rok, mock_rem, mock_him):
        """target_titles=[] is equivalent to no gate."""
        mock_rok.return_value = [
            _make_job(title="IT Service Lead", url="https://x/1"),
            _make_job(title="Senior Data Scientist", url="https://x/2"),
        ]
        jobs = fetch_all_portals(["Engineer"], dataforseo_source=None, target_titles=[])
        assert len(jobs) == 2

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_respects_exclusions(self, mock_rok, mock_rem, mock_him):
        """Exclusion keyword overrides target_title match."""
        mock_rok.return_value = [
            _make_job(title="Senior Data Scientist", url="https://x/1"),
            _make_job(title="Senior Data Scientist Intern", url="https://x/2"),
        ]
        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            target_titles=["Data Scientist"],
            exclusions=["Intern"],
        )
        assert len(jobs) == 1
        assert "Intern" not in jobs[0].title

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_uses_word_boundary(self, mock_rok, mock_rem, mock_him):
        """Word-boundary regex — substring matches are rejected."""
        mock_rok.return_value = [
            _make_job(title="Database Administrator", url="https://x/1"),
            _make_job(title="Data Engineer", url="https://x/2"),
        ]
        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            target_titles=["Data"],
        )
        # "Database" should NOT match \bData\b; "Data Engineer" should.
        assert len(jobs) == 1
        assert jobs[0].title == "Data Engineer"

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok")
    def test_gate_normalizes_abbreviations(self, mock_rok, mock_rem, mock_him):
        """`_normalize_title` expansion lets `Sr DS` match `Senior Data Scientist`."""
        mock_rok.return_value = [
            _make_job(title="Sr DS, Growth", url="https://x/1"),
        ]
        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            target_titles=["Senior Data Scientist"],
        )
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


# ---------------------------------------------------------------------------
# Stage 3 — Google CSE backend selection in fetch_serp_portals / fetch_all_portals
# ---------------------------------------------------------------------------


class TestFetchSerpPortalsCseBackend:
    """Stage 3 acceptance criteria — backend selection logic."""

    def test_cse_used_when_only_cse_configured(self):
        """When DataForSEO is None but CSE is set, CSE is the backend."""
        mock_cse = MagicMock()
        mock_cse.fetch_jobs.return_value = [
            _make_job(url="https://wellfound.com/cse-1"),
        ]

        jobs = fetch_serp_portals(
            ["Engineer"],
            dataforseo_source=None,
            google_cse_source=mock_cse,
        )
        assert len(jobs) == 1
        assert jobs[0].source == "portal_wellfound"
        mock_cse.fetch_jobs.assert_called_once()

    def test_dataforseo_preferred_when_both_configured(self):
        """When both are set, DataForSEO wins (no daily quota, supports batching)."""
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/dfse-1")]
        mock_cse = MagicMock()
        mock_cse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/cse-1")]

        jobs = fetch_serp_portals(
            ["Engineer"],
            dataforseo_source=mock_dfse,
            google_cse_source=mock_cse,
        )

        assert len(jobs) == 1
        assert jobs[0].source_url == "https://wellfound.com/dfse-1"
        mock_dfse.fetch_jobs.assert_called_once()
        mock_cse.fetch_jobs.assert_not_called()

    def test_neither_backend_returns_empty(self):
        """Calling with both backends None is a silent no-op (no exception)."""
        jobs = fetch_serp_portals(
            ["Engineer"],
            dataforseo_source=None,
            google_cse_source=None,
        )
        assert jobs == []

    def test_cse_failure_returns_empty(self):
        """CSE-backend exception is logged but caller gets [] (matches DataForSEO)."""
        mock_cse = MagicMock()
        mock_cse.fetch_jobs.side_effect = RuntimeError("CSE 503")

        jobs = fetch_serp_portals(
            ["Engineer"],
            dataforseo_source=None,
            google_cse_source=mock_cse,
        )
        assert jobs == []


class TestFetchAllPortalsCseBackend:
    """Stage 3 — fetch_all_portals wiring of google_cse_source kwarg."""

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_cse_runs_when_dataforseo_none(self, mock_rok, mock_rem, mock_him):
        mock_cse = MagicMock()
        mock_cse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/x")]

        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            google_cse_source=mock_cse,
        )
        assert len(jobs) == 1
        mock_cse.fetch_jobs.assert_called_once()

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_dataforseo_preferred_when_both(self, mock_rok, mock_rem, mock_him):
        mock_dfse = MagicMock()
        mock_dfse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/dfse")]
        mock_cse = MagicMock()
        mock_cse.fetch_jobs.return_value = [_make_job(url="https://wellfound.com/cse")]

        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=mock_dfse,
            google_cse_source=mock_cse,
        )
        assert len(jobs) == 1
        assert jobs[0].source_url == "https://wellfound.com/dfse"
        mock_cse.fetch_jobs.assert_not_called()

    @patch("job_finder.sources.portal_search_source._fetch_himalayas", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remotive", return_value=[])
    @patch("job_finder.sources.portal_search_source._fetch_remoteok", return_value=[])
    def test_neither_skips_serp_tier(self, mock_rok, mock_rem, mock_him):
        """No SERP backend → free portals only, no SERP exceptions."""
        jobs = fetch_all_portals(
            ["Engineer"],
            dataforseo_source=None,
            google_cse_source=None,
        )
        assert jobs == []


# ---------------------------------------------------------------------------
# Stage 7.5 — Portal-source JD parse quality
# ---------------------------------------------------------------------------


class TestStage75Helpers:
    """Unit tests for the parse-hygiene helpers added in Stage 7.5."""

    def test_strip_html_removes_tags_keeps_text(self):
        from job_finder.sources.portal_search_source import _strip_html

        html = (
            "<div><a href='https://x.com'>X</a><h3>About</h3>"
            "<p>We are <b>hiring</b> a <i>staff engineer</i>.</p></div>"
        )
        out = _strip_html(html)
        assert "<" not in out and ">" not in out
        assert "About" in out
        assert "hiring" in out
        assert "staff engineer" in out

    def test_strip_html_preserves_word_boundaries(self):
        """`<b>Foo</b><i>Bar</i>` must not collapse to `FooBar`."""
        from job_finder.sources.portal_search_source import _strip_html

        out = _strip_html("<b>Foo</b><i>Bar</i>")
        assert "Foo" in out and "Bar" in out
        assert "FooBar" not in out

    def test_strip_html_none_and_empty(self):
        from job_finder.sources.portal_search_source import _strip_html

        assert _strip_html(None) == ""
        assert _strip_html("") == ""

    def test_clean_text_repairs_mojibake(self):
        """ftfy round-trips UTF-8-mangled-as-cp1252 back to clean unicode.

        ftfy also normalizes smart-quote U+2019 to ASCII U+0027 by default;
        we keep that behavior because ASCII apostrophes are friendlier for
        downstream substring scans (matcher, scorer prompt construction).
        """
        from job_finder.sources.portal_search_source import _clean_text

        # `weâ€™re` is the canonical "we're" with cp1252 mojibake.
        # ftfy repairs the bytes AND folds U+2019 -> U+0027.
        assert _clean_text("weâ€™re hiring") == "we're hiring"

    def test_clean_text_passes_clean_input_through(self):
        from job_finder.sources.portal_search_source import _clean_text

        assert _clean_text("Paris, Île-de-France") == "Paris, Île-de-France"

    def test_clean_text_none_returns_empty(self):
        from job_finder.sources.portal_search_source import _clean_text

        assert _clean_text(None) == ""
        assert _clean_text("") == ""

    def test_unix_to_datetime_basic(self):
        # Post timezone-normalization (2026-05-29): _unix_to_datetime returns
        # naive UTC so callers can store the value directly into a column
        # following the store-UTC convention.
        from job_finder.sources.portal_search_source import _unix_to_datetime

        dt = _unix_to_datetime(1779443427)
        assert dt is not None
        assert dt.tzinfo is None  # naive UTC
        assert dt.year == 2026 and dt.month == 5

    def test_unix_to_datetime_invalid_inputs(self):
        from job_finder.sources.portal_search_source import _unix_to_datetime

        assert _unix_to_datetime(None) is None
        assert _unix_to_datetime("not-a-number") is None
        assert _unix_to_datetime(0) is None
        assert _unix_to_datetime(-1) is None


class TestStage75HimalayasParseHygiene:
    """Regression guards for Finding #5 — Himalayas parse quality."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_description_html_stripped(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Staff Data Scientist",
                    "companyName": "AnalyticsCo",
                    "applicationLink": "https://himalayas.app/j/strip",
                    "description": (
                        "<div><a href='https://x.com'>X</a>"
                        "<h3>About The Position</h3>"
                        "<p>We are <b>hiring</b> a "
                        "<i>staff data scientist</i> to lead analytics.</p></div>"
                    ),
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["Data Scientist"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        assert "<" not in desc and ">" not in desc
        assert "About The Position" in desc
        assert "hiring" in desc
        assert "staff data scientist" in desc

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_description_truncate_widened_to_8000(self, mock_get):
        """Inputs up to 8000 chars must pass through, matching the jd_full
        eager-promote write width in job_finder/db/_jobs.py:178."""
        # 7500 chars of plain text — should NOT be truncated (was 2000 pre-7.5).
        long_text = "Senior data work. " * 400  # ~7200 chars, no HTML
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Senior DS",
                    "companyName": "BigCo",
                    "applicationLink": "https://himalayas.app/j/long",
                    "description": long_text,
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["DS"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        # Pre-fix: would be truncated to 2000. Post-fix: under 8000 passes through.
        assert len(desc) > 2000

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_posted_date_extracted_from_pubdate(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Lead DS",
                    "companyName": "Co",
                    "applicationLink": "https://himalayas.app/j/dated",
                    "description": "<p>Role.</p>",
                    "pubDate": 1779443427,
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["DS"])
        assert len(jobs) == 1
        pd = jobs[0].posted_date
        assert pd is not None
        assert pd.year == 2026 and pd.month == 5

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_mojibake_repaired_in_description(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "DS",
                    "companyName": "Co",
                    "applicationLink": "https://himalayas.app/j/utf",
                    "description": "<p>We donâ€™t require relocation</p>",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_himalayas

        jobs = _fetch_himalayas(["DS"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        # ftfy folds U+2019 -> U+0027 by default, alongside the cp1252 repair
        assert "don't" in desc
        assert "â€™" not in desc


class TestStage75YcParseHygiene:
    """Regression guards for Finding #5 — YC jd_full population."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_description_crosses_200_char_threshold(self, _mock_sleep, mock_get):
        """YC `jd_full` eager-promote requires description > 200 chars
        (see job_finder/db/_jobs.py:174-180). Synthesizing from metadata
        guarantees this even when only listing fields are available."""
        payload = (
            '{"props":{"jobs":[{"id":7777,"title":"Senior Data Scientist",'
            '"companyName":"AcmeAI","companySlug":"acmeai",'
            '"location":"San Francisco, CA, US / Remote",'
            '"salary":"$180K - $240K","companyOneLiner":'
            '"AI-powered analytics for life sciences companies",'
            '"roleType":"Data Scientist","jobType":"Fulltime",'
            '"companyBatch":"W24"}]}}'
        )
        html_body = f'<div data-page="{payload.replace(chr(34), "&quot;")}"></div>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        jobs = _fetch_yc_workatastartup(["Data Scientist"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        assert len(desc) > 200, f"description must exceed 200 chars, got {len(desc)}"

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_description_contains_role_and_company_signal(self, _mock_sleep, mock_get):
        """Scorer needs title + role + company context, not just a tagline."""
        payload = (
            '{"props":{"jobs":[{"id":7778,"title":"Staff Engineer",'
            '"companyName":"FintechCo","companySlug":"fintechco",'
            '"location":"Remote","salary":"$200K - $260K",'
            '"companyOneLiner":"Payments rails for marketplaces",'
            '"roleType":"Backend engineer","jobType":"Fulltime",'
            '"companyBatch":"S23"}]}}'
        )
        html_body = f'<div data-page="{payload.replace(chr(34), "&quot;")}"></div>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        jobs = _fetch_yc_workatastartup(["Engineer"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        assert "Staff Engineer" in desc
        assert "FintechCo" in desc
        assert "Backend engineer" in desc
        assert "S23" in desc
        assert "Payments rails for marketplaces" in desc
        assert "$200K - $260K" in desc
        # Honest about the limitation
        assert "YC login" in desc

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_synthesis_handles_missing_optional_fields(self, _mock_sleep, mock_get):
        """When YC ships only title+company, description still builds without error."""
        payload = (
            '{"props":{"jobs":[{"id":7779,"title":"Engineer",'
            '"companyName":"X","companySlug":"x"}]}}'
        )
        html_body = f'<div data-page="{payload.replace(chr(34), "&quot;")}"></div>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        jobs = _fetch_yc_workatastartup(["X"])
        assert len(jobs) == 1
        desc = jobs[0].description or ""
        assert "Engineer" in desc
        assert "X" in desc
        # Honest closing line is always present
        assert "YC login" in desc

    @patch("job_finder.sources.portal_search_source.requests.get")
    @patch("job_finder.sources.portal_search_source.time.sleep")
    def test_mojibake_repaired_in_location(self, _mock_sleep, mock_get):
        """Per the shakedown: YC location field was the canonical mojibake site."""
        payload = (
            '{"props":{"jobs":[{"id":7780,"title":"DS",'
            '"companyName":"FrCo","companySlug":"frco",'
            '"location":"Paris, Île-de-France, FR",'
            '"companyOneLiner":"French analytics shop","roleType":"DS"}]}}'
        )
        html_body = f'<div data-page="{payload.replace(chr(34), "&quot;")}"></div>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_body
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_yc_workatastartup

        jobs = _fetch_yc_workatastartup(["DS"])
        assert len(jobs) == 1
        # ftfy preserves clean unicode untouched
        assert "Île-de-France" in (jobs[0].location or "")


class TestStage78JobicyMojibakeRepair:
    """Regression guards for Stage 7.8 — Jobicy parse hygiene.

    Surfaced during the Stage 7.8 keyword-breadth re-verification: live
    Jobicy responses ship with U+FFFD replacement characters in some
    titles (e.g. "Senior Consultant � Vault CRM"). Applying _clean_text
    via ftfy is the same defense pattern from Stage 7.5's Himalayas/YC
    fixes — per the handoff policy, ftfy adoption is reactive on
    observed mojibake.
    """

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_title_mojibake_repaired(self, mock_get):
        """ftfy fixes cp1252-as-UTF-8 round-trip mojibake in titles."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "jobTitle": "Weâ€™re hiring: Senior Analyst",
                    "companyName": "AcmeCo",
                    "jobGeo": "Remote",
                    "url": "https://jobicy.com/jobs/1",
                    "jobDescription": "Join our team",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jobicy

        jobs = _fetch_jobicy([])
        assert len(jobs) == 1
        # ftfy normalizes U+2019 to U+0027 (ASCII apostrophe) by default
        assert "we're hiring: senior analyst" in jobs[0].title.lower()
        # The mangled bytes must not appear
        assert "â€™" not in jobs[0].title

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_company_and_location_mojibake_repaired(self, mock_get):
        """Defense-in-depth: clean text fields in addition to title."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "jobTitle": "Engineer",
                    "companyName": "Sociétê AcmeFR",
                    "jobGeo": "Paris, Île-de-France",
                    "url": "https://jobicy.com/jobs/2",
                    "jobDescription": "Description text",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jobicy

        jobs = _fetch_jobicy([])
        assert len(jobs) == 1
        # ftfy preserves clean unicode untouched
        assert "Île-de-France" in (jobs[0].location or "")
        # Société is left alone if not mangled (the input is intentionally a mix)
        assert jobs[0].company  # non-empty, not crashed

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_missing_description_does_not_crash(self, mock_get):
        """Both jobDescription and jobExcerpt absent must not raise."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "jobTitle": "Engineer",
                    "companyName": "MinimalCo",
                    "jobGeo": "Remote",
                    "url": "https://jobicy.com/jobs/3",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jobicy

        jobs = _fetch_jobicy([])
        assert len(jobs) == 1
        # Empty string OK; eager-promote logic in upsert_job skips short/empty
        assert jobs[0].description in ("", None)


# ---------------------------------------------------------------------------
# Proactive ftfy adoption (post-Stage-7.8 — extends Jobicy/Himalayas pattern
# to the remaining 5 portal fetchers: RemoteOK, Remotive, USAJobs, Adzuna,
# Jooble). Defense-in-depth — these portals haven't surfaced mojibake yet
# but the cleanup is mechanically identical and cheap.
# ---------------------------------------------------------------------------


class TestRemoteokMojibakeRepair:
    """ftfy hygiene for RemoteOK title/company/location/description fields."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_title_company_location_mojibake_repaired(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # RemoteOK returns [metadata, listing1, listing2, ...]
        mock_resp.json.return_value = [
            {"legal": "metadata"},
            {
                "position": "Weâ€™re hiring: Senior Engineer",
                "company": "Sociâ€™étê",
                "location": "Île-de-France",
                "description": "Joinâ€™ our team",
                "tags": ["engineer"],
                "url": "https://remoteok.com/jobs/1",
            },
        ]
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remoteok

        jobs = _fetch_remoteok(["engineer"])
        assert len(jobs) == 1
        assert "â€™" not in jobs[0].title
        assert "â€™" not in jobs[0].company
        assert "we're hiring: senior engineer" in jobs[0].title.lower()

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_missing_description_does_not_crash(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"legal": "metadata"},
            {
                "position": "Engineer",
                "company": "MinimalCo",
                "tags": ["engineer"],
                "url": "https://remoteok.com/jobs/2",
            },
        ]
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remoteok

        jobs = _fetch_remoteok(["engineer"])
        assert len(jobs) == 1
        assert jobs[0].description in ("", None)


class TestRemotiveMojibakeRepair:
    """ftfy hygiene for Remotive title/company/location/description fields."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_title_company_location_mojibake_repaired(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Weâ€™re hiring Engineer",
                    "company_name": "Sociâ€™étê",
                    "candidate_required_location": "Île-de-France",
                    "description": "Joinâ€™ us",
                    "tags": ["engineer"],
                    "url": "https://remotive.com/jobs/1",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remotive

        jobs = _fetch_remotive(["engineer"])
        assert len(jobs) == 1
        assert "â€™" not in jobs[0].title
        assert "â€™" not in jobs[0].company
        assert "we're hiring engineer" in jobs[0].title.lower()

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_missing_description_does_not_crash(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Engineer",
                    "company_name": "MinimalCo",
                    "tags": ["engineer"],
                    "url": "https://remotive.com/jobs/2",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_remotive

        jobs = _fetch_remotive(["engineer"])
        assert len(jobs) == 1
        assert jobs[0].description in ("", None)


class TestUsajobsMojibakeRepair:
    """ftfy hygiene for USAJobs title/company/location/description fields."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_title_company_location_mojibake_repaired(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "SearchResult": {
                "SearchResultItems": [
                    {
                        "MatchedObjectDescriptor": {
                            "PositionTitle": "Weâ€™re hiring Analyst",
                            "OrganizationName": "Sociâ€™étê Federal",
                            "PositionLocation": [
                                {"LocationName": "San Joséâ€™, CA"}
                            ],
                            "PositionURI": "https://usajobs.gov/jobs/1",
                            "UserArea": {
                                "Details": {"JobSummary": "Joinâ€™ the team"}
                            },
                        }
                    }
                ]
            }
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_usajobs

        jobs = _fetch_usajobs(
            ["analyst"],
            user_agent_email="test@example.com",
            authorization_key="dummy",
        )
        assert len(jobs) == 1
        assert "â€™" not in jobs[0].title
        assert "â€™" not in jobs[0].company
        assert "we're hiring analyst" in jobs[0].title.lower()

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_missing_description_does_not_crash(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "SearchResult": {
                "SearchResultItems": [
                    {
                        "MatchedObjectDescriptor": {
                            "PositionTitle": "Analyst",
                            "OrganizationName": "MinimalCo",
                            "PositionURI": "https://usajobs.gov/jobs/2",
                        }
                    }
                ]
            }
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_usajobs

        jobs = _fetch_usajobs(
            ["analyst"],
            user_agent_email="test@example.com",
            authorization_key="dummy",
        )
        assert len(jobs) == 1
        assert jobs[0].description in ("", None)


class TestAdzunaMojibakeRepair:
    """ftfy hygiene for Adzuna title/company/location/description fields."""

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_title_company_location_mojibake_repaired(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Weâ€™re hiring Engineer",
                    "company": {"display_name": "Sociâ€™étê"},
                    "location": {"display_name": "Île-de-France"},
                    "description": "Joinâ€™ us",
                    "redirect_url": "https://adzuna.com/jobs/1",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_adzuna

        jobs = _fetch_adzuna(
            ["engineer"], app_id="dummy", app_key="dummy"
        )
        assert len(jobs) == 1
        assert "â€™" not in jobs[0].title
        assert "â€™" not in jobs[0].company
        assert "we're hiring engineer" in jobs[0].title.lower()

    @patch("job_finder.sources.portal_search_source.requests.get")
    def test_missing_description_does_not_crash(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Engineer",
                    "company": {"display_name": "MinimalCo"},
                    "location": {"display_name": "Remote"},
                    "redirect_url": "https://adzuna.com/jobs/2",
                }
            ]
        }
        mock_get.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_adzuna

        jobs = _fetch_adzuna(
            ["engineer"], app_id="dummy", app_key="dummy"
        )
        assert len(jobs) == 1
        assert jobs[0].description in ("", None)


class TestJoobleMojibakeRepair:
    """ftfy hygiene for Jooble title/company/location/snippet fields."""

    @patch("job_finder.sources.portal_search_source.requests.post")
    def test_title_company_location_mojibake_repaired(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Weâ€™re hiring Engineer",
                    "company": "Sociâ€™étê",
                    "location": "Île-de-France",
                    "snippet": "Joinâ€™ us",
                    "link": "https://jooble.org/jobs/1",
                }
            ]
        }
        mock_post.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jooble

        jobs = _fetch_jooble(["engineer"], api_key="dummy")
        assert len(jobs) == 1
        assert "â€™" not in jobs[0].title
        assert "â€™" not in jobs[0].company
        assert "we're hiring engineer" in jobs[0].title.lower()

    @patch("job_finder.sources.portal_search_source.requests.post")
    def test_missing_snippet_does_not_crash(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Engineer",
                    "company": "MinimalCo",
                    "link": "https://jooble.org/jobs/2",
                }
            ]
        }
        mock_post.return_value = mock_resp

        from job_finder.sources.portal_search_source import _fetch_jooble

        jobs = _fetch_jooble(["engineer"], api_key="dummy")
        assert len(jobs) == 1
        assert jobs[0].description in ("", None)
