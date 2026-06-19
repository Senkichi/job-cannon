"""Tests for data_enricher.py — cost-ordered enrichment pipeline.

Tests cover both the existing private helpers (unchanged API) and the
synthesis-free enrich_job pipeline introduced in Phase 2b sub-fix RC4.

Existing test classes preserved:
- TestSearchSerpapi
- TestSearchDuckDuckGo
- TestEnrichCompanyInfo

Phase 2b cascade tests:
- TestEnrichJobTierOrder — free -> DDG -> SerpAPI -> agentic ordering
- TestFieldCeilings — salary capped at ddg; JD escalates to agentic
- TestEnrichmentTierPersistence — atomic DB write, resume-from-next-tier, exhausted skip
- TestEnrichJobBackwardCompat — old call pattern still works
- TestPipelineIntegration — enrich_job wiring + migration columns
"""

import inspect
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_enrichment_network():
    """Neutralize all outbound-I/O enrichment tiers at the data_enricher seam.

    enrich_job() runs a cost-ordered network cascade (direct fetch, ATS API,
    careers scrape, DDG, SerpAPI). Tests that call enrich_job only to verify
    DB-side behavior (description->jd_full promotion, never-raises, signature
    compat) don't need real network and were paying 5-27s each plus inheriting
    network flakiness. This stubs every tier to its real "no result" shape so
    enrich_job proceeds to its DB logic immediately. Individual tests may still
    override a specific tier inside their own ``with patch(...)``.

    Mock at the data_enricher import site (not enrichment_tiers) because
    data_enricher does ``from enrichment_tiers import <fn>`` — the names are
    bound in the data_enricher namespace. Classes that test the tier functions
    DIRECTLY (TestSearchSerpapi/TestSearchDuckDuckGo) call them via
    enrichment_tiers.* and are unaffected.

    Return shapes are the verified per-function "miss" values (see
    enrichment_tiers.py signatures + data_enricher.py call sites):
      - fetch_direct_jd  -> str | None              (caller truthiness-checks)
      - query_ats_api    -> dict                     (caller truthiness-checks)
      - scrape_careers   -> dict                     (caller truthiness-checks)
      - search_ddg_web   -> dict                     (caller does .get(...))
      - fetch_ddg_jds    -> tuple[str|None, str|None] (caller unpacks 2)
      - search_duckduckgo-> str | None              (caller filters falsy)
      - search_serpapi   -> tuple[dict|None, list]   (caller unpacks 2)

    The deepest tier (agentic) is neutralized file-wide by the
    _neutralize_agentic_tier autouse below — when every cheaper tier misses (as
    they do here), enrich_job would otherwise escalate into the live agentic
    seam (Playwright + Ollama + real DDG), which made these tests SLOWER, not
    faster.
    """
    targets = {
        "fetch_direct_jd": None,
        "query_ats_api": {},
        "scrape_careers": {},
        "search_ddg_web": {},
        "fetch_ddg_jds": (None, None),
        "search_duckduckgo": None,
        "search_serpapi": (None, []),
    }
    patchers = [
        patch(f"job_finder.web.data_enricher.{name}", return_value=ret)
        for name, ret in targets.items()
    ]
    mocks = [p.start() for p in patchers]
    try:
        yield dict(zip(targets.keys(), mocks, strict=True))
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture(autouse=True)
def _neutralize_agentic_tier():
    """Block enrich_job's deepest tier (agentic) for every test in this file.

    enrich_job's cost cascade ends in agentic_enricher.enrich_one_job (TIER_ORDER
    index 'agentic'), which launches Playwright + Ollama + real DuckDuckGo. Any
    test where the cheaper tiers miss and jd_full stays missing escalates into it
    — the TierOrder/TierPersistence escalation tests paid 40-65s each this way.

    enrich_job imports it lazily (``from job_finder.web.agentic_enricher import
    enrich_one_job`` inside the function), so we patch at the source module.
    Returns {} — its real no-result shape — so escalation still occurs (call-order
    assertions hold) but no live I/O fires. No test in this file patches or asserts
    on the real enrich_one_job, so an agentic-only autouse cannot mask a cheap-tier
    call-order assertion (which is why the cheap-tier stub is opt-in, not autouse).
    """
    with patch("job_finder.web.agentic_enricher.enrich_one_job", return_value={}) as m:
        yield m


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
        "source_urls": "[]",
        "company_id": None,
        "enrichment_tier": None,
        "description": "Lead data science.",
    }


@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client that returns structured extraction result."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps(
        {
            "jd_full": "Data Scientist role at Acme Corp building ML models.",
            "salary_min": 140000,
            "salary_max": 180000,
            "location": "Remote",
        }
    )
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
            -- P1.5 (m104): trust-ranked reconciliation metadata the _persist
            -- salary path now reads (salary_provenance) and appends to
            -- (salary_observations).
            salary_provenance TEXT DEFAULT NULL,
            salary_observations TEXT NOT NULL DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            company_id INTEGER DEFAULT NULL,
            enrichment_tier TEXT DEFAULT NULL,
            -- Canonical location columns the apply_location_observation funnel
            -- writes through (#386). Present so an enrichment-extracted location
            -- routes through the funnel rather than a direct column write.
            locations_raw TEXT DEFAULT NULL,
            locations_structured TEXT DEFAULT NULL,
            workplace_type TEXT DEFAULT 'UNSPECIFIED',
            primary_country_code TEXT DEFAULT NULL
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


# Tests for extract_with_low/extract_with_mid were removed in Phase 2b
# sub-fix RC4 — both functions were deleted from enrichment_tiers.py because
# the synthesis tiers fabricated short pseudo-JDs and blocked escalation to
# real fetch tiers.


# ---------------------------------------------------------------------------
# Tests for enrich_job tier ordering (Phase 10 — NEW)
# ---------------------------------------------------------------------------


class TestEnrichJobTierOrder:
    """Verify strict cost ordering: free -> DDG -> Haiku -> SerpAPI -> Sonnet."""

    def test_free_tier_url_fetch_runs_first(self, sparse_job_row):
        """Direct URL fetch is attempted before any other tier."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job/123"]'

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            # Free tier URL fetch succeeds with a real-length JD (>= 200 chars)
            mock_fetch.return_value = "Full job description from direct URL fetch. " * 5
            result = enrich_job(sparse_job_row, serpapi_key="key")

        mock_fetch.assert_called_once()
        # DDG and SerpAPI should not be called if free tier satisfied JD
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_ddg_runs_after_free_tier_fails(self, sparse_job_row):
        """DDG only called when free tier doesn't satisfy missing fields."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            # search_ddg_web/fetch_ddg_jds were unpatched here -> real DuckDuckGo
            # web search (~4s + flaky). Stub them to their no-result shapes; the
            # assertion below is on search_duckduckgo, which is unaffected.
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = "Some DDG text about the job."
            mock_serp.return_value = None

            result = enrich_job(sparse_job_row, serpapi_key=None)

        mock_ddg.assert_called_once()

    def test_serpapi_runs_after_ddg_for_jd(self, sparse_job_row):
        """SerpAPI only called when JD still missing after DDG."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = None
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

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = "Full job description from direct URL."
            result = enrich_job(sparse_job_row, serpapi_key="test-key")

        mock_serp.assert_not_called()

    def test_free_tier_ats_query_runs_when_company_has_slug(self, sparse_job_row, temp_db):
        """ATS API queried in free tier when company has ats_probe_status='hit'."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = 1

        # Insert company with hit status
        temp_db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_platform, ats_slug, ats_probe_status) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'lever', 'acme-corp', 'hit')"
        )
        temp_db.commit()

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.query_ats_api") as mock_ats,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
        ):
            mock_fetch.return_value = None
            # ATS result must be >= 200 chars to pass the stub-JD gate
            mock_ats.return_value = {"jd_full": "ATS API returned full JD. " * 8}
            mock_ddg.return_value = None

            result = enrich_job(sparse_job_row, conn=temp_db)

        mock_ats.assert_called_once()
        # DDG should not be called if ATS satisfied JD
        mock_ddg.assert_not_called()

    def test_free_tier_careers_scrape_runs_after_ats(self, sparse_job_row, temp_db):
        """Careers page scraper tried when ATS query returns nothing."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = 1

        # Insert company without ATS hit — careers scraper fallback
        temp_db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_probe_status, homepage_url) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'miss', 'https://acmecorp.com')"
        )
        temp_db.commit()

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.query_ats_api") as mock_ats,
            patch("job_finder.web.data_enricher.scrape_careers") as mock_scrape,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
        ):
            mock_fetch.return_value = None
            mock_ats.return_value = {}
            # Careers JD must be >= 200 chars to pass the stub-JD gate
            mock_scrape.return_value = {"jd_full": "Careers page JD. " * 13}
            mock_ddg.return_value = None

            result = enrich_job(sparse_job_row, conn=temp_db)

        mock_scrape.assert_called_once()
        mock_ddg.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for DDG tier persistence (issue #224 — silent JD drop)
# ---------------------------------------------------------------------------


class TestDDGTierPersist:
    """DDG-fetched JDs must be persisted under enrichment_tier='ddg' rather than
    being captured into ``fragments`` then discarded when control falls through to
    the terminal exhausted-persist.
    """

    def test_ddg_jd_persisted_under_ddg_tier(self, sparse_job_row, temp_db):
        """A real DDG-fetched JD (>= 200 chars) is written with enrichment_tier='ddg'.

        Free tier yields nothing, fetch_ddg_jds returns a usable JD, no SerpAPI
        key — the row must end up with jd_full set and enrichment_tier='ddg'
        (not 'exhausted').
        """
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        temp_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        temp_db.commit()

        ddg_jd = "DuckDuckGo-fetched job description with lots of detail. " * 5
        assert len(ddg_jd) >= 200  # sanity: must pass the stub gate

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {
                "ddg_urls": ["https://example.com/posting"],
                "ddg_snippet": "snippet",
            }
            mock_ddg_jds.return_value = (ddg_jd, "https://example.com/posting")
            mock_ddg.return_value = None
            mock_serp.return_value = (None, [])

            result = enrich_job(sparse_job_row, serpapi_key=None, conn=temp_db)

        row = temp_db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["jd_full"] == ddg_jd, "DDG JD must be persisted, not dropped"
        assert row["enrichment_tier"] == "ddg", "Tier must reflect DDG (not 'exhausted')"
        assert result.get("jd_full") == ddg_jd
        # SerpAPI never invoked when DDG already satisfied JD
        mock_serp.assert_not_called()

    def test_ddg_stub_jd_rejected_and_escalates(self, sparse_job_row, temp_db):
        """A DDG-returned stub (< 200 chars) is rejected by _is_stub_jd; the row
        is not persisted under 'ddg' and escalation to SerpAPI/agentic proceeds.
        """
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        temp_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        temp_db.commit()

        stub_jd = "Apply now"  # < 200 chars — must be rejected

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {
                "ddg_urls": ["https://example.com/posting"],
                "ddg_snippet": "snippet",
            }
            mock_ddg_jds.return_value = (stub_jd, "https://example.com/posting")
            mock_ddg.return_value = None
            mock_serp.return_value = (None, [])

            enrich_job(sparse_job_row, serpapi_key=None, conn=temp_db)

        row = temp_db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["jd_full"] is None, "Stub DDG JD must NOT be persisted"
        # No serpapi key + no agentic JD => cascade terminates at 'exhausted'
        assert row["enrichment_tier"] == "exhausted"

    def test_ddg_jd_triggers_post_fetch_salary_extraction(self, sparse_job_row, temp_db):
        """The DDG-fetched JD must flow through _apply_post_fetch_extraction so
        salary regex sees the description (proves effective_jd is populated).
        """
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        temp_db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        temp_db.commit()

        ddg_jd = (
            "Senior Data Scientist role at Acme Corp. "
            "Salary range: $150,000 - $200,000 USD per year. "
            "Build ML models and collaborate cross-functionally. "
            "We work on large-scale ML systems and value engineering rigor. "
            "Required: 5+ years Python; strong SQL; experience with cloud platforms."
        )
        assert len(ddg_jd) >= 200

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {
                "ddg_urls": ["https://example.com/posting"],
                "ddg_snippet": "snippet",
            }
            mock_ddg_jds.return_value = (ddg_jd, "https://example.com/posting")
            mock_ddg.return_value = None
            mock_serp.return_value = (None, [])

            enrich_job(sparse_job_row, serpapi_key=None, conn=temp_db)

        row = temp_db.execute(
            "SELECT jd_full, enrichment_tier, salary_min, salary_max "
            "FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["enrichment_tier"] == "ddg"
        assert row["jd_full"] == ddg_jd
        # Regex-based salary extractor should have picked up "$150,000 - $200,000"
        assert row["salary_min"] == 150_000
        assert row["salary_max"] == 200_000


# ---------------------------------------------------------------------------
# Tests for per-field cost ceilings (Phase 10 — NEW)
# ---------------------------------------------------------------------------


class TestFieldCeilings:
    """Salary stops at ddg tier; JD escalates all the way to agentic."""

    def test_salary_ceiling_at_ddg(self, sparse_job_row):
        """When only salary is missing and ddg fetch fails, SerpAPI is not called."""
        from job_finder.web.data_enricher import enrich_job

        # Job has JD but no salary
        sparse_job_row["jd_full"] = "Full job description already present."
        sparse_job_row["salary_min"] = None
        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg_web,
            patch("job_finder.web.data_enricher.fetch_ddg_jds") as mock_ddg_jds,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_ddg_web.return_value = {"ddg_urls": [], "ddg_snippet": ""}
            mock_ddg_jds.return_value = (None, None)
            mock_ddg.return_value = None
            mock_serp.return_value = None

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
            )

        # SerpAPI should NOT be called when only salary is missing — JD already present,
        # so jd_still_missing guard prevents SerpAPI/agentic escalation.
        mock_serp.assert_not_called()


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
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        temp_db.commit()

        mock_jd = "Full job description from direct URL. " * 6  # 228 chars — passes stub gate
        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch:
            mock_fetch.return_value = mock_jd

            result = enrich_job(sparse_job_row, conn=temp_db)

        # Verify DB was updated atomically
        row = temp_db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        # Both fields should be set together
        assert row is not None
        assert row["jd_full"] == mock_jd
        assert row["enrichment_tier"] == "free"

    def test_resumes_from_next_tier(self, sparse_job_row, temp_db):
        """Job with enrichment_tier='ddg' starts at SerpAPI, not free."""
        from job_finder.web.data_enricher import enrich_job

        # Job was previously enriched up to DDG tier
        sparse_job_row["enrichment_tier"] = "ddg"
        sparse_job_row["source_urls"] = '["https://example.com/job"]'

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.query_ats_api") as mock_ats,
            patch("job_finder.web.data_enricher.scrape_careers") as mock_scrape,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            mock_fetch.return_value = None
            mock_serp.return_value = {"jd_full": "SerpAPI found the JD."}

            result = enrich_job(
                sparse_job_row,
                serpapi_key="test-key",
                conn=temp_db,
            )

        # Free tier and DDG should NOT be called (already attempted)
        mock_fetch.assert_not_called()
        mock_ats.assert_not_called()
        mock_scrape.assert_not_called()
        mock_ddg.assert_not_called()
        # SerpAPI should be called (next tier after DDG in the new cascade)
        mock_serp.assert_called_once()

    def test_exhausted_jobs_skipped(self, sparse_job_row):
        """Job with enrichment_tier='exhausted' returns empty dict immediately."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["enrichment_tier"] = "exhausted"

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            result = enrich_job(sparse_job_row, serpapi_key="key")

        assert result == {}
        mock_fetch.assert_not_called()
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()


# ---------------------------------------------------------------------------
# _persist invariant-guard tests (issue #106)
# ---------------------------------------------------------------------------


class TestPersistInvariantGuards:
    """_persist routes jd_full through set_jd_full() and salary through
    _normalize_salary() so m078 invariant violations (I-02/I-13) cannot
    silently discard the enrichment_tier bookmark or sibling fields.
    """

    def _insert_job(self, conn, dedup_key: str) -> None:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location) VALUES (?, ?, ?, ?)",
            (dedup_key, "Test Job", "Test Co", "Remote"),
        )
        conn.commit()

    def _fetch(self, conn, dedup_key: str) -> dict:
        row = conn.execute(
            "SELECT jd_full, salary_min, salary_max, location, enrichment_tier "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row is not None, f"Job {dedup_key!r} not found"
        return dict(row)

    # ------------------------------------------------------------------ #
    # jd_full — I-13 gate
    # ------------------------------------------------------------------ #

    def test_junk_jd_not_written_but_tier_recorded(self, temp_db):
        """A junk jd_full (< 200 chars) is gated by set_jd_full(); tier still written."""
        from job_finder.web.data_enricher import _persist

        key = "co|junk-jd|test"
        self._insert_job(temp_db, key)

        junk_jd = "loading"  # classic auth-wall junk
        _persist(temp_db, {"dedup_key": key}, {"jd_full": junk_jd}, "free")

        row = self._fetch(temp_db, key)
        assert row["jd_full"] is None, "Junk jd_full must NOT be written"
        assert row["enrichment_tier"] == "free", "Tier must be written even when jd_full is junk"

    def test_valid_jd_written_and_tier_recorded(self, temp_db):
        """A valid jd_full (>= 200 chars) is written and tier is recorded."""
        from job_finder.web.data_enricher import _persist

        key = "co|valid-jd|test"
        self._insert_job(temp_db, key)

        good_jd = "This is a real job description with lots of detail. " * 4  # > 200 chars
        _persist(temp_db, {"dedup_key": key}, {"jd_full": good_jd}, "free")

        row = self._fetch(temp_db, key)
        assert row["jd_full"] == good_jd
        assert row["enrichment_tier"] == "free"

    # ------------------------------------------------------------------ #
    # salary — I-02 normalisation
    # ------------------------------------------------------------------ #

    def test_inverted_salary_swapped_and_written(self, temp_db):
        """An inverted salary pair (min > max, same magnitude) is swapped before the UPDATE."""
        from job_finder.web.data_enricher import _persist

        key = "anthropic|data-scientist|test"
        self._insert_job(temp_db, key)

        # Simulate the Anthropic parser-inversion bug: min and max are swapped
        _persist(
            temp_db,
            {"dedup_key": key},
            {"salary_min": 300_000, "salary_max": 200_000},
            "free",
        )

        row = self._fetch(temp_db, key)
        assert row["salary_min"] == 200_000, "Inverted salary_min should be swapped to 200k"
        assert row["salary_max"] == 300_000, "Inverted salary_max should be swapped to 300k"
        assert row["enrichment_tier"] == "free"

    def test_extreme_salary_inversion_dropped_tier_written(self, temp_db):
        """An extreme salary inversion (>10x ratio) drops both salary fields; tier still written."""
        from job_finder.web.data_enricher import _persist

        key = "co|extreme-sal|test"
        self._insert_job(temp_db, key)

        # 300000 vs 15 is clearly a unit mismatch (annual vs hourly)
        _persist(
            temp_db,
            {"dedup_key": key},
            {"salary_min": 300_000, "salary_max": 15},
            "free",
        )

        row = self._fetch(temp_db, key)
        assert row["salary_min"] is None, "Extreme-inversion salary_min must be dropped"
        assert row["salary_max"] is None, "Extreme-inversion salary_max must be dropped"
        assert row["enrichment_tier"] == "free", "Tier must be written even when salary is dropped"

    def test_normal_salary_order_written_unchanged(self, temp_db):
        """A correctly ordered salary pair passes through unchanged."""
        from job_finder.web.data_enricher import _persist

        key = "co|normal-sal|test"
        self._insert_job(temp_db, key)

        _persist(
            temp_db,
            {"dedup_key": key},
            {"salary_min": 120_000, "salary_max": 180_000},
            "ddg",
        )

        row = self._fetch(temp_db, key)
        assert row["salary_min"] == 120_000
        assert row["salary_max"] == 180_000
        assert row["enrichment_tier"] == "ddg"

    # ------------------------------------------------------------------ #
    # field isolation — one bad field must not discard siblings
    # ------------------------------------------------------------------ #

    def test_valid_location_written_when_jd_is_junk(self, temp_db):
        """When jd_full is junk and location is valid, location is still persisted."""
        from job_finder.web.data_enricher import _persist

        key = "co|partial-enrich|test"
        self._insert_job(temp_db, key)

        _persist(
            temp_db,
            {"dedup_key": key},
            {"jd_full": "sign in to view", "location": "San Francisco, CA"},
            "free",
        )

        row = self._fetch(temp_db, key)
        assert row["jd_full"] is None, "Junk jd_full must NOT be written"
        assert row["location"] == "San Francisco, CA", "Valid location must be written"
        assert row["enrichment_tier"] == "free"

    def test_tier_written_when_all_fields_are_junk(self, temp_db):
        """When every enriched field is dropped, tier is still recorded."""
        from job_finder.web.data_enricher import _persist

        key = "co|all-junk|test"
        self._insert_job(temp_db, key)

        _persist(
            temp_db,
            {"dedup_key": key},
            {"jd_full": "loading", "salary_min": 300_000, "salary_max": 15},
            "free",
        )

        row = self._fetch(temp_db, key)
        assert row["jd_full"] is None
        assert row["salary_min"] is None
        assert row["salary_max"] is None
        assert row["enrichment_tier"] == "free", (
            "Tier must be written even when all fields dropped"
        )

    # ------------------------------------------------------------------ #
    # trigger fallback — simulate a DB trigger rejecting the UPDATE
    # ------------------------------------------------------------------ #

    def test_trigger_rejection_still_records_tier(self, temp_db):
        """Even if a DB trigger fires on the remaining-fields UPDATE, the tier
        fallback UPDATE ensures the job is not re-fetched indefinitely."""
        from job_finder.web.data_enricher import _persist

        key = "co|trigger-test|test"
        self._insert_job(temp_db, key)

        # Install a trigger that rejects any location UPDATE with a custom error,
        # simulating a future invariant that the Python layer hasn't learnt about.
        temp_db.execute(
            "CREATE TRIGGER tg_test_reject_location "
            "BEFORE UPDATE OF location ON jobs "
            "BEGIN "
            "  SELECT RAISE(ABORT, 'X-01: test rejection'); "
            "END"
        )
        temp_db.commit()

        _persist(
            temp_db,
            {"dedup_key": key},
            {"location": "New York, NY"},
            "ddg",
        )

        row = self._fetch(temp_db, key)
        # location update was rejected by trigger — location stays default
        assert row["location"] == "Remote"
        # tier must still be written via the fallback path
        assert row["enrichment_tier"] == "ddg", (
            "Tier fallback UPDATE must succeed even when the main UPDATE is rejected"
        )


# ---------------------------------------------------------------------------
# Backward compatibility and never-raises (Phase 10 — updated)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("stub_enrichment_network")
class TestEnrichJobBackwardCompat:
    """Old call patterns and error handling still work."""

    def test_enrich_job_backward_compatible_signature(self, sparse_job_row, temp_db):
        """Call pattern (job_row, serpapi_key, conn, config) works with keyword args."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
        ):
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

        sparse_job_row["source_urls"] = "[]"

        with patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch:
            mock_fetch.side_effect = Exception("Unexpected catastrophic error")

            # Should not raise
            result = enrich_job(sparse_job_row, serpapi_key="test-key", conn=temp_db)

        assert isinstance(result, dict)

    def test_enrich_job_returns_empty_dict_when_nothing_missing(self, rich_job_row):
        """enrich_job returns empty dict when job already has all scoring-relevant data."""
        from job_finder.web.data_enricher import enrich_job

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serpapi,
        ):
            result = enrich_job(rich_job_row, serpapi_key="test-key")

        # Should return early without calling any tier
        mock_fetch.assert_not_called()
        mock_serpapi.assert_not_called()
        assert result == {}

    def test_enrich_job_skips_serpapi_when_no_key(self, sparse_job_row, temp_db):
        """enrich_job skips SerpAPI tier when serpapi_key is None."""
        from job_finder.web.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd") as mock_fetch,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serpapi,
            patch("job_finder.web.data_enricher.search_duckduckgo") as mock_ddg,
        ):
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
        """TIER_ORDER constant is exported from data_enricher (synthesis-free)."""
        from job_finder.web.data_enricher import TIER_ORDER

        assert isinstance(TIER_ORDER, list)
        assert "free" in TIER_ORDER
        assert "ddg" in TIER_ORDER
        assert "serpapi" in TIER_ORDER
        assert "agentic" in TIER_ORDER
        assert "exhausted" in TIER_ORDER
        # Synthesis tiers removed in Phase 2b sub-fix RC4
        assert "low" not in TIER_ORDER
        assert "mid" not in TIER_ORDER
        # Verify ordering: each tier before next
        assert TIER_ORDER.index("free") < TIER_ORDER.index("ddg")
        assert TIER_ORDER.index("ddg") < TIER_ORDER.index("serpapi")
        assert TIER_ORDER.index("serpapi") < TIER_ORDER.index("agentic")
        assert TIER_ORDER.index("agentic") < TIER_ORDER.index("exhausted")


# ---------------------------------------------------------------------------
# Pipeline integration tests (Phase 10 Plan 02 — NEW)
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    """Integration tests verifying pipeline_runner wiring and Migration 8 schema.

    Tests verify:
    - enrich_job is called BEFORE score_job_low in _run_low_scoring
    - _run_mid_evaluation does NOT reference fetch_jd
    - Migration 8 adds enrichment_tier column correctly
    - Migration 8 backfills existing enriched jobs as 'serpapi'
    - Migration 8 leaves unenriched jobs with NULL enrichment_tier
    """

    def test_run_scoring_does_not_call_fetch_jd(self):
        """run_scoring source does not reference fetch_jd.

        v3 unified runner inherits the legacy no-JD-fetch contract from
        run_mid_evaluation -- JD fetching is enrich_job's job.
        """
        from job_finder.web.scoring_runner import run_scoring

        source = inspect.getsource(run_scoring)
        assert "fetch_jd" not in source, (
            "run_scoring references fetch_jd -- JD fetching must remain exclusively in enrich_job"
        )

    def test_enrichment_tier_column_exists_after_migration(self, tmp_db_path):
        """Migration 8 adds enrichment_tier column to jobs table."""
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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

        html = (
            "<html><body><div id='main'><p>"
            "Senior Data Scientist role at Acme. We are looking for an experienced data "
            "scientist to join our growing analytics team. You will build production ML "
            "models, design and analyze A/B tests, partner with product and engineering "
            "teams, and present findings to leadership. Strong SQL and Python required."
            "</p></div></body></html>"
        )
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
            "<main><p>Job description content here. We are seeking a Senior Data "
            "Scientist to join our team and own end-to-end ML projects: data exploration, "
            "feature engineering, model training, evaluation, and deployment to "
            "production. Strong SQL, Python, and experimentation experience required.</p></main>"
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

    def test_caps_result_at_storage_limit(self):
        """_fetch_direct_jd() caps returned text at the shared JD storage limit.

        The cap is JD_STORAGE_MAX_CHARS (a storage bound well above the scorer's
        own prompt cap), not the old hard-coded 8000 that chopped readable JDs.
        """
        from unittest.mock import MagicMock, patch

        from job_finder.config import JD_STORAGE_MAX_CHARS
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        # Create a page with content longer than the cap so truncation triggers.
        long_content = "A" * (JD_STORAGE_MAX_CHARS + 10_000)
        html = f"<html><body><p>{long_content}</p></body></html>"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html
        mock_response.raise_for_status.return_value = None

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_response):
            result = _fetch_direct_jd("https://example.com/jobs/789")

        assert result is not None
        assert len(result) <= JD_STORAGE_MAX_CHARS

    def test_returns_none_on_timeout(self):
        """_fetch_direct_jd() returns None when requests.get raises Timeout."""
        from unittest.mock import patch

        import requests as req_lib

        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        with patch(
            "job_finder.web.enrichment_tiers.requests.get",
            side_effect=req_lib.Timeout("timed out"),
        ):
            result = _fetch_direct_jd("https://example.com/jobs/slow")

        assert result is None

    def test_returns_none_on_404(self):
        """_fetch_direct_jd() returns None when the response is a 404 error."""
        from unittest.mock import MagicMock, patch

        import requests as req_lib

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
        from unittest.mock import patch

        import requests as req_lib

        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        with patch(
            "job_finder.web.enrichment_tiers.requests.get",
            side_effect=req_lib.ConnectionError("refused"),
        ):
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
            "You will build ML models, analyze large datasets, and collaborate with "
            "engineers. The role spans the full ML lifecycle: ideation, prototyping, "
            "rigorous experimentation, productionization, and ongoing monitoring. We "
            "value strong technical writing and stakeholder communication skills.</p>"
            "</body></html>"
        )
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://example.com/jobs/data-scientist")

        assert result is not None, "Expected text returned for normal JD page"
        # Assert the JD *body* survived (auth-wall guard did not block it).
        # Not the <h1> "Data Scientist" — structure-aware extraction routes a
        # leading h1 to title-metadata (carried separately as job.title), so
        # the title heading is intentionally absent from the jd_full body.
        assert "ML models" in result
        assert "talented data scientist" in result.lower()

    def test_auth_wall_check_is_case_insensitive(self):
        """Auth wall detection is case-insensitive: 'WE'RE SIGNING YOU IN' -> None."""
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = "<html><body><p>WE'RE SIGNING YOU IN</p><p>DISCOVER PEOPLE, JOBS, AND MORE.</p></body></html>"
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://linkedin.com/jobs/view/uppercase/")

        assert result is None, "Expected None even for uppercase auth-wall text"

    def test_rejects_spa_shell_with_only_title(self):
        """JS-rendered SPA shell where only <title> survives noise-strip -> None.

        Regression test: Workday's user-facing pages return an HTML shell whose
        only static text is <title>Workday</title>. Before the min-length gate,
        this got persisted as jd_full="Workday" on 87% of Workday jobs.
        """
        from job_finder.web.enrichment_tiers import fetch_direct_jd as _fetch_direct_jd

        html = (
            "<html><head><title>Workday</title>"
            "<style>.app-frame { color: red; }</style>"
            "<script>var __WORKDAY_BOOTSTRAP__ = {};</script></head>"
            "<body id='vpsBody'></body></html>"
        )
        mock_resp = self._make_mock_response(html)

        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=mock_resp):
            result = _fetch_direct_jd("https://x.wd5.myworkdayjobs.com/en-US/careers/job/Foo")

        assert result is None, "SPA shell with only <title> should be rejected"


# ---------------------------------------------------------------------------
# Migration 15 tests (Phase 40 Data Quality — DQ-04, DQ-05)
# ---------------------------------------------------------------------------


class TestMigration15:
    """Migration 15 cleans poison data and promotes descriptions."""

    def test_migration_15_nullifies_poison_jd_full(self, tmp_db_path):
        """Poison jd_full with LinkedIn login text is nullified."""
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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
        row = conn.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE title = 'Data Scientist'"
        ).fetchone()
        conn.close()

        if row is not None:
            assert row["jd_full"] is None, (
                f"Poison jd_full should be NULL, got: {row['jd_full']!r}"
            )
            assert row["enrichment_tier"] == "ddg", (
                f"enrichment_tier should be 'ddg', got: {row['enrichment_tier']!r}"
            )

    def test_migration_15_deletes_notification_rows(self, tmp_db_path):
        """Garbage rows with notification text in title are deleted."""
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE title LIKE '%receive notifications%'"
        ).fetchone()[0]
        conn.close()

        assert count == 0, f"Notification garbage rows should be deleted, found {count}"

    def test_migration_15_promotes_descriptions(self, tmp_db_path):
        """Long descriptions are promoted to jd_full where jd_full is NULL."""
        from tests.helpers.contract_triggers import (
            run_migrations_without_contract as run_migrations,
        )

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


@pytest.mark.usefixtures("stub_enrichment_network")
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
            (
                job_row["dedup_key"],
                job_row["title"],
                job_row["company"],
                job_row["location"],
                long_desc,
            ),
        )
        promo_db.commit()

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None),
            patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None),
        ):
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

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None),
            patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None),
        ):
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

        with (
            patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None),
            patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None),
        ):
            enrich_job(job_row, conn=promo_db)

        # Verify DB row was updated with jd_full
        row = promo_db.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()

        assert row is not None, "Job row not found in DB"
        assert row["jd_full"] is not None, "Expected jd_full to be set in DB after promotion"
        from job_finder.config import JD_STORAGE_MAX_CHARS

        assert row["jd_full"] == long_desc[:JD_STORAGE_MAX_CHARS], (
            f"Expected jd_full in DB to match description (capped at JD_STORAGE_MAX_CHARS), "
            f"got: {row['jd_full']!r}"
        )


# ---------------------------------------------------------------------------
# run_enrichment_backfill SELECT correctness (regression test)
# ---------------------------------------------------------------------------


class TestRunEnrichmentBackfillSelect:
    """Regression test for the SELECT in run_enrichment_backfill.

    The pre-fix SELECT matched any row in a resumable tier regardless of
    whether fields were actually missing, so LIMIT N hit already-enriched
    rows first and the real backlog was never reached. The fix adds an
    AND clause filtering for actually-missing fields plus ORDER BY
    first_seen DESC.
    """

    @pytest.fixture
    def backfill_db_path(self, tmp_path):
        """File-based SQLite with the minimal jobs columns the SELECT needs."""
        db_path = tmp_path / "backfill.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                jd_full TEXT DEFAULT NULL,
                salary_min INTEGER DEFAULT NULL,
                salary_max INTEGER DEFAULT NULL,
                enrichment_tier TEXT DEFAULT NULL,
                first_seen TEXT DEFAULT NULL
            )
        """)
        rows = [
            # (dedup_key, jd_full, salary_min, tier, first_seen)
            # Already fully enriched -- must NOT be selected (the bug)
            ("enriched-old-1", "full JD text", 100_000, None, "2026-01-01"),
            ("enriched-old-2", "full JD text", 120_000, "low", "2026-01-02"),
            ("enriched-old-3", "full JD text", 150_000, "free", "2026-01-03"),
            # Terminal tier -- must NOT be selected regardless of fields
            ("terminal-serpapi", None, None, "serpapi", "2026-01-04"),
            ("terminal-mid", None, None, "mid", "2026-01-05"),
            ("terminal-exhausted", None, None, "exhausted", "2026-01-06"),
            # Legacy/unknown terminal tiers -- must NOT be selected (issue #255)
            ("terminal-low", "full JD", None, "low", "2026-01-07"),
            ("terminal-high", "full JD", None, "high", "2026-01-08"),
            ("terminal-ag-ex", None, None, "agentic_exhausted", "2026-01-09"),
            # Actually missing fields -- MUST be selected
            ("needs-jd-new-1", None, 90_000, None, "2026-04-23"),
            ("needs-sal-new-2", "full JD", None, "free", "2026-04-22"),
            ("needs-both-3", None, None, "ddg", "2026-04-21"),
        ]
        conn.executemany(
            "INSERT INTO jobs (dedup_key, jd_full, salary_min, enrichment_tier, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[3], r[4]) for r in rows],
        )
        # Title/company/location need values too (NOT NULL wasn't set, but keep clean)
        conn.execute("UPDATE jobs SET title = dedup_key, company = 'co', location = 'loc'")
        conn.commit()
        conn.close()
        return str(db_path)

    def test_skips_rows_where_all_fields_already_populated(self, backfill_db_path):
        """Rows with both jd_full and salary_min set must NOT be passed to enrich_job."""
        from job_finder.web.data_enricher import run_enrichment_backfill

        with patch("job_finder.web.data_enricher.enrich_job") as mock_enrich:
            mock_enrich.return_value = {}
            run_enrichment_backfill(backfill_db_path, limit=20)

        passed_keys = {call.args[0]["dedup_key"] for call in mock_enrich.call_args_list}
        assert "enriched-old-1" not in passed_keys
        assert "enriched-old-2" not in passed_keys
        assert "enriched-old-3" not in passed_keys

    def test_skips_terminal_tiers(self, backfill_db_path):
        """Rows with known terminal tiers must NOT be selected (issue #255)."""
        from job_finder.web.data_enricher import run_enrichment_backfill

        with patch("job_finder.web.data_enricher.enrich_job") as mock_enrich:
            mock_enrich.return_value = {}
            run_enrichment_backfill(backfill_db_path, limit=20)

        passed_keys = {call.args[0]["dedup_key"] for call in mock_enrich.call_args_list}
        assert "terminal-serpapi" not in passed_keys
        assert "terminal-mid" not in passed_keys
        assert "terminal-exhausted" not in passed_keys
        # Legacy migration tiers and agentic_exhausted are now also terminal (#255)
        assert "terminal-low" not in passed_keys
        assert "terminal-high" not in passed_keys
        assert "terminal-ag-ex" not in passed_keys

    def test_selects_only_rows_that_need_fields(self, backfill_db_path):
        """Only rows missing jd_full or salary_min should be passed to enrich_job."""
        from job_finder.web.data_enricher import run_enrichment_backfill

        with patch("job_finder.web.data_enricher.enrich_job") as mock_enrich:
            mock_enrich.return_value = {}
            run_enrichment_backfill(backfill_db_path, limit=20)

        passed_keys = {call.args[0]["dedup_key"] for call in mock_enrich.call_args_list}
        assert passed_keys == {"needs-jd-new-1", "needs-sal-new-2", "needs-both-3"}

    def test_orders_by_first_seen_desc(self, backfill_db_path):
        """Newest first_seen must be processed first (freshest rows are what users view)."""
        from job_finder.web.data_enricher import run_enrichment_backfill

        with patch("job_finder.web.data_enricher.enrich_job") as mock_enrich:
            mock_enrich.return_value = {}
            run_enrichment_backfill(backfill_db_path, limit=20)

        order = [call.args[0]["dedup_key"] for call in mock_enrich.call_args_list]
        # Expected descending by first_seen: 2026-04-23, 2026-04-22, 2026-04-21
        assert order == ["needs-jd-new-1", "needs-sal-new-2", "needs-both-3"]

    def test_limit_none_processes_full_backlog(self, backfill_db_path):
        """limit=None must not truncate — every eligible row is passed to enrich_job."""
        import sqlite3

        from job_finder.web.data_enricher import run_enrichment_backfill

        conn = sqlite3.connect(backfill_db_path)
        extra = [
            ("extra-a", None, 80_000, None, "2026-04-24"),
            ("extra-b", None, 70_000, "ddg", "2026-04-25"),
        ]
        conn.executemany(
            "INSERT INTO jobs (dedup_key, jd_full, salary_min, enrichment_tier, first_seen, "
            "title, company, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[3], r[4], r[0], "co", "loc") for r in extra],
        )
        conn.commit()
        conn.close()

        with patch("job_finder.web.data_enricher.enrich_job") as mock_enrich:
            mock_enrich.return_value = {}
            run_enrichment_backfill(backfill_db_path, limit=None)

        passed_keys = {call.args[0]["dedup_key"] for call in mock_enrich.call_args_list}
        assert passed_keys == {
            "extra-b",
            "extra-a",
            "needs-jd-new-1",
            "needs-sal-new-2",
            "needs-both-3",
        }
        # Newest first_seen first (2026-04-25 … 2026-04-21)
        order = [call.args[0]["dedup_key"] for call in mock_enrich.call_args_list]
        assert order == [
            "extra-b",
            "extra-a",
            "needs-jd-new-1",
            "needs-sal-new-2",
            "needs-both-3",
        ]


# ---------------------------------------------------------------------------
# Stub-JD gate: _is_stub_jd, _find_missing_fields, _resolve_from_fragments
# ---------------------------------------------------------------------------


class TestStubJdGate:
    """Verify that stub JDs (title-restatements < _MIN_JD_LENGTH chars) are
    rejected by _find_missing_fields and _resolve_from_fragments.

    This guards the stub-JD gating ported from the deleted
    enrichment_sources.{find_missing_fields,resolve_from_fragments} into the
    live data_enricher private helpers.

    Acceptance criterion: a fragment whose jd_full is a title-restatement stub
    must NOT be persisted — the helpers must treat it as if jd_full is missing
    so the pipeline escalates to a richer tier.
    """

    _STUB = "Software Engineer at Acme Corp"  # 30 chars — well below 200
    _REAL = "x" * 201  # 201 chars — above the 200-char threshold

    # ------------------------------------------------------------------ #
    # _is_stub_jd
    # ------------------------------------------------------------------ #

    def test_is_stub_jd_none_is_stub(self):
        """None jd_text → stub."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd(None) is True

    def test_is_stub_jd_empty_is_stub(self):
        """Empty string → stub."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd("") is True

    def test_is_stub_jd_short_is_stub(self):
        """Text shorter than _MIN_JD_LENGTH after strip → stub."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd(self._STUB) is True

    def test_is_stub_jd_exactly_200_is_not_stub(self):
        """Text of exactly 200 chars → NOT a stub (threshold is < 200)."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd("x" * 200) is False

    def test_is_stub_jd_real_jd_not_stub(self):
        """Text of 201+ chars → NOT a stub."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd(self._REAL) is False

    def test_is_stub_jd_whitespace_only_is_stub(self):
        """Whitespace-only (collapses to empty on strip) → stub."""
        from job_finder.web.data_enricher import _is_stub_jd

        assert _is_stub_jd("   \n\t  ") is True

    # ------------------------------------------------------------------ #
    # _find_missing_fields — stub JD treated as missing
    # ------------------------------------------------------------------ #

    def test_find_missing_fields_stub_jd_is_missing(self):
        """A stub jd_full (< 200 chars) is treated as missing."""
        from job_finder.web.data_enricher import _find_missing_fields

        row = {"jd_full": self._STUB, "title": "SWE", "company": "Acme", "salary_min": 100_000}
        assert "jd_full" in _find_missing_fields(row)

    def test_find_missing_fields_real_jd_not_missing(self):
        """A real jd_full (>= 200 chars) is NOT treated as missing."""
        from job_finder.web.data_enricher import _find_missing_fields

        row = {"jd_full": self._REAL, "title": "SWE", "company": "Acme", "salary_min": 100_000}
        assert "jd_full" not in _find_missing_fields(row)

    def test_find_missing_fields_none_jd_is_missing(self):
        """None jd_full → missing (baseline regression guard)."""
        from job_finder.web.data_enricher import _find_missing_fields

        row = {"jd_full": None, "salary_min": 100_000}
        assert "jd_full" in _find_missing_fields(row)

    # ------------------------------------------------------------------ #
    # _resolve_from_fragments — stub fragments rejected
    # ------------------------------------------------------------------ #

    def test_resolve_rejects_stub_jd_fragment(self):
        """A stub jd_full in fragments is NOT returned."""
        from job_finder.web.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._STUB}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result

    def test_resolve_rejects_stub_url_jd_fragment(self):
        """A stub url_jd (< 200 chars) is NOT mapped to jd_full."""
        from job_finder.web.data_enricher import _resolve_from_fragments

        fragments = {"url_jd": self._STUB}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result

    def test_resolve_accepts_real_jd_fragment(self):
        """A real jd_full (>= 200 chars) IS returned."""
        from job_finder.web.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._REAL}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert result.get("jd_full") == self._REAL

    def test_resolve_accepts_real_url_jd_fragment(self):
        """A real url_jd (>= 200 chars) IS mapped to jd_full."""
        from job_finder.web.data_enricher import _resolve_from_fragments

        fragments = {"url_jd": self._REAL}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert result.get("jd_full") == self._REAL

    def test_resolve_stub_does_not_block_non_jd_fields(self):
        """Stub jd rejection does not prevent other fields (salary_min) from resolving."""
        from job_finder.web.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._STUB, "salary_min": 120_000}
        result = _resolve_from_fragments(
            fragments, ["jd_full", "salary_min"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result
        assert result.get("salary_min") == 120_000
