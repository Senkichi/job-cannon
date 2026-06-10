"""Unit tests for Cerebras provider adapter (Phase 153 + Issue 292)."""

import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from job_finder.web.model_provider import ModelResult, _maybe_record_cost
from job_finder.web.providers.cerebras_provider import (
    _CEREBRAS_PRICING,
    CerebrasProvider,
    _cerebras_cost,
)


def test_cerebras_provider_init_with_key():
    """CerebrasProvider initialises when CEREBRAS_API_KEY is set."""
    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-cerebras-key"}):
        provider = CerebrasProvider(config={})
        assert provider._api_key == "test-cerebras-key"
        assert provider._base_url == "https://api.cerebras.ai/v1"


def test_cerebras_provider_init_no_key_raises():
    """CerebrasProvider raises ValueError (not crashes) when no API key — cascade skips it."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Cerebras API key not set"):
            CerebrasProvider(config={})


def test_cerebras_provider_call_returns_model_result():
    """CerebrasProvider.call() returns a valid ModelResult with correct fields."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"score": 4, "label": "strong_apply"})}}],
        "usage": {"prompt_tokens": 150, "completion_tokens": 60},
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = CerebrasProvider(config={})
            result = provider.call(
                model="llama3.1-8b",
                system="Score this job",
                messages=[{"role": "user", "content": "Job description here"}],
            )

            # Correct endpoint and payload shape
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["model"] == "llama3.1-8b"
            assert call_kwargs["json"]["temperature"] == 0
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"
            url = mock_post.call_args[0][0]
            assert url == "https://api.cerebras.ai/v1/chat/completions"

            # System prompt injected as first message
            sent_messages = call_kwargs["json"]["messages"]
            assert sent_messages[0] == {"role": "system", "content": "Score this job"}

            # ModelResult contract
            assert isinstance(result, ModelResult)
            assert result.provider == "cerebras"
            # cost_usd must be > 0 for a known model with real token counts
            expected_cost = _cerebras_cost("llama3.1-8b", 150, 60)
            assert abs(result.cost_usd - expected_cost) < 1e-9
            assert result.cost_usd > 0.0
            assert result.input_tokens == 150
            assert result.output_tokens == 60
            assert result.schema_valid is True
            assert result.data == {"score": 4, "label": "strong_apply"}


def test_cerebras_provider_call_with_output_schema_adds_response_format():
    """output_schema triggers response_format=json_object in the request payload."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"result": "ok"})}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    mock_response.raise_for_status = Mock()

    schema = {"type": "object", "properties": {"result": {"type": "string"}}}

    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = CerebrasProvider(config={})
            result = provider.call(
                model="llama-3.3-70b",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
                output_schema=schema,
            )

            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["response_format"] == {"type": "json_object"}
            assert result.provider == "cerebras"


def test_cerebras_provider_call_without_output_schema_omits_response_format():
    """When output_schema is None, response_format is not sent."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = CerebrasProvider(config={})
            provider.call(
                model="llama3.1-8b",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
                output_schema=None,
            )

            call_kwargs = mock_post.call_args[1]
            assert "response_format" not in call_kwargs["json"]


def test_cerebras_provider_call_missing_usage_defaults_to_zero():
    """Missing usage block defaults input_tokens and output_tokens to 0."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"ok": True})}}],
        # no "usage" key
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response):
            provider = CerebrasProvider(config={})
            result = provider.call(
                model="llama3.1-8b",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
            )

    assert result.input_tokens == 0
    assert result.output_tokens == 0
    # Zero tokens → zero cost (no rounding issues)
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Issue 292 — pricing table + cost computation
# ---------------------------------------------------------------------------


def test_cerebras_cost_known_model_8b():
    """llama3.1-8b: 1M in + 1M out = $0.10 + $0.10 = $0.20."""
    cost = _cerebras_cost("llama3.1-8b", 1_000_000, 1_000_000)
    assert abs(cost - 0.20) < 1e-9


def test_cerebras_cost_known_model_70b():
    """llama-3.3-70b: 1M in + 1M out = $0.85 + $1.20 = $2.05."""
    cost = _cerebras_cost("llama-3.3-70b", 1_000_000, 1_000_000)
    assert abs(cost - 2.05) < 1e-9


def test_cerebras_cost_unknown_model_uses_most_expensive_fallback():
    """Unknown model falls back to the most expensive entry in _CEREBRAS_PRICING."""
    most_expensive = max(_CEREBRAS_PRICING.values(), key=lambda p: p["input"] + p["output"])
    cost_unknown = _cerebras_cost("unknown-model-xyz", 1_000_000, 1_000_000)
    expected = most_expensive["input"] + most_expensive["output"]
    assert abs(cost_unknown - expected) < 1e-9


def test_cerebras_cost_partial_tokens():
    """200k in + 50k out for llama-3.3-70b."""
    # 200k / 1M * 0.85 + 50k / 1M * 1.20 = 0.17 + 0.06 = 0.23
    cost = _cerebras_cost("llama-3.3-70b", 200_000, 50_000)
    assert abs(cost - 0.23) < 1e-9


def test_cerebras_result_flows_through_maybe_record_cost(tmp_path):
    """A Cerebras ModelResult with real cost_usd lands in scoring_costs with cost_usd > 0."""
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "cerebras_cost_test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    result = ModelResult(
        data={"score": 3},
        cost_usd=_cerebras_cost("llama-3.3-70b", 300_000, 100_000),
        input_tokens=300_000,
        output_tokens=100_000,
        model="llama-3.3-70b",
        provider="cerebras",
        schema_valid=True,
    )

    _maybe_record_cost(result, conn, job_id="test-job-2", purpose="score_job")

    rows = conn.execute("SELECT cost_usd, provider FROM scoring_costs").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["provider"] == "cerebras"
    assert rows[0]["cost_usd"] > 0.0
    # 300k / 1M * 0.85 + 100k / 1M * 1.20 = 0.255 + 0.12 = 0.375
    assert abs(rows[0]["cost_usd"] - 0.375) < 1e-6


def test_cerebras_cost_included_in_cost_gate_sum(tmp_path):
    """cost_gate counts cerebras spend toward the daily budget (cerebras not in FREE_PROVIDERS)."""
    from datetime import UTC, datetime

    from job_finder.web.claude_client import cost_gate
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "cerebras_gate_test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
    # Insert a cerebras row with $5.00 spend — above a $1.00 cap
    conn.execute(
        "INSERT INTO scoring_costs "
        "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("j2", "score_job", "llama-3.3-70b", 1000, 1000, 5.00, ts, "cerebras"),
    )
    conn.commit()

    config = {"scoring": {"daily_budget_usd": 1.00}}
    # $5.00 cerebras spend > $1.00 cap → gate must block
    assert cost_gate(conn, config, model_tier="score") is False
    conn.close()
