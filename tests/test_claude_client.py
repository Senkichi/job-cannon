"""Tests for claude_client.py — record_cost provider parameter and get_monthly_provider_breakdown."""

from datetime import datetime, timezone

import pytest

from job_finder.web.claude_client import record_cost


def test_record_cost_default_provider(migrated_db):
    """record_cost() with no provider arg inserts provider='anthropic'."""
    _, conn = migrated_db
    record_cost(conn, "job1", "haiku_score", "claude-haiku-4-5", 100, 50)
    row = conn.execute(
        "SELECT provider FROM scoring_costs WHERE job_id = ?", ("job1",)
    ).fetchone()
    assert row is not None
    assert row[0] == "anthropic"


def test_record_cost_explicit_provider(migrated_db):
    """record_cost() with provider='gemini' inserts provider='gemini'."""
    _, conn = migrated_db
    record_cost(conn, "job2", "haiku_score", "gemini-2.0-flash", 100, 50, provider="gemini")
    row = conn.execute(
        "SELECT provider FROM scoring_costs WHERE job_id = ?", ("job2",)
    ).fetchone()
    assert row is not None
    assert row[0] == "gemini"


# ---------------------------------------------------------------------------
# Tests: get_monthly_provider_breakdown
# ---------------------------------------------------------------------------


class TestGetMonthlyProviderBreakdown:
    def test_empty_when_no_rows(self, migrated_db):
        """Returns empty list when scoring_costs has no rows."""
        path, conn = migrated_db
        from job_finder.web.claude_client import get_monthly_provider_breakdown
        result = get_monthly_provider_breakdown(conn)
        assert result == []

    def test_groups_by_provider(self, migrated_db):
        """After inserting 3 rows (2 anthropic, 1 gemini), returns 2 dicts ordered by spend DESC."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        conn.executemany(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("j1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.01, ts, "anthropic"),
                ("j2", "sonnet_eval", "claude-sonnet-4-6", 200, 100, 0.05, ts, "anthropic"),
                ("j3", "haiku_score", "gemini-2.0-flash", 150, 75, 0.0, ts, "gemini"),
            ],
        )
        conn.commit()
        from job_finder.web.claude_client import get_monthly_provider_breakdown
        result = get_monthly_provider_breakdown(conn)
        assert len(result) == 2
        assert result[0]["provider"] == "anthropic"  # higher spend first
        assert result[0]["calls"] == 2
        assert result[0]["spend"] == pytest.approx(0.06)
        assert result[1]["provider"] == "gemini"
        assert result[1]["calls"] == 1
        assert result[1]["spend"] == pytest.approx(0.0)

    def test_dict_keys(self, migrated_db):
        """Each dict has keys 'provider', 'calls', 'spend' with correct types."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "haiku_score", "m", 100, 50, 0.01, ts, "anthropic"),
        )
        conn.commit()
        from job_finder.web.claude_client import get_monthly_provider_breakdown
        result = get_monthly_provider_breakdown(conn)
        assert len(result) == 1
        item = result[0]
        assert set(item.keys()) == {"provider", "calls", "spend"}
        assert isinstance(item["provider"], str)
        assert isinstance(item["calls"], int)
        assert isinstance(item["spend"], float)

    def test_excludes_old_months(self, migrated_db):
        """Only rows from current calendar month are included."""
        path, conn = migrated_db
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "haiku_score", "m", 100, 50, 0.01, "2020-01-15T12:00:00Z", "anthropic"),
        )
        conn.commit()
        from job_finder.web.claude_client import get_monthly_provider_breakdown
        result = get_monthly_provider_breakdown(conn)
        assert result == []
