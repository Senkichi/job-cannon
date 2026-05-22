"""Tests for GoogleCSESource — Stage 3 free SERP backend.

Coverage:
- fetch_jobs happy path (CSE response → Job objects)
- Empty / malformed results
- HTTP failure path is per-query, not fatal
- Missing api_key or cse_id short-circuits without HTTP
- Quota gate trips at the configured limit (default 95/day)
- _split_title_company helper
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from job_finder.sources.google_cse_source import (
    GoogleCSESource,
    _split_title_company,
)


def _make_cse_response(items: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"items": items}
    resp.raise_for_status = MagicMock()
    return resp


def _make_item(
    title: str = "Senior Engineer - Acme Corp",
    link: str = "https://wellfound.com/jobs/123",
    snippet: str = "We are hiring",
) -> dict:
    return {"title": title, "link": link, "snippet": snippet}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestSplitTitleCompany:
    def test_dash_separator(self):
        assert _split_title_company("Senior Engineer - Acme Corp") == (
            "Senior Engineer",
            "Acme Corp",
        )

    def test_pipe_separator(self):
        assert _split_title_company("Staff ML | Foo Inc") == ("Staff ML", "Foo Inc")

    def test_at_separator(self):
        assert _split_title_company("Data Scientist at OpenAI") == (
            "Data Scientist",
            "OpenAI",
        )

    def test_em_dash_separator(self):
        assert _split_title_company("Senior PM — Stripe") == ("Senior PM", "Stripe")

    def test_no_separator_falls_back(self):
        assert _split_title_company("Just A Title") == ("Just A Title", "")

    def test_empty_string(self):
        assert _split_title_company("") == ("", "")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestFetchJobs:
    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_basic_success(self, mock_get):
        mock_get.return_value = _make_cse_response(
            [
                _make_item(
                    title="Senior Engineer - Acme",
                    link="https://wellfound.com/jobs/123",
                ),
                _make_item(
                    title="Staff ML | Foo Inc",
                    link="https://weworkremotely.com/jobs/456",
                ),
            ]
        )
        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs([{"query": "site:wellfound.com python", "location": ""}])

        assert len(jobs) == 2
        assert jobs[0].title == "Senior Engineer"
        assert jobs[0].company == "Acme"
        assert jobs[0].source == "portal_serp_cse"
        assert jobs[0].source_url == "https://wellfound.com/jobs/123"
        assert jobs[1].title == "Staff ML"
        assert jobs[1].company == "Foo Inc"

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_url_dedup_within_response(self, mock_get):
        mock_get.return_value = _make_cse_response(
            [
                _make_item(link="https://wellfound.com/same"),
                _make_item(link="https://wellfound.com/same", title="Other - Co"),
            ]
        )
        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs([{"query": "site:wellfound.com kw", "location": ""}])
        assert len(jobs) == 1

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_empty_items_returns_no_jobs(self, mock_get):
        mock_get.return_value = _make_cse_response([])
        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs([{"query": "site:foo.com bar", "location": ""}])
        assert jobs == []

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_item_missing_link_skipped(self, mock_get):
        mock_get.return_value = _make_cse_response(
            [
                {"title": "Title", "snippet": ""},  # no link
                _make_item(link="https://wellfound.com/ok"),
            ]
        )
        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs([{"query": "site:wellfound.com", "location": ""}])
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Missing credentials
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_no_api_key_returns_empty(self, mock_get):
        src = GoogleCSESource(api_key="", cse_id="cx")
        jobs = src.fetch_jobs([{"query": "site:foo.com", "location": ""}])
        assert jobs == []
        mock_get.assert_not_called()

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_no_cse_id_returns_empty(self, mock_get):
        src = GoogleCSESource(api_key="k", cse_id="")
        jobs = src.fetch_jobs([{"query": "site:foo.com", "location": ""}])
        assert jobs == []
        mock_get.assert_not_called()

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_no_queries_returns_empty(self, mock_get):
        src = GoogleCSESource(api_key="k", cse_id="cx")
        assert src.fetch_jobs([]) == []
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_http_error_continues_to_next_query(self, mock_get):
        ok = _make_cse_response([_make_item(link="https://wellfound.com/ok")])
        fail = MagicMock()
        fail.raise_for_status.side_effect = ConnectionError("boom")
        mock_get.side_effect = [fail, ok]

        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs(
            [
                {"query": "site:dead.com kw", "location": ""},
                {"query": "site:wellfound.com kw", "location": ""},
            ]
        )
        # First query failed; second succeeded.
        assert len(jobs) == 1
        assert jobs[0].source_url == "https://wellfound.com/ok"

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_invalid_json_continues(self, mock_get):
        bad = MagicMock()
        bad.raise_for_status = MagicMock()
        bad.json.side_effect = ValueError("not json")
        ok = _make_cse_response([_make_item()])
        mock_get.side_effect = [bad, ok]

        src = GoogleCSESource(api_key="k", cse_id="cx")
        jobs = src.fetch_jobs(
            [
                {"query": "q1", "location": ""},
                {"query": "q2", "location": ""},
            ]
        )
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Quota gate
# ---------------------------------------------------------------------------


class TestQuotaGate:
    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_quota_gate_trips_at_limit(self, mock_get, caplog):
        # Use a small limit to keep the test fast.
        mock_get.return_value = _make_cse_response([_make_item()])
        src = GoogleCSESource(api_key="k", cse_id="cx", quota_limit_per_day=3)

        # 5 queries; gate stops at 3.
        queries = [{"query": f"q{i}", "location": ""} for i in range(5)]
        with caplog.at_level("WARNING"):
            src.fetch_jobs(queries)

        # Only the first 3 calls go through.
        assert mock_get.call_count == 3
        # Warning logged when 4th query is attempted.
        assert any("CSE quota nearly exhausted" in rec.getMessage() for rec in caplog.records)

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_quota_persists_across_fetch_calls_same_day(self, mock_get):
        mock_get.return_value = _make_cse_response([])
        src = GoogleCSESource(api_key="k", cse_id="cx", quota_limit_per_day=2)

        src.fetch_jobs([{"query": "q1", "location": ""}])
        src.fetch_jobs([{"query": "q2", "location": ""}])
        # Third call from a separate fetch_jobs invocation — gate should still trip.
        src.fetch_jobs([{"query": "q3", "location": ""}])

        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_quota_rolls_over_on_new_day(self, mock_get):
        mock_get.return_value = _make_cse_response([])
        src = GoogleCSESource(api_key="k", cse_id="cx", quota_limit_per_day=1)

        # Burn the day-1 quota.
        src.fetch_jobs([{"query": "q1", "location": ""}])
        assert mock_get.call_count == 1

        # Simulate a new day passing.
        src._quota_day = date.today() - timedelta(days=1)

        # New day → quota resets → next call goes through.
        src.fetch_jobs([{"query": "q2", "location": ""}])
        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_default_quota_limit_is_95(self, mock_get):
        # Sanity check on the documented defense-in-depth threshold.
        src = GoogleCSESource(api_key="k", cse_id="cx")
        assert src._quota_limit == 95


# ---------------------------------------------------------------------------
# F2 — DB-backed quota counter (2026-05-22)
# ---------------------------------------------------------------------------

import sqlite3 as _sqlite3


def _make_quota_db(tmp_path):
    """Create a minimal scoring_costs table matching m001 + m018."""
    db_path = tmp_path / "jobs.db"
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL,
            provider TEXT DEFAULT 'anthropic'
        )"""
    )
    conn.commit()
    conn.close()
    return str(db_path)


class TestDbBackedQuota:
    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_each_query_inserts_a_zero_cost_row(self, mock_get, tmp_path):
        """Every CSE call must append a (provider='google_cse', cost_usd=0) row."""
        db_path = _make_quota_db(tmp_path)
        mock_get.return_value = _make_cse_response([_make_item()])

        src = GoogleCSESource(api_key="k", cse_id="cx", db_path=db_path)
        src.fetch_jobs(
            [
                {"query": "q1", "location": ""},
                {"query": "q2", "location": ""},
                {"query": "q3", "location": ""},
            ]
        )

        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*), SUM(cost_usd) FROM scoring_costs WHERE provider='google_cse'"
        ).fetchone()
        conn.close()
        assert row[0] == 3
        assert row[1] == 0  # always zero-cost rows

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_db_count_survives_new_source_instance(self, mock_get, tmp_path):
        """Restart simulation: a fresh GoogleCSESource sees prior day's count via DB."""
        db_path = _make_quota_db(tmp_path)
        mock_get.return_value = _make_cse_response([])

        # Run 1 — consume 2 of a 2-cap.
        src1 = GoogleCSESource(api_key="k", cse_id="cx", db_path=db_path, quota_limit_per_day=2)
        src1.fetch_jobs([{"query": "a", "location": ""}, {"query": "b", "location": ""}])
        assert mock_get.call_count == 2

        # Run 2 — fresh instance (simulates Flask restart). Cap is 2, DB already
        # has 2 rows for today, so no more queries should fire.
        src2 = GoogleCSESource(api_key="k", cse_id="cx", db_path=db_path, quota_limit_per_day=2)
        src2.fetch_jobs([{"query": "c", "location": ""}])
        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_only_today_rows_count(self, mock_get, tmp_path):
        """Yesterday's rows must not count against today's quota."""
        db_path = _make_quota_db(tmp_path)
        mock_get.return_value = _make_cse_response([])

        # Pre-seed 5 rows with yesterday's timestamp.
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        conn = _sqlite3.connect(db_path)
        for _ in range(5):
            conn.execute(
                "INSERT INTO scoring_costs (purpose, model, cost_usd, timestamp, provider)"
                " VALUES (?, ?, 0, ?, ?)",
                ("cse_query", "cse_query", yesterday, "google_cse"),
            )
        conn.commit()
        conn.close()

        # Cap is 2. Today's count should be 0 → first 2 queries fire.
        src = GoogleCSESource(api_key="k", cse_id="cx", db_path=db_path, quota_limit_per_day=2)
        src.fetch_jobs([{"query": f"q{i}", "location": ""} for i in range(4)])
        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_other_providers_not_counted(self, mock_get, tmp_path):
        """anthropic/openrouter rows must not bleed into the CSE quota."""
        db_path = _make_quota_db(tmp_path)
        mock_get.return_value = _make_cse_response([])

        now = datetime.now().isoformat()
        conn = _sqlite3.connect(db_path)
        for provider in ("anthropic", "openrouter", "gemini"):
            for _ in range(10):
                conn.execute(
                    "INSERT INTO scoring_costs (purpose, model, cost_usd, timestamp, provider)"
                    " VALUES (?, ?, 0.001, ?, ?)",
                    ("scoring", "claude-3", now, provider),
                )
        conn.commit()
        conn.close()

        src = GoogleCSESource(api_key="k", cse_id="cx", db_path=db_path, quota_limit_per_day=2)
        src.fetch_jobs([{"query": "q1", "location": ""}, {"query": "q2", "location": ""}])
        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_db_write_failure_falls_back_to_in_process(self, mock_get, tmp_path):
        """Broken DB path must not break ingestion — fall back to the in-process counter."""
        mock_get.return_value = _make_cse_response([])
        bad_path = str(tmp_path / "does_not_exist" / "jobs.db")

        src = GoogleCSESource(api_key="k", cse_id="cx", db_path=bad_path, quota_limit_per_day=2)
        # Should not raise; should still respect the cap via the fallback counter.
        src.fetch_jobs(
            [
                {"query": "q1", "location": ""},
                {"query": "q2", "location": ""},
                {"query": "q3", "location": ""},
            ]
        )
        assert mock_get.call_count == 2

    @patch("job_finder.sources.google_cse_source.requests.get")
    def test_in_process_mode_unchanged_when_db_path_none(self, mock_get, tmp_path):
        """db_path=None preserves the pre-F2 in-process counter behavior."""
        db_path = _make_quota_db(tmp_path)
        mock_get.return_value = _make_cse_response([])

        src = GoogleCSESource(api_key="k", cse_id="cx", db_path=None, quota_limit_per_day=2)
        src.fetch_jobs(
            [
                {"query": "q1", "location": ""},
                {"query": "q2", "location": ""},
                {"query": "q3", "location": ""},
            ]
        )
        assert mock_get.call_count == 2
        # No rows should have been written to the DB (because db_path is None).
        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM scoring_costs WHERE provider='google_cse'"
        ).fetchone()
        conn.close()
        assert row[0] == 0
