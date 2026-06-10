"""Canary tests for fail-closed terminal-tier handling in data_enricher.

Covers:
- _start_tier_index: known tiers return correct indices; None/'free' start at 0;
  unknown/legacy tiers return len(TIER_ORDER) (fail-closed) and log a warning.
- Backfill SQL skip-set: rows with agentic_exhausted/low/high enrichment_tier
  are not selected even when jd_full IS NULL.
- enrich_job: an agentic_exhausted row returns {} without invoking any tier
  fetcher (DDG, SerpAPI, agentic).
"""

import logging
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from job_finder.web.data_enricher import TIER_ORDER, _start_tier_index

# ---------------------------------------------------------------------------
# _start_tier_index
# ---------------------------------------------------------------------------


class TestStartTierIndex:
    """_start_tier_index returns the correct resume index for every case."""

    def test_none_returns_zero(self):
        assert _start_tier_index(None) == 0

    def test_free_returns_one(self):
        # 'free' is TIER_ORDER[0]; idx+1 = 1 (resume from ddg, not restart)
        assert _start_tier_index("free") == TIER_ORDER.index("free") + 1

    def test_known_tier_returns_next_index(self):
        assert _start_tier_index("ddg") == TIER_ORDER.index("ddg") + 1
        assert _start_tier_index("serpapi") == TIER_ORDER.index("serpapi") + 1
        assert _start_tier_index("agentic") == TIER_ORDER.index("agentic") + 1

    def test_exhausted_is_past_end(self):
        # 'exhausted' is the last element; idx+1 == len(TIER_ORDER)
        assert _start_tier_index("exhausted") == len(TIER_ORDER)

    def test_agentic_exhausted_returns_len_tier_order(self):
        assert _start_tier_index("agentic_exhausted") == len(TIER_ORDER)

    def test_low_returns_len_tier_order(self):
        assert _start_tier_index("low") == len(TIER_ORDER)

    def test_high_returns_len_tier_order(self):
        assert _start_tier_index("high") == len(TIER_ORDER)

    def test_unknown_tier_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="job_finder.web.data_enricher"):
            result = _start_tier_index("agentic_exhausted")
        assert result == len(TIER_ORDER)
        assert "agentic_exhausted" in caplog.text
        assert any(
            "terminal" in r.message.lower() or "fail-closed" in r.message.lower()
            for r in caplog.records
        )

    def test_arbitrary_unknown_tier_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="job_finder.web.data_enricher"):
            result = _start_tier_index("some_future_tier")
        assert result == len(TIER_ORDER)
        assert "some_future_tier" in caplog.text


# ---------------------------------------------------------------------------
# Backfill SQL skip-set
# ---------------------------------------------------------------------------


def _make_minimal_db() -> tuple[str, sqlite3.Connection]:
    """Create a temp SQLite file with a minimal jobs table and return (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            dedup_key TEXT,
            title TEXT,
            company TEXT,
            enrichment_tier TEXT,
            jd_full TEXT,
            salary_min REAL,
            salary_max REAL,
            location TEXT,
            first_seen TEXT
        )"""
    )
    conn.commit()
    return path, conn


@pytest.fixture
def minimal_db():
    path, conn = _make_minimal_db()
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


_BACKFILL_SQL = """
    SELECT * FROM jobs
    WHERE (enrichment_tier IS NULL
           OR enrichment_tier NOT IN ('exhausted', 'serpapi', 'agentic', 'mid',
                                      'agentic_exhausted', 'low', 'high'))
      AND (jd_full IS NULL OR jd_full = '' OR salary_min IS NULL)
    ORDER BY first_seen DESC
"""


class TestBackfillSkipSet:
    """Terminal-tier rows must not be selected by the backfill query."""

    def _insert(self, conn, tier, jd_full=None, salary_min=None):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, enrichment_tier, jd_full, salary_min) "
            "VALUES (?, 'Eng', 'Corp', ?, ?, ?)",
            (f"key-{tier}", tier, jd_full, salary_min),
        )
        conn.commit()

    def test_agentic_exhausted_not_selected_with_null_jd(self, minimal_db):
        path, conn = minimal_db
        self._insert(conn, "agentic_exhausted", jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert not any(r["enrichment_tier"] == "agentic_exhausted" for r in rows)

    def test_low_not_selected_with_null_jd(self, minimal_db):
        path, conn = minimal_db
        self._insert(conn, "low", jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert not any(r["enrichment_tier"] == "low" for r in rows)

    def test_high_not_selected_with_null_jd(self, minimal_db):
        path, conn = minimal_db
        self._insert(conn, "high", jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert not any(r["enrichment_tier"] == "high" for r in rows)

    def test_null_tier_is_selected(self, minimal_db):
        """NULL enrichment_tier (brand-new job) should still be selected."""
        path, conn = minimal_db
        self._insert(conn, None, jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert any(r["enrichment_tier"] is None for r in rows)

    def test_ddg_tier_is_selected(self, minimal_db):
        """'ddg' is resumable and should appear in results."""
        path, conn = minimal_db
        self._insert(conn, "ddg", jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert any(r["enrichment_tier"] == "ddg" for r in rows)

    def test_exhausted_standard_not_selected(self, minimal_db):
        """Standard 'exhausted' tier should not be selected."""
        path, conn = minimal_db
        self._insert(conn, "exhausted", jd_full=None)
        rows = conn.execute(_BACKFILL_SQL).fetchall()
        assert not any(r["enrichment_tier"] == "exhausted" for r in rows)


# ---------------------------------------------------------------------------
# enrich_job: agentic_exhausted row returns {} without calling any tier fetcher
# ---------------------------------------------------------------------------


class TestEnrichJobTerminalTier:
    """enrich_job on a terminal unknown tier must return {} and touch no fetchers."""

    def _make_job_row(self, tier: str) -> dict:
        return {
            "dedup_key": f"dk-{tier}",
            "title": "Software Engineer",
            "company": "Acme Corp",
            "enrichment_tier": tier,
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "location": None,
            "url": "https://example.com/job/123",
            "description": None,
            "company_id": None,
        }

    def test_agentic_exhausted_returns_empty(self):
        from job_finder.web.data_enricher import enrich_job

        with (
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            result = enrich_job(self._make_job_row("agentic_exhausted"))

        assert result == {}
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_low_returns_empty(self):
        from job_finder.web.data_enricher import enrich_job

        with (
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            result = enrich_job(self._make_job_row("low"))

        assert result == {}
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_high_returns_empty(self):
        from job_finder.web.data_enricher import enrich_job

        with (
            patch("job_finder.web.data_enricher.search_ddg_web") as mock_ddg,
            patch("job_finder.web.data_enricher.search_serpapi") as mock_serp,
        ):
            result = enrich_job(self._make_job_row("high"))

        assert result == {}
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()
