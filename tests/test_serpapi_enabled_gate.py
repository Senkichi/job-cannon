"""Tests for issue #304: enrichment SerpAPI tier respects sources.serpapi.enabled
and sources.serpapi.daily_call_cap.

Acceptance criteria:
  - With SERPAPI_API_KEY set but sources.serpapi.enabled=false, zero SerpAPI calls.
  - daily_call_cap halts the tier once reached; resumes next day (ledger rolled).
  - Calls are recorded in the scoring_costs ledger so the cap survives restarts.
"""

import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web.data_enricher import (
    _record_serpapi_call,
    _serpapi_daily_calls_used,
    enrich_job,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_conn():
    """In-memory SQLite with the tables enrich_job touches."""
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
            timestamp TEXT NOT NULL,
            provider TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            jd_full TEXT,
            salary_min INTEGER,
            salary_max INTEGER,
            source_urls TEXT DEFAULT '[]',
            company_id INTEGER,
            enrichment_tier TEXT,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


@pytest.fixture()
def sparse_job():
    """Job row that needs enrichment (no jd_full, no salary)."""
    return {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "jd_full": None,
        "salary_min": None,
        "salary_max": None,
        "source_urls": "[]",
        "company_id": None,
        "enrichment_tier": None,
        "description": None,
    }


# Neutralise every tier except serpapi so tests exercise only the gate logic.
@pytest.fixture(autouse=True)
def _neutralise_other_tiers():
    """Stub free/DDG/agentic so only the serpapi path is under test."""
    stubs = {
        "fetch_direct_jd": None,
        "query_ats_api": {},
        "scrape_careers": {},
        "search_ddg_web": {},
        "fetch_ddg_jds": (None, None),
        "search_duckduckgo": None,
    }
    patchers = [
        patch(f"job_finder.web.data_enricher.{name}", return_value=ret)
        for name, ret in stubs.items()
    ]
    for p in patchers:
        p.start()
    # enrich_job no longer runs the agentic tier synchronously (2026-06-22); the
    # cascade terminates at 'exhausted' with no Playwright/Ollama I/O to stub.
    yield
    for p in patchers:
        p.stop()


# ---------------------------------------------------------------------------
# Test: enabled=false blocks calls even when key is present
# ---------------------------------------------------------------------------


class TestSerpApiEnabledGate:
    def test_disabled_flag_prevents_serpapi_call(self, sparse_job, mem_conn):
        """sources.serpapi.enabled=false must produce zero SerpAPI calls."""
        config = {"sources": {"serpapi": {"enabled": False}}}

        with patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_disabled_flag_records_no_ledger_row(self, sparse_job, mem_conn):
        """When disabled, no scoring_costs row should be written for serpapi."""
        config = {"sources": {"serpapi": {"enabled": False}}}

        with patch("job_finder.web.data_enricher.search_serpapi"):
            enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        count = mem_conn.execute(
            "SELECT COUNT(*) FROM scoring_costs WHERE provider='serpapi_enrichment'"
        ).fetchone()[0]
        assert count == 0

    def test_enabled_true_allows_serpapi_call(self, sparse_job, mem_conn):
        """sources.serpapi.enabled=true (explicit) allows the tier to fire."""
        config = {"sources": {"serpapi": {"enabled": True}}}

        with patch(
            "job_finder.web.data_enricher.search_serpapi", return_value=(None, [])
        ) as mock_serp:
            enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_absent_enabled_key_allows_serpapi_call(self, sparse_job, mem_conn):
        """When sources.serpapi.enabled is absent, default is True (backward compat)."""
        config = {"sources": {"serpapi": {}}}  # no 'enabled' key

        with patch(
            "job_finder.web.data_enricher.search_serpapi", return_value=(None, [])
        ) as mock_serp:
            enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_no_key_still_skips_serpapi(self, sparse_job, mem_conn):
        """Even with enabled=true, no serpapi_key means the tier is skipped."""
        config = {"sources": {"serpapi": {"enabled": True}}}

        with patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            enrich_job(sparse_job, serpapi_key=None, conn=mem_conn, config=config)

        mock_serp.assert_not_called()


# ---------------------------------------------------------------------------
# Test: daily_call_cap halts the tier once reached
# ---------------------------------------------------------------------------


class TestSerpApiDailyCap:
    def test_cap_not_reached_allows_call(self, sparse_job, mem_conn):
        """When cap=5 and 0 calls logged today, the tier fires normally."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 5}}}

        with patch(
            "job_finder.web.data_enricher.search_serpapi", return_value=(None, [])
        ) as mock_serp:
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_cap_reached_blocks_call(self, sparse_job, mem_conn):
        """When cap=3 and 3 calls already logged, the tier is skipped."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 3}}}
        # Pre-seed 3 ledger rows for today
        for _ in range(3):
            _record_serpapi_call(mem_conn)

        with patch("job_finder.web.data_enricher.search_serpapi") as mock_serp:
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_cap_zero_means_uncapped(self, sparse_job, mem_conn):
        """daily_call_cap=0 means no cap — tier always fires when enabled."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 0}}}
        # Seed many rows — should not matter
        for _ in range(100):
            _record_serpapi_call(mem_conn)

        with patch(
            "job_finder.web.data_enricher.search_serpapi", return_value=(None, [])
        ) as mock_serp:
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_cap_absent_means_uncapped(self, sparse_job, mem_conn):
        """Absent daily_call_cap (no key) means no cap."""
        config = {"sources": {"serpapi": {"enabled": True}}}
        for _ in range(50):
            _record_serpapi_call(mem_conn)

        with patch(
            "job_finder.web.data_enricher.search_serpapi", return_value=(None, [])
        ) as mock_serp:
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_successful_call_increments_ledger(self, sparse_job, mem_conn):
        """A fired SerpAPI call writes exactly one row to scoring_costs."""
        config = {"sources": {"serpapi": {"enabled": True}}}
        before = _serpapi_daily_calls_used(mem_conn)

        with patch("job_finder.web.data_enricher.search_serpapi", return_value=(None, [])):
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        after = _serpapi_daily_calls_used(mem_conn)
        assert after == before + 1

    def test_disabled_call_does_not_increment_ledger(self, sparse_job, mem_conn):
        """A skipped (disabled) call must not write to the ledger."""
        config = {"sources": {"serpapi": {"enabled": False}}}
        before = _serpapi_daily_calls_used(mem_conn)

        with patch("job_finder.web.data_enricher.search_serpapi"):
            enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        after = _serpapi_daily_calls_used(mem_conn)
        assert after == before


# ---------------------------------------------------------------------------
# Test: ledger helpers directly
# ---------------------------------------------------------------------------


class TestSerpApiLedgerHelpers:
    def test_daily_calls_used_zero_on_empty_db(self, mem_conn):
        assert _serpapi_daily_calls_used(mem_conn) == 0

    def test_daily_calls_used_none_conn_returns_zero(self):
        assert _serpapi_daily_calls_used(None) == 0

    def test_record_call_increments_count(self, mem_conn):
        _record_serpapi_call(mem_conn)
        assert _serpapi_daily_calls_used(mem_conn) == 1
        _record_serpapi_call(mem_conn)
        assert _serpapi_daily_calls_used(mem_conn) == 2

    def test_record_call_none_conn_is_noop(self):
        """_record_serpapi_call(None) must not raise."""
        _record_serpapi_call(None)  # should not raise

    def test_ledger_row_has_correct_provider(self, mem_conn):
        _record_serpapi_call(mem_conn)
        row = mem_conn.execute("SELECT provider, purpose, cost_usd FROM scoring_costs").fetchone()
        assert row["provider"] == "serpapi_enrichment"
        assert row["purpose"] == "serpapi_enrichment"
        assert row["cost_usd"] == 0.0
