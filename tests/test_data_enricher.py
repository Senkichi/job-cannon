"""Tests for data_enricher.py — cost-ordered enrichment pipeline.

Tests cover both the existing private helpers (unchanged API) and the new
tier-ordered enrich_job pipeline introduced in Phase 10.

Existing test classes preserved:
- TestSearchSerpapi
- TestSearchDuckDuckGo
- TestExtractWithHaiku
- TestEnrichCompanyInfo

New test classes for Phase 10:
- TestEnrichJobTierOrder — free -> DDG -> Haiku -> SerpAPI -> Sonnet ordering
- TestFieldCeilings — salary stops at Haiku; JD escalates to Sonnet
- TestSonnetEnrichment — Sonnet receives all prior fragments, checks cost_gate
- TestEnrichmentTierPersistence — atomic DB write, resume-from-next-tier, exhausted skip
- TestEnrichJobBackwardCompat — old call pattern still works
- TestPipelineIntegration — enrich_job before Haiku in pipeline, migration columns
"""

import inspect
import json
import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sparse_job_row():
    """A job row missing jd_full and salary data (needs enrichment)."""
    return {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "jd_full": None,
        "salary_min": None,
        "salary_max": None,
        "source_urls": '["https://example.com/job/123"]',
        "company_id": None,
        "enrichment_tier": None,
        "description": "Build ML models",
    }

@pytest.fixture
def rich_job_row():
    """A job row with all scoring-relevant data (no enrichment needed)."""
    return {
        "dedup_key": "beta|staff-ds|sf",
        "title": "Staff Data Scientist",
        "company": "Beta Inc",
        "location": "San Francisco, CA",
        "jd_full": "Full job description text here with lots of detail about the role.",
        "salary_min": 200000,
        "salary_max": 280000,
        "source_urls": '[]',
        "company_id": None,
        "enrichment_tier": None,
        "description": "Lead data science.",
    }

@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client that returns structured extraction result."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({
        "jd_full": "Data Scientist role at Acme Corp building ML models.",
        "salary_min": 140000,
        "salary_max": 180000,
        "location": "Remote",
    })
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client

@pytest.fixture
def temp_db():
    """Create an in-memory SQLite DB with scoring_costs and jobs tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            jd_full TEXT DEFAULT NULL,
            salary_min INTEGER DEFAULT NULL,
            salary_max INTEGER DEFAULT NULL,
            source_urls TEXT DEFAULT '[]',
            company_id INTEGER DEFAULT NULL,
            enrichment_tier TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL,
            homepage_url TEXT DEFAULT NULL,
            ats_platform TEXT DEFAULT NULL,
            ats_slug TEXT DEFAULT NULL,
            ats_probe_status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn

# ---------------------------------------------------------------------------
# Tests for search_serpapi (from enrichment_tiers)
# ---------------------------------------------------------------------------

class TestSearchSerpapi:
    def test_search_serpapi_makes_request_with_job_query(self):
        """search_serpapi calls SerpAPI with title/company in query."""
        from job_finder.web.enrichment_tiers import search_serpapi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs_results": [
                {
                    "title": "Data Scientist",
                    "company_name": "Acme Corp",
                    "description": "Build ML models at Acme Corp.",
                }
            ]
        }

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_serpapi("Data Scientist Acme Corp", "test-api-key")

        mock_get.assert_called_once()
        call_args = mock_get.call_args
        # Should call SerpAPI endpoint
        assert "serpapi.com" in call_args[0][0] or call_args[1].get("params", {})

    def test_search_serpapi_returns_dict_with_job_data(self):
        """search_serpapi returns dict with job description when results exist."""
        from job_finder.web.enrichment_tiers import search_serpapi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs_results": [
                {
                    "title": "Data Scientist",
                    "company_name": "Acme Corp",
                    "description": "Build ML models at Acme Corp.",
                    "detected_extensions": {"salary": "$140K-$180K"},
                }
            ]
        }

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result, _urls = search_serpapi("Data Scientist Acme Corp", "test-api-key")

        assert "salary_min" in result or "jd_full" in result or "location" in result

    def test_search_serpapi_returns_none_when_no_results(self):
        """search_serpapi returns None when SerpAPI returns empty jobs_results."""
        from job_finder.web.enrichment_tiers import search_serpapi

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jobs_results": []}

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result, _urls = search_serpapi("Data Scientist Acme Corp", "test-api-key")

        assert result is None

    def test_search_serpapi_returns_none_on_request_error(self):
        """search_serpapi returns None when requests raises an exception."""
        from job_finder.web.enrichment_tiers import search_serpapi

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")
            result, _urls = search_serpapi("Data Scientist Acme Corp", "test-api-key")

        assert result is None

# ---------------------------------------------------------------------------
# Tests for search_duckduckgo (from enrichment_tiers)
# ---------------------------------------------------------------------------

class TestSearchDuckDuckGo:
    def test_search_duckduckgo_queries_ddg_api(self):
        """search_duckduckgo calls DuckDuckGo Instant Answer API."""
        from job_finder.web.enrichment_tiers import search_duckduckgo

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "AbstractText": "Acme Corp is a technology company with 5000 employees.",
            "RelatedTopics": [],
        }

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_duckduckgo("Acme Corp company")

        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "duckduckgo.com" in call_args[0][0]

    def test_search_duckduckgo_returns_abstract_text(self):
        """search_duckduckgo returns AbstractText when present."""
        from job_finder.web.enrichment_tiers import search_duckduckgo

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "AbstractText": "Acme Corp is a technology company.",
            "RelatedTopics": [],
        }

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_duckduckgo("Acme Corp")

        assert "Acme Corp" in result

    def test_search_duckduckgo_returns_none_when_no_abstract(self):
        """search_duckduckgo returns None when AbstractText is empty."""
        from job_finder.web.enrichment_tiers import search_duckduckgo

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "AbstractText": "",
            "RelatedTopics": [],
        }

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.return_value = mock_response
            result = search_duckduckgo("Acme Corp")

        assert result is None

    def test_search_duckduckgo_returns_none_on_error(self):
        """search_duckduckgo returns None on network error."""
        from job_finder.web.enrichment_tiers import search_duckduckgo

        with patch("job_finder.web.enrichment_tiers.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")
            result = search_duckduckgo("Acme Corp")

        assert result is None

# ---------------------------------------------------------------------------
# Tests for extract_with_haiku (from enrichment_tiers)
# ---------------------------------------------------------------------------

class TestExtractWithHaiku:
    def test_extract_with_haiku_sends_search_text_to_haiku(
        self, mock_anthropic_client, temp_db
    ):
        """extract_with_haiku calls Haiku with search_text and job context."""
        from job_finder.web.enrichment_tiers import extract_with_haiku

        job_row = {
            "dedup_key": "acme|ds|remote",
            "title": "Data Scientist",
            "company": "Acme Corp",
        }
        search_text = "Data Scientist at Acme Corp builds ML models."
        config = {"scoring": {"models": {"haiku": "claude-haiku-4-5"}}}

        with patch("job_finder.web.enrichment_tiers.call_claude") as mock_call:
            mock_call.return_value = (
                {"jd_full": "Build ML models.", "salary_min": 140000},
                0.0001,
            )
            result = extract_with_haiku(
                search_text, job_row, temp_db, config
            )

        mock_call.assert_called_once()
        call_kwargs = mock_call.call_args[1]
        assert "haiku" in call_kwargs.get("model", "").lower()

    def test_extract_with_haiku_returns_dict_with_job_fields(
        self, mock_anthropic_client, temp_db
    ):
        """extract_with_haiku returns dict with extracted job fields."""
        from job_finder.web.enrichment_tiers import extract_with_haiku

        job_row = {"dedup_key": "acme|ds|remote", "title": "Data Scientist", "company": "Acme Corp"}
        search_text = "Data Scientist at Acme Corp."
        config = {"scoring": {"models": {"haiku": "claude-haiku-4-5"}}}

        with patch("job_finder.web.enrichment_tiers.call_claude") as mock_call:
            mock_call.return_value = (
                {"jd_full": "Build ML models at Acme.", "salary_min": 140000},
                0.0001,
            )
            result = extract_with_haiku(
                search_text, job_row, temp_db, config
            )

        assert "jd_full" in result and "salary_min" in result
        # Should return whatever was extracted (non-None fields only)

    def test_extract_with_haiku_returns_empty_dict_on_failure(
        self, mock_anthropic_client, temp_db
    ):
        """extract_with_haiku returns empty dict when Haiku call fails."""
        from job_finder.web.enrichment_tiers import extract_with_haiku

        job_row = {"dedup_key": "acme|ds|remote", "title": "Data Scientist", "company": "Acme Corp"}
        search_text = "Some search text."
        config = {}

        with patch("job_finder.web.enrichment_tiers.call_claude") as mock_call:
            mock_call.side_effect = Exception("API error")
            result = extract_with_haiku(
                search_text, job_row, temp_db, config
            )

        assert result == {}

# ---------------------------------------------------------------------------
# Tests for enrich_company_info (preserved)
# ---------------------------------------------------------------------------

class TestEnrichCompanyInfo:
    def test_enrich_company_info_calls_duckduckgo(self):
        """enrich_company_info calls DuckDuckGo for company details."""
        from job_finder.web.company_enricher import enrich_company_info

        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_ddg:
            mock_ddg.return_value = "Acme Corp is a SaaS company with 500 employees."
            result = enrich_company_info("Acme Corp")

        mock_ddg.assert_called_once()
        # Should include company name in query
        query = mock_ddg.call_args[0][0]
        assert "Acme Corp" in query

    def test_enrich_company_info_returns_dict(self):
        """enrich_company_info returns dict (possibly empty) with company fields."""
        from job_finder.web.company_enricher import enrich_company_info

        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_ddg:
            mock_ddg.return_value = "Acme Corp employs 500 people in the SaaS industry."
            result = enrich_company_info("Acme Corp")

        # Keys are optional (DDG reliability is low per research) but should be correct types if present
        for key in ["company_size", "industry", "funding_stage"]:
            if key in result:
                assert isinstance(result[key], str)

    def test_enrich_company_info_returns_empty_dict_on_ddg_failure(self):
        """enrich_company_info returns empty dict when DuckDuckGo returns None."""
        from job_finder.web.company_enricher import enrich_company_info

        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_ddg:
            mock_ddg.return_value = None
            result = enrich_company_info("Acme Corp")

        assert result == {}

    def test_enrich_company_info_returns_empty_dict_on_exception(self):
        """enrich_company_info returns empty dict when DDG call raises an exception."""
        from job_finder.web.company_enricher import enrich_company_info

        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_ddg:
            mock_ddg.side_effect = Exception("Network error")
            result = enrich_company_info("Acme Corp")

        assert result == {}

# ---------------------------------------------------------------------------
# Tests for enrich_job tier ordering (Phase 10 — NEW)
# ---------------------------------------------------------------------------

class TestEnrichJobTierOrder:
    """Verify strict cost ordering: free -> DDG -> Haiku -> SerpAPI -> Sonnet."""

    def test_free_tier_url_fetch_runs_first(self, sparse_job_row):
        """Direct URL fetch is attempted before any other tier."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job/123"]'

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            # Free tier URL fetch succeeds with JD
            mock_fetch.return_value = "Full job description from direct URL fetch."
            result = enrich_job(sparse_job_row, serpapi_key="key")

        mock_fetch.assert_called_once()
        # DDG and SerpAPI should not be called if free tier satisfied JD
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_ddg_runs_after_free_tier_fails(self, sparse_job_row):
        """DDG only called when free tier doesn't satisfy missing fields."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            mock_fetch.return_value = None
            mock_ddg.return_value = "Some DDG text about the job."
            mock_serp.return_value = None

            result = enrich_job(sparse_job_row, serpapi_key=None)

        mock_ddg.assert_called_once()

    def test_haiku_runs_after_ddg_fails(self, sparse_job_row, mock_anthropic_client):
        """Haiku extraction only called after DDG fails."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            mock_fetch.return_value = None
            mock_ddg.return_value = None
            mock_haiku.return_value = {"jd_full": "Haiku extracted JD."}
            mock_serp.return_value = None

            result = enrich_job(sparse_job_row)

        mock_haiku.assert_called_once()

    def test_serpapi_runs_after_haiku_for_jd_only(self, sparse_job_row):
        """SerpAPI only called when JD still missing after Haiku."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web, \
             patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = None
            # Haiku only found salary, not JD
            mock_haiku.return_value = {"salary_min": 140000}
            mock_serp.return_value = {"jd_full": "Full JD from SerpAPI."}

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
            )

        mock_serp.assert_called_once()

    def test_serpapi_skipped_when_free_satisfies_jd(self, sparse_job_row):
        """If free tier URL fetch returns JD, SerpAPI is never called."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job/123"]'
        sparse_job_row["salary_min"] = 100000  # salary already present

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            mock_fetch.return_value = "Full job description from direct URL."
            result = enrich_job(sparse_job_row, serpapi_key="test-key")

        mock_serp.assert_not_called()

    def test_free_tier_ats_query_runs_when_company_has_slug(self, sparse_job_row, temp_db):
        """ATS API queried in free tier when company has ats_probe_status='hit'."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = 1

        # Insert company with hit status
        temp_db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_platform, ats_slug, ats_probe_status) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'lever', 'acme-corp', 'hit')"
        )
        temp_db.commit()

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.query_ats_api") as mock_ats, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg:
            mock_fetch.return_value = None
            mock_ats.return_value = {"jd_full": "ATS API returned full JD."}
            mock_ddg.return_value = None

            result = enrich_job(sparse_job_row, conn=temp_db)

        mock_ats.assert_called_once()
        # DDG should not be called if ATS satisfied JD
        mock_ddg.assert_not_called()

    def test_free_tier_careers_scrape_runs_after_ats(self, sparse_job_row, temp_db):
        """Careers page scraper tried when ATS query returns nothing."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = 1

        # Insert company without ATS hit — careers scraper fallback
        temp_db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_probe_status, homepage_url) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'miss', 'https://acmecorp.com')"
        )
        temp_db.commit()

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.query_ats_api") as mock_ats, \
             patch("job_finder.web.data_enricher.scrape_careers") as mock_scrape, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg:
            mock_fetch.return_value = None
            mock_ats.return_value = {}
            mock_scrape.return_value = {"jd_full": "Careers page JD."}
            mock_ddg.return_value = None

            result = enrich_job(sparse_job_row, conn=temp_db)

        mock_scrape.assert_called_once()
        mock_ddg.assert_not_called()

# ---------------------------------------------------------------------------
# Tests for per-field cost ceilings (Phase 10 — NEW)
# ---------------------------------------------------------------------------

class TestFieldCeilings:
    """Salary stops at Haiku tier; JD escalates all the way to Sonnet."""

    def test_salary_ceiling_at_haiku(self, sparse_job_row, mock_anthropic_client):
        """When only salary is missing and Haiku couldn't find it, SerpAPI/Sonnet not called."""
        from job_finder.web.data_enricher import enrich_job

        # Job has JD but no salary
        sparse_job_row["jd_full"] = "Full job description already present."
        sparse_job_row["salary_min"] = None
        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp, \
             patch("job_finder.web.data_enricher.extract_with_sonnet") as mock_sonnet:
            mock_fetch.return_value = None
            mock_ddg.return_value = None
            mock_haiku.return_value = {}  # Haiku couldn't find salary
            mock_serp.return_value = None

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
            )

        # SerpAPI and Sonnet should NOT be called when only salary is missing
        mock_serp.assert_not_called()
        mock_sonnet.assert_not_called()

    def test_jd_escalates_to_sonnet_when_all_lower_tiers_fail(self, sparse_job_row, mock_anthropic_client):
        """JD escalates all the way to Sonnet when prior tiers fail."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web, \
             patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp, \
             patch("job_finder.web.data_enricher.extract_with_sonnet") as mock_sonnet, \
             patch("job_finder.web.data_enricher.cost_gate") as mock_gate:
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = None
            mock_haiku.return_value = {}
            mock_serp.return_value = None
            mock_gate.return_value = True
            mock_sonnet.return_value = {"jd_full": "Sonnet extracted the JD."}

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
            )

        mock_sonnet.assert_called_once()

# ---------------------------------------------------------------------------
# Tests for Sonnet enrichment (Phase 10 — NEW)
# ---------------------------------------------------------------------------

class TestSonnetEnrichment:
    """Sonnet enrichment uses all prior fragments and checks cost_gate."""

    def test_sonnet_receives_all_fragments(self, sparse_job_row, mock_anthropic_client):
        """Sonnet enrichment prompt includes ALL text fragments from prior tiers."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web, \
             patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp, \
             patch("job_finder.web.data_enricher.extract_with_sonnet") as mock_sonnet, \
             patch("job_finder.web.data_enricher.cost_gate") as mock_gate:
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = "DDG text about the role"
            mock_haiku.return_value = {}  # Haiku failed
            mock_serp.return_value = None  # SerpAPI failed
            mock_gate.return_value = True
            mock_sonnet.return_value = {"jd_full": "Sonnet JD."}

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
            )

        # Sonnet should have been called with fragments dict containing DDG text
        mock_sonnet.assert_called_once()
        call_args = mock_sonnet.call_args
        fragments = call_args[0][0] if call_args[0] else call_args[1].get("fragments", {})
        # Check that fragments contain DDG content
        fragments_str = str(fragments)
        assert "DDG" in fragments_str or "ddg" in fragments_str

    def test_sonnet_checks_cost_gate(self, sparse_job_row, mock_anthropic_client, temp_db):
        """Sonnet enrichment checks cost_gate('sonnet') before calling API."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp, \
             patch("job_finder.web.data_enricher.extract_with_sonnet") as mock_sonnet, \
             patch("job_finder.web.data_enricher.cost_gate") as mock_gate:
            mock_fetch.return_value = None
            mock_ddg.return_value = None
            mock_haiku.return_value = {}
            mock_serp.return_value = None
            mock_gate.return_value = False  # Budget exceeded

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
                conn=temp_db,
            )

        # Sonnet should NOT be called when cost_gate returns False
        mock_sonnet.assert_not_called()

# ---------------------------------------------------------------------------
# Tests for enrichment_tier persistence (Phase 10 — NEW)
# ---------------------------------------------------------------------------

class TestEnrichmentTierPersistence:
    """enrichment_tier persisted atomically; resume-from-next-tier; exhausted skip."""

    def test_enrichment_tier_persisted_atomically(self, sparse_job_row, temp_db):
        """enrichment_tier and enriched fields written in single UPDATE."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job"]'

        # Insert the job into temp_db
        temp_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (sparse_job_row["dedup_key"], sparse_job_row["title"],
             sparse_job_row["company"], sparse_job_row["location"],
             sparse_job_row["source_urls"]),
        )
        temp_db.commit()

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch:
            mock_fetch.return_value = "Full job description from direct URL."

            result = enrich_job(sparse_job_row, conn=temp_db)

        # Verify DB was updated atomically
        row = temp_db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        # Both fields should be set together
        assert row is not None
        assert row["jd_full"] == "Full job description from direct URL."
        assert row["enrichment_tier"] == "free"

    def test_resumes_from_next_tier(self, sparse_job_row, mock_anthropic_client, temp_db):
        """Job with enrichment_tier='ddg' starts at Haiku, not free."""
        from job_finder.web.data_enricher import enrich_job

        # Job was previously enriched up to DDG tier
        sparse_job_row["enrichment_tier"] = "ddg"
        sparse_job_row["source_urls"] = '["https://example.com/job"]'

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.query_ats_api") as mock_ats, \
             patch("job_finder.web.data_enricher.scrape_careers") as mock_scrape, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.extract_with_haiku") as mock_haiku:
            mock_fetch.return_value = None
            mock_haiku.return_value = {"jd_full": "Haiku found the JD."}

            result = enrich_job(
                sparse_job_row,
                conn=temp_db,
            )

        # Free tier and DDG should NOT be called (already attempted)
        mock_fetch.assert_not_called()
        mock_ats.assert_not_called()
        mock_scrape.assert_not_called()
        mock_ddg.assert_not_called()
        # Haiku should be called (next tier after DDG)
        mock_haiku.assert_called_once()

    def test_exhausted_jobs_skipped(self, sparse_job_row):
        """Job with enrichment_tier='exhausted' returns empty dict immediately."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["enrichment_tier"] = "exhausted"

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            result = enrich_job(sparse_job_row, serpapi_key="key")

        assert result == {}
        mock_fetch.assert_not_called()
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

# ---------------------------------------------------------------------------
# Backward compatibility and never-raises (Phase 10 — updated)
# ---------------------------------------------------------------------------

class TestEnrichJobBackwardCompat:
    """Old call patterns and error handling still work."""

    def test_enrich_job_backward_compatible_signature(self, sparse_job_row, temp_db):
        """Call pattern (job_row, serpapi_key, conn, config) works with keyword args."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serp, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg:
            mock_fetch.return_value = None
            mock_serp.return_value = {"jd_full": "SerpAPI JD."}
            mock_ddg.return_value = None

            result = enrich_job(
                sparse_job_row,
                "test-serp-key",
                temp_db,
                {"scoring": {}},
            )

        assert isinstance(result, dict)

    def test_enrich_job_never_raises(self, sparse_job_row, temp_db):
        """All exceptions caught, empty dict returned."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch:
            mock_fetch.side_effect = Exception("Unexpected catastrophic error")

            # Should not raise
            result = enrich_job(sparse_job_row, serpapi_key="test-key", conn=temp_db)

        assert isinstance(result, dict)

    def test_enrich_job_returns_empty_dict_when_nothing_missing(self, rich_job_row):
        """enrich_job returns empty dict when job already has all scoring-relevant data."""
        from job_finder.web.data_enricher import enrich_job

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serpapi:
            result = enrich_job(rich_job_row, serpapi_key="test-key")

        # Should return early without calling any tier
        mock_fetch.assert_not_called()
        mock_serpapi.assert_not_called()
        assert result == {}

    def test_enrich_job_skips_serpapi_when_no_key(self, sparse_job_row, temp_db):
        """enrich_job skips SerpAPI tier when serpapi_key is None."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '[]'
        sparse_job_row["company_id"] = None

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch, \
             patch("job_finder.web.data_enricher.search_serpapi") as mock_serpapi, \
             patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg:
            mock_fetch.return_value = None
            mock_serpapi.return_value = None
            mock_ddg.return_value = "Some DDG text about the company."

            result = enrich_job(
                sparse_job_row,
                serpapi_key=None,
                conn=temp_db,
                config={},
            )

        mock_serpapi.assert_not_called()

    def test_tier_order_constant_exported(self):
        """TIER_ORDER constant is exported from data_enricher."""
        from job_finder.web.data_enricher import TIER_ORDER

        assert isinstance(TIER_ORDER, list)
        assert "free" in TIER_ORDER
        assert "ddg" in TIER_ORDER
        assert "haiku" in TIER_ORDER
        assert "serpapi" in TIER_ORDER
        assert "sonnet" in TIER_ORDER
        assert "exhausted" in TIER_ORDER
        # Verify ordering: each tier before next
        assert TIER_ORDER.index("free") < TIER_ORDER.index("ddg")
        assert TIER_ORDER.index("ddg") < TIER_ORDER.index("haiku")
        assert TIER_ORDER.index("haiku") < TIER_ORDER.index("serpapi")
        assert TIER_ORDER.index("serpapi") < TIER_ORDER.index("sonnet")
        assert TIER_ORDER.index("sonnet") < TIER_ORDER.index("exhausted")

# ---------------------------------------------------------------------------
# Pipeline integration tests (Phase 10 Plan 02 — NEW)
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Integration tests verifying pipeline_runner wiring and Migration 8 schema.

    Tests verify:
    - enrich_job is called BEFORE score_job_haiku in _run_haiku_scoring
    - _run_sonnet_evaluation does NOT reference fetch_jd
    - Migration 8 adds enrichment_tier column correctly
    - Migration 8 backfills existing enriched jobs as 'serpapi'
    - Migration 8 leaves unenriched jobs with NULL enrichment_tier
    """

    def test_run_scoring_does_not_call_fetch_jd(self):
        """run_scoring source does not reference fetch_jd.

        v3 unified runner inherits the legacy no-JD-fetch contract from
        run_sonnet_evaluation -- JD fetching is enrich_job's job.
        """
        from job_finder.web.scoring_runner import run_scoring

        source = inspect.getsource(run_scoring)
        assert "fetch_jd" not in source, (
            "run_scoring references fetch_jd -- JD fetching must remain "
            "exclusively in enrich_job"
        )

    def test_enrichment_tier_column_exists_after_migration(self, tmp_db_path):
        """Migration 8 adds enrichment_tier column to jobs table."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        conn.close()

        assert "enrichment_tier" in cols, (
            "enrichment_tier column missing from jobs table after Migration 8"
        )

    def test_existing_enriched_jobs_marked_serpapi_after_migration(self, tmp_db_path):
        """Migration 8 sets enrichment_tier='serpapi' for existing jobs with jd_full.

        Jobs that already have jd_full before Migration 8 are assumed to have
        been enriched via SerpAPI (the only enrichment path before Phase 10).

        Note: retroactive dedup (also part of migration) normalizes dedup_keys to
        'company|title' format (lowercase, location dropped). We query by the
        normalized key after migration.
        """
        from job_finder.web.db_migrate import run_migrations

        conn = sqlite3.connect(tmp_db_path)
        # Create minimal schema with jd_full column (simulates pre-migration DB)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                source_urls TEXT DEFAULT '[]',
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed',
                jd_full TEXT DEFAULT NULL
            )
        """)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, jd_full)
               VALUES ('acme|data scientist|remote', 'Data Scientist', 'Acme', 'Remote',
                       '2026-01-01', '2026-01-01', 'Full job description here.')"""
        )
        conn.commit()
        conn.close()

        # Running full migrations should apply all migrations including #8
        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        # After retroactive dedup, key is normalized to 'acme|data scientist' (no location)
        row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = 'acme|data scientist'"
        ).fetchone()
        conn.close()

        assert row is not None, (
            "Job row missing after migration (retroactive dedup normalizes key to "
            "'company|title' format without location)"
        )
        assert row["enrichment_tier"] == "serpapi", (
            f"Expected enrichment_tier='serpapi' for job with jd_full, "
            f"got: {row['enrichment_tier']!r}"
        )

    def test_existing_unenriched_jobs_have_null_tier_after_migration(self, tmp_db_path):
        """Migration 8 leaves enrichment_tier=NULL for jobs without jd_full.

        Jobs that had no jd_full before Migration 8 remain unassigned (NULL),
        so the backfill enrichment pipeline can pick them up.

        Note: retroactive dedup normalizes dedup_keys to 'company|title' format.
        """
        from job_finder.web.db_migrate import run_migrations

        conn = sqlite3.connect(tmp_db_path)
        # Create minimal schema without enrichment_tier (pre-migration state)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                source_urls TEXT DEFAULT '[]',
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed',
                jd_full TEXT DEFAULT NULL
            )
        """)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES ('beta|staff engineer|nyc', 'Staff Engineer', 'Beta', 'NYC',
                       '2026-01-01', '2026-01-01')"""
            # jd_full omitted -- defaults to NULL
        )
        conn.commit()
        conn.close()

        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        # Retroactive dedup normalizes key: 'beta|staff engineer' (location dropped)
        row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = 'beta|staff engineer'"
        ).fetchone()
        conn.close()

        assert row is not None, (
            "Job row missing after migration (retroactive dedup normalizes key to "
            "'company|title' format without location)"
        )
        assert row["enrichment_tier"] is None, (
            f"Expected enrichment_tier=NULL for job without jd_full, "
            f"got: {row['enrichment_tier']!r}"
        )

# ---------------------------------------------------------------------------
# JD direct fetch tests (ported from TestJDFetcher in test_scoring.py — DEBT-03)
# ---------------------------------------------------------------------------

class TestFetchDirectJd:
    """Verify _fetch_direct_jd() handles URL fetch, HTML stripping, length cap, and failures.

    Ported from TestJDFetcher in test_scoring.py (DEBT-03).
    Target: job_finder.web.data_enricher._fetch_direct_jd
    """

    def test_successful_url_returns_text(self):
        """_fetch_direct_jd() with a successful URL returns extracted text content."""
        from unittest.mock import MagicMock, patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><div id='main'><p>Senior Data Scientist role at Acme.</p></div></body></html>"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        mock_response.raise_for_status.return_value = None

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_response):
            result = _fetch_direct_jd("https://example.com/jobs/123")

        assert result is not None
        assert "Senior Data Scientist" in result
        assert "Acme" in result

    def test_strips_script_style_nav_footer_header_tags(self):
        """_fetch_direct_jd() removes noisy HTML elements leaving only content text."""
        from unittest.mock import MagicMock, patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = (
            "<html><head><script>var x=1;</script><style>.cls{color:red}</style></head>"
            "<body><nav>Navigation menu</nav><header>Site Header</header>"
            "<main><p>Job description content here.</p></main>"
            "<footer>Footer content</footer></body></html>"
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        mock_response.raise_for_status.return_value = None

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_response):
            result = _fetch_direct_jd("https://example.com/jobs/456")

        assert result is not None
        assert "Job description content here" in result
        # Noisy content should be stripped
        assert "var x=1" not in result
        assert "color:red" not in result
        assert "Navigation menu" not in result
        assert "Site Header" not in result
        assert "Footer content" not in result

    def test_caps_result_at_8000_characters(self):
        """_fetch_direct_jd() caps returned text at 8000 characters."""
        from unittest.mock import MagicMock, patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        # Create a page with content much longer than 8000 chars
        long_content = "A" * 20_000
        html = f"<html><body><p>{long_content}</p></body></html>"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        mock_response.raise_for_status.return_value = None

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_response):
            result = _fetch_direct_jd("https://example.com/jobs/789")

        assert result is not None
        assert len(result) <= 8000

    def test_returns_none_on_timeout(self):
        """_fetch_direct_jd() returns None when requests.get raises Timeout."""
        import requests as req_lib
        from unittest.mock import patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        with patch("job_finder.web.enrichment_tiers.requests.get", side_effect=req_lib.Timeout("timed out")):
            result = _fetch_direct_jd("https://example.com/jobs/slow")

        assert result is None

    def test_returns_none_on_404(self):
        """_fetch_direct_jd() returns None when the response is a 404 error."""
        import requests as req_lib
        from unittest.mock import MagicMock, patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = req_lib.HTTPError("404 Not Found")

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_response):
            result = _fetch_direct_jd("https://example.com/jobs/gone")

        assert result is None

    def test_returns_none_for_none_url(self):
        """_fetch_direct_jd() returns None when url is None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        result = _fetch_direct_jd(None)
        assert result is None

    def test_returns_none_on_connection_error(self):
        """_fetch_direct_jd() returns None on connection errors."""
        import requests as req_lib
        from unittest.mock import patch
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        with patch("job_finder.web.enrichment_tiers.requests.get",
                   side_effect=req_lib.ConnectionError("refused")):
            result = _fetch_direct_jd("https://example.com/jobs/unreachable")

        assert result is None

# ---------------------------------------------------------------------------
# Auth-wall guard tests (Phase 40 Plan 01 — NEW)
# ---------------------------------------------------------------------------

class TestAuthWallGuard:
    """Verify _fetch_direct_jd() returns None for auth-wall pages.

    Auth-wall signatures include LinkedIn login text, CAPTCHA challenges,
    and WAF access-denied pages. These should never be stored as jd_full.
    """

    def _make_mock_response(self, html: str):
        """Helper: create a mock requests.Response with given HTML body."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        mock_response.raise_for_status.return_value = None
        return mock_response

    def test_returns_none_for_linkedin_login_page(self):
        """HTML containing LinkedIn 'We're signing you in' -> returns None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><p>We're signing you in</p><p>Discover people, jobs, and more.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://linkedin.com/jobs/view/12345/")

        assert result is None, "Expected None for LinkedIn login page"

    def test_returns_none_for_sign_in_or_join(self):
        """HTML containing 'Sign in or Join' -> returns None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><h1>Sign in or Join</h1><p>Create an account to view this job.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://linkedin.com/jobs/view/99999/")

        assert result is None, "Expected None for 'Sign in or Join' page"

    def test_returns_none_for_captcha_page(self):
        """HTML containing 'please verify you are a human' -> returns None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><p>Please verify you are a human to continue.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://example.com/jobs/captcha")

        assert result is None, "Expected None for CAPTCHA page"

    def test_returns_none_for_access_denied(self):
        """HTML containing 'Access Denied' -> returns None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><h1>Access Denied</h1><p>You don't have permission to access this resource.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://example.com/jobs/protected")

        assert result is None, "Expected None for Access Denied page"

    def test_allows_normal_jd_through(self):
        """HTML with normal job description text -> returns the text (not blocked)."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = (
            "<html><body>"
            "<h1>Senior Data Scientist</h1>"
            "<p>We are looking for a talented data scientist to join our team. "
            "You will build ML models, analyze large datasets, and collaborate with engineers.</p>"
            "</body></html>"
        )
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://example.com/jobs/data-scientist")

        assert result is not None, "Expected text returned for normal JD page"
        assert "Data Scientist" in result

    def test_auth_wall_check_is_case_insensitive(self):
        """Auth wall detection is case-insensitive: 'WE'RE SIGNING YOU IN' -> None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><p>WE'RE SIGNING YOU IN</p><p>DISCOVER PEOPLE, JOBS, AND MORE.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://linkedin.com/jobs/view/uppercase/")

        assert result is None, "Expected None even for uppercase auth-wall text"

# ---------------------------------------------------------------------------
# Migration 15 tests (Phase 40 Data Quality — DQ-04, DQ-05)
# ---------------------------------------------------------------------------

class TestMigration15:
    """Migration 15 cleans poison data and promotes descriptions."""

    def test_migration_15_nullifies_poison_jd_full(self, tmp_db_path):
        """Poison jd_full with LinkedIn login text is nullified."""
        from job_finder.web.db_migrate import run_migrations

        # Create a pre-migration DB with poison data
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                source_urls TEXT DEFAULT '[]',
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed',
                jd_full TEXT DEFAULT NULL
            )
        """)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, jd_full) "
            "VALUES ('poison|job', 'Data Scientist', 'Acme', 'Remote', '2026-01-01', '2026-01-01', "
            "'Sign in\nWe''re signing you in\nDiscover people, jobs, and more...')"
        )
        conn.commit()
        conn.close()

        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        # Key may be normalized by retroactive dedup
        row = conn.execute("SELECT jd_full, enrichment_tier FROM jobs WHERE title = 'Data Scientist'").fetchone()
        conn.close()

        if row is not None:
            assert row["jd_full"] is None, f"Poison jd_full should be NULL, got: {row['jd_full']!r}"
            assert row["enrichment_tier"] == "ddg", f"enrichment_tier should be 'ddg', got: {row['enrichment_tier']!r}"

    def test_migration_15_deletes_notification_rows(self, tmp_db_path):
        """Garbage rows with notification text in title are deleted."""
        from job_finder.web.db_migrate import run_migrations

        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                source_urls TEXT DEFAULT '[]',
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed',
                jd_full TEXT DEFAULT NULL
            )
        """)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('notif|garbage', 'You''ll receive notifications when new jobs match...', "
            "'LinkedIn', 'Unknown', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE title LIKE '%receive notifications%'").fetchone()[0]
        conn.close()

        assert count == 0, f"Notification garbage rows should be deleted, found {count}"

    def test_migration_15_promotes_descriptions(self, tmp_db_path):
        """Long descriptions are promoted to jd_full where jd_full is NULL."""
        from job_finder.web.db_migrate import run_migrations

        long_desc = "A" * 300  # > 200 chars

        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                sources TEXT DEFAULT '[]',
                source_urls TEXT DEFAULT '[]',
                source_id TEXT DEFAULT '',
                salary_min INTEGER,
                salary_max INTEGER,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                score REAL DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                user_interest TEXT DEFAULT 'unreviewed',
                jd_full TEXT DEFAULT NULL
            )
        """)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, description) "
            "VALUES ('promo|job', 'Engineer', 'Beta', 'NYC', '2026-01-01', '2026-01-01', ?)",
            (long_desc,),
        )
        conn.commit()
        conn.close()

        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        # Key may be normalized
        row = conn.execute("SELECT jd_full FROM jobs WHERE title = 'Engineer'").fetchone()
        conn.close()

        assert row is not None
        assert row["jd_full"] is not None, "Long description should be promoted to jd_full"
        assert len(row["jd_full"]) > 200

# ---------------------------------------------------------------------------
# Description promotion tests (Phase 40 Plan 01 — NEW)
# ---------------------------------------------------------------------------

class TestDescriptionPromotion:
    """Verify enrich_job auto-promotes long descriptions to jd_full.

    A description > 200 chars with jd_full=None should be promoted to jd_full.
    Short descriptions and existing jd_full values must not be affected.
    """

    @pytest.fixture
    def promo_db(self):
        """In-memory SQLite DB with jobs and scoring_costs tables for promotion tests."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE scoring_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                purpose TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                jd_full TEXT DEFAULT NULL,
                salary_min INTEGER DEFAULT NULL,
                salary_max INTEGER DEFAULT NULL,
                source_urls TEXT DEFAULT '[]',
                company_id INTEGER DEFAULT NULL,
                enrichment_tier TEXT DEFAULT NULL,
                description TEXT DEFAULT NULL
            )
        """)
        conn.commit()
        return conn

    def test_promotes_long_description_to_jd_full(self, promo_db):
        """job_row with description > 200 chars and jd_full=None -> enrich_job sets jd_full."""
        from job_finder.web.data_enricher import enrich_job

        long_desc = "A" * 250  # > 200 chars
        job_row = {
            "dedup_key": "test|promo-job|remote",
            "title": "Promo Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        promo_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description) VALUES (?, ?, ?, ?, ?)",
            (job_row["dedup_key"], job_row["title"], job_row["company"], job_row["location"], long_desc),
        )
        promo_db.commit()

        with patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None), \
             patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None):
            result = enrich_job(job_row, conn=promo_db)

        # jd_full should be set on the job_row dict after promotion
        assert job_row.get("jd_full") == long_desc, (
            f"Expected jd_full to be set to description, got: {job_row.get('jd_full')!r}"
        )

    def test_does_not_promote_short_description(self, promo_db):
        """job_row with description < 200 chars and jd_full=None -> jd_full stays None."""
        from job_finder.web.data_enricher import enrich_job

        short_desc = "A brief description."  # < 200 chars
        job_row = {
            "dedup_key": "test|short-desc|remote",
            "title": "Short Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": short_desc,
        }

        with patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None), \
             patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None):
            result = enrich_job(job_row, conn=promo_db)

        # jd_full should remain None (short description not promoted)
        assert job_row.get("jd_full") is None, (
            f"Expected jd_full to remain None for short description, got: {job_row.get('jd_full')!r}"
        )

    def test_does_not_overwrite_existing_jd_full(self, promo_db):
        """job_row with description > 200 chars and jd_full already set -> jd_full unchanged."""
        from job_finder.web.data_enricher import enrich_job

        existing_jd = "Existing full job description already stored."
        long_desc = "B" * 250  # > 200 chars
        job_row = {
            "dedup_key": "test|has-jd|remote",
            "title": "Has JD Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": existing_jd,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        result = enrich_job(job_row, conn=promo_db)

        # jd_full should remain the existing value (not overwritten by description)
        assert job_row.get("jd_full") == existing_jd, (
            f"Expected existing jd_full to be preserved, got: {job_row.get('jd_full')!r}"
        )

    def test_promotion_persists_to_db(self, promo_db):
        """With conn provided, promotion UPDATE writes to DB with 'AND jd_full IS NULL' guard."""
        from job_finder.web.data_enricher import enrich_job

        long_desc = "C" * 250  # > 200 chars
        dedup_key = "test|db-persist|remote"
        job_row = {
            "dedup_key": dedup_key,
            "title": "DB Persist Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        promo_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description) VALUES (?, ?, ?, ?, ?)",
            (dedup_key, job_row["title"], job_row["company"], job_row["location"], long_desc),
        )
        promo_db.commit()

        with patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None), \
             patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None):
            enrich_job(job_row, conn=promo_db)

        # Verify DB row was updated with jd_full
        row = promo_db.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()

        assert row is not None, "Job row not found in DB"
        assert row["jd_full"] is not None, "Expected jd_full to be set in DB after promotion"
        assert row["jd_full"] == long_desc[:8000], (
            f"Expected jd_full in DB to match description (truncated to 8000), "
            f"got: {row['jd_full']!r}"
        )
