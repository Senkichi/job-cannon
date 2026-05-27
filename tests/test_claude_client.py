"""Tests for claude_client.py — record_cost, provider breakdown, and schema validation retry."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from job_finder.web.claude_client import call_claude, record_cost


def test_record_cost_default_provider(migrated_db):
    """record_cost() with no provider arg inserts provider='anthropic'."""
    _, conn = migrated_db
    record_cost(conn, "job1", "haiku_score", "claude-haiku-4-5", 100, 50)
    row = conn.execute("SELECT provider FROM scoring_costs WHERE job_id = ?", ("job1",)).fetchone()
    assert row is not None
    assert row[0] == "anthropic"


def test_record_cost_explicit_provider(migrated_db):
    """record_cost() with provider='gemini' inserts provider='gemini'."""
    _, conn = migrated_db
    record_cost(conn, "job2", "haiku_score", "gemini-2.0-flash", 100, 50, provider="gemini")
    row = conn.execute("SELECT provider FROM scoring_costs WHERE job_id = ?", ("job2",)).fetchone()
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
    cost = record_cost(
        conn, "job4", "sonnet_eval", "claude-sonnet-4-6", 2000, 800, provider="claude_cli"
    )
    assert cost == 0.0


def test_record_cost_rejects_empty_provider(migrated_db):
    """U6 guard: record_cost raises on provider='' to prevent default-leak rows.

    scoring_costs.provider has DEFAULT 'anthropic' (m018), and 'anthropic' is
    in FREE_PROVIDERS post-F2 — so an INSERT that fails to pass a provider
    name would silently disappear from cost rollups.
    """
    _, conn = migrated_db
    with pytest.raises(ValueError, match="provider must be"):
        record_cost(conn, "j1", "purpose", "model", 1, 1, provider="")


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
        """After inserting 3 rows from two paid providers, returns 2 dicts ordered by spend DESC.

        Uses ``openrouter`` and ``cerebras`` as paid markers since
        polish-review F2 (2026-05-26) moved ``anthropic`` into
        ``FREE_PROVIDERS`` (CLI-subscription transport).
        """
        _, conn = migrated_db
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        conn.executemany(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("j1", "haiku_score", "openrouter/m", 100, 50, 0.01, ts, "openrouter"),
                ("j2", "sonnet_eval", "openrouter/m", 200, 100, 0.05, ts, "openrouter"),
                ("j3", "judge", "llama-3.1-70b", 150, 75, 0.02, ts, "cerebras"),
            ],
        )
        conn.commit()
        from job_finder.web.claude_client import get_monthly_provider_breakdown

        result = get_monthly_provider_breakdown(conn)
        assert len(result) == 2
        assert result[0]["provider"] == "openrouter"  # higher spend first
        assert result[0]["calls"] == 2
        assert result[0]["spend"] == pytest.approx(0.06)
        assert result[1]["provider"] == "cerebras"
        assert result[1]["calls"] == 1
        assert result[1]["spend"] == pytest.approx(0.02)

    def test_excludes_free_providers(self, migrated_db):
        """Free/subscription providers must not appear — symmetric with cost_gate semantics.

        F2 (2026-05-26) added ``anthropic`` to ``FREE_PROVIDERS``; this
        test now asserts that anthropic is excluded too.
        """
        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        conn.executemany(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("j1", "p", "openrouter/m", 1, 1, 0.10, ts, "openrouter"),
                ("j2", "p", "x", 1, 1, 0.00, ts, "ollama"),
                ("j3", "p", "x", 1, 1, 0.00, ts, "claude_cli"),
                ("j4", "p", "x", 1, 1, 0.00, ts, "gemini"),
                ("j5", "p", "x", 1, 1, 0.00, ts, "claude_code_cli"),
                # F2 — anthropic is now free too and must be excluded.
                ("j6", "p", "x", 1, 1, 0.00, ts, "anthropic"),
            ],
        )
        conn.commit()
        from job_finder.web.claude_client import get_monthly_provider_breakdown

        result = get_monthly_provider_breakdown(conn)
        assert {r["provider"] for r in result} == {"openrouter"}

    def test_dict_keys(self, migrated_db):
        """Each dict has keys 'provider', 'calls', 'spend' with correct types."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "haiku_score", "openrouter/m", 100, 50, 0.01, ts, "openrouter"),
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
        mock_oneshot.return_value = _make_oneshot_envelope({"score": 75, "summary": "Good"})

        result, cost, _schema_valid = call_claude(
            model="claude-haiku-4-5",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            output_schema=self.SCHEMA,
            conn=conn,
            purpose="test",
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

        result, cost, _schema_valid = call_claude(
            model="claude-haiku-4-5",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            output_schema=self.SCHEMA,
            conn=conn,
            purpose="test",
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
                model="claude-haiku-4-5",
                system="test",
                messages=[{"role": "user", "content": "test"}],
                output_schema=self.SCHEMA,
                conn=conn,
                purpose="test",
            )

    @patch("job_finder.web.claude_client._run_oneshot")
    def test_no_validation_without_schema(self, mock_oneshot, migrated_db):
        """Without output_schema, no validation occurs even with odd data."""
        _, conn = migrated_db
        mock_oneshot.return_value = {
            "result": json.dumps({"score": 999}),
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }

        result, cost, _schema_valid = call_claude(
            model="claude-haiku-4-5",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            conn=conn,
            purpose="test",
        )
        assert result["score"] == 999
        assert mock_oneshot.call_count == 1
