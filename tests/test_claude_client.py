"""Tests for claude_client.py — record_cost, provider breakdown, and schema validation retry."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import ValidationError, validate

from job_finder.web.claude_client import call_claude, record_cost


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


def test_record_cost_free_provider_records_zero(migrated_db):
    """record_cost() sets cost_usd=0.0 for free/subscription providers."""
    _, conn = migrated_db
    cost = record_cost(conn, "job3", "haiku_score", "qwen2.5:14b", 1000, 500, provider="ollama")
    assert cost == 0.0
    row = conn.execute(
        "SELECT cost_usd, input_tokens, output_tokens FROM scoring_costs WHERE job_id = ?",
        ("job3",),
    ).fetchone()
    assert row[0] == 0.0
    # Token counts still recorded for analytics
    assert row[1] == 1000
    assert row[2] == 500


def test_record_cost_claude_cli_records_zero(migrated_db):
    """record_cost() with provider='claude_cli' records $0 (subscription-based)."""
    _, conn = migrated_db
    cost = record_cost(conn, "job4", "sonnet_eval", "claude-sonnet-4-6", 2000, 800, provider="claude_cli")
    assert cost == 0.0


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


# ---------------------------------------------------------------------------
# Tests: call_claude schema validation retry
# ---------------------------------------------------------------------------


def _make_oneshot_envelope(data: dict):
    """Build a mock _run_oneshot return envelope with structured output."""
    return {
        "result": json.dumps(data),
        "structured_output": data,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


class TestCallClaudeValidationRetry:
    SCHEMA = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "summary": {"type": "string"},
        },
        "required": ["score", "summary"],
    }

    @patch("job_finder.web.claude_client._run_oneshot")
    def test_no_retry_when_valid(self, mock_oneshot, migrated_db):
        _, conn = migrated_db
        mock_oneshot.return_value = _make_oneshot_envelope(
            {"score": 75, "summary": "Good"}
        )

        result, cost = call_claude(
            model="claude-haiku-4-5", system="test",
            messages=[{"role": "user", "content": "test"}],
            output_schema=self.SCHEMA, conn=conn, purpose="test",
        )
        assert result["score"] == 75
        assert mock_oneshot.call_count == 1

    @patch("job_finder.web.claude_client._run_oneshot")
    def test_retry_on_invalid_score(self, mock_oneshot, migrated_db):
        _, conn = migrated_db
        mock_oneshot.side_effect = [
            _make_oneshot_envelope({"score": 150, "summary": "Bad"}),
            _make_oneshot_envelope({"score": 85, "summary": "Fixed"}),
        ]

        result, cost = call_claude(
            model="claude-haiku-4-5", system="test",
            messages=[{"role": "user", "content": "test"}],
            output_schema=self.SCHEMA, conn=conn, purpose="test",
        )
        assert result["score"] == 85
        assert mock_oneshot.call_count == 2

    @patch("job_finder.web.claude_client._run_oneshot")
    def test_raises_on_second_failure(self, mock_oneshot, migrated_db):
        _, conn = migrated_db
        mock_oneshot.side_effect = [
            _make_oneshot_envelope({"score": 150, "summary": "Bad"}),
            _make_oneshot_envelope({"score": -10, "summary": "Still bad"}),
        ]

        with pytest.raises(ValueError, match="Schema validation failed after retry"):
            call_claude(
                model="claude-haiku-4-5", system="test",
                messages=[{"role": "user", "content": "test"}],
                output_schema=self.SCHEMA, conn=conn, purpose="test",
            )

    @patch("job_finder.web.claude_client._run_oneshot")
    def test_no_validation_without_schema(self, mock_oneshot, migrated_db):
        """Without output_schema, no validation occurs even with odd data."""
        _, conn = migrated_db
        mock_oneshot.return_value = {
            "result": json.dumps({"score": 999}),
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }

        result, cost = call_claude(
            model="claude-haiku-4-5", system="test",
            messages=[{"role": "user", "content": "test"}],
            conn=conn, purpose="test",
        )
        assert result["score"] == 999
        assert mock_oneshot.call_count == 1
