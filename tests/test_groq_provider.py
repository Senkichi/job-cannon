"""Unit tests for Groq provider adapter (Phase 153 + Issue 292)."""

import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from job_finder.web.model_provider import ModelResult, _maybe_record_cost
from job_finder.web.providers.groq_provider import (
    _GROQ_PRICING,
    GroqProvider,
    _groq_cost,
)


def test_groq_provider_init_with_key():
    """GroqProvider initialises when GROQ_API_KEY is set."""
    with patch.dict("os.environ", {"GROQ_API_KEY": "test-groq-key"}):
        provider = GroqProvider(config={})
        assert provider._api_key == "test-groq-key"
        assert provider._base_url == "https://api.groq.com/openai/v1"


def test_groq_provider_init_no_key_raises():
    """GroqProvider raises ValueError (not crashes) when no API key — cascade skips it."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Groq API key not set"):
            GroqProvider(config={})


def test_groq_provider_call_returns_model_result():
    """GroqProvider.call() returns a valid ModelResult with correct fields."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"score": 3, "label": "apply"})}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80},
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = GroqProvider(config={})
            result = provider.call(
                model="llama-3.1-8b-instant",
                system="Score this job",
                messages=[{"role": "user", "content": "Job description here"}],
            )

            # Correct endpoint and payload shape
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["model"] == "llama-3.1-8b-instant"
            assert call_kwargs["json"]["temperature"] == 0
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"
            url = mock_post.call_args[0][0]
            assert url == "https://api.groq.com/openai/v1/chat/completions"

            # System prompt injected as first message
            sent_messages = call_kwargs["json"]["messages"]
            assert sent_messages[0] == {"role": "system", "content": "Score this job"}

            # ModelResult contract
            assert isinstance(result, ModelResult)
            assert result.provider == "groq"
            # cost_usd must be > 0 for a known model with real token counts
            expected_cost = _groq_cost("llama-3.1-8b-instant", 200, 80)
            assert abs(result.cost_usd - expected_cost) < 1e-9
            assert result.cost_usd > 0.0
            assert result.input_tokens == 200
            assert result.output_tokens == 80
            assert result.schema_valid is True
            assert result.data == {"score": 3, "label": "apply"}


def test_groq_provider_call_with_output_schema_adds_response_format():
    """output_schema triggers response_format=json_object in the request payload."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"result": "ok"})}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    mock_response.raise_for_status = Mock()

    schema = {"type": "object", "properties": {"result": {"type": "string"}}}

    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = GroqProvider(config={})
            result = provider.call(
                model="llama-3.3-70b-versatile",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
                output_schema=schema,
            )

            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["response_format"] == {"type": "json_object"}
            assert result.provider == "groq"


def test_groq_provider_call_without_output_schema_omits_response_format():
    """When output_schema is None, response_format is not sent."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = GroqProvider(config={})
            provider.call(
                model="llama-3.1-8b-instant",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
                output_schema=None,
            )

            call_kwargs = mock_post.call_args[1]
            assert "response_format" not in call_kwargs["json"]


def test_groq_provider_call_missing_usage_defaults_to_zero():
    """Missing usage block defaults input_tokens and output_tokens to 0."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"ok": True})}}],
        # no "usage" key
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response):
            provider = GroqProvider(config={})
            result = provider.call(
                model="llama-3.1-8b-instant",
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


def test_groq_cost_known_model_8b():
    """llama-3.1-8b-instant: 1M in + 1M out = $0.05 + $0.08 = $0.13."""
    cost = _groq_cost("llama-3.1-8b-instant", 1_000_000, 1_000_000)
    assert abs(cost - 0.13) < 1e-9


def test_groq_cost_known_model_70b():
    """llama-3.3-70b-versatile: 1M in + 1M out = $0.59 + $0.79 = $1.38."""
    cost = _groq_cost("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert abs(cost - 1.38) < 1e-9


def test_groq_cost_unknown_model_uses_most_expensive_fallback():
    """Unknown model falls back to the most expensive entry in _GROQ_PRICING."""
    most_expensive = max(_GROQ_PRICING.values(), key=lambda p: p["input"] + p["output"])
    cost_unknown = _groq_cost("unknown-model-xyz", 1_000_000, 1_000_000)
    expected = most_expensive["input"] + most_expensive["output"]
    assert abs(cost_unknown - expected) < 1e-9


def test_groq_cost_partial_tokens():
    """500k in + 100k out for llama-3.1-8b-instant."""
    # 500k / 1M * 0.05 + 100k / 1M * 0.08 = 0.025 + 0.008 = 0.033
    cost = _groq_cost("llama-3.1-8b-instant", 500_000, 100_000)
    assert abs(cost - 0.033) < 1e-9


def test_groq_result_flows_through_maybe_record_cost(tmp_path):
    """A Groq ModelResult with real cost_usd lands in scoring_costs with cost_usd > 0."""
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "groq_cost_test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    result = ModelResult(
        data={"score": 4},
        cost_usd=_groq_cost("llama-3.3-70b-versatile", 500_000, 200_000),
        input_tokens=500_000,
        output_tokens=200_000,
        model="llama-3.3-70b-versatile",
        provider="groq",
        schema_valid=True,
    )

    _maybe_record_cost(result, conn, job_id="test-job-1", purpose="score_job")

    rows = conn.execute("SELECT cost_usd, provider FROM scoring_costs").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["provider"] == "groq"
    assert rows[0]["cost_usd"] > 0.0
    # 500k / 1M * 0.59 + 200k / 1M * 0.79 = 0.295 + 0.158 = 0.453
    assert abs(rows[0]["cost_usd"] - 0.453) < 1e-6


def test_groq_cost_included_in_cost_gate_sum(tmp_path):
    """cost_gate counts groq spend toward the daily budget (groq not in FREE_PROVIDERS)."""
    from job_finder.json_utils import utc_now_iso
    from job_finder.web.claude_client import cost_gate
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "groq_gate_test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)

    # Anchor on the real "now" (naive UTC ISO, matching production storage) so
    # the row lands inside cost_gate's local-day window regardless of the run
    # machine's timezone. A hardcoded "...T12:00:00Z" on now(UTC).date() falls
    # outside the window in the evening Pacific (UTC already on the next day).
    ts = utc_now_iso()
    # Insert a groq row with $5.00 spend — above a $1.00 cap
    conn.execute(
        "INSERT INTO scoring_costs "
        "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("j1", "score_job", "llama-3.3-70b-versatile", 1000, 1000, 5.00, ts, "groq"),
    )
    conn.commit()

    config = {"scoring": {"daily_budget_usd": 1.00}}
    # $5.00 groq spend > $1.00 cap → gate must block
    assert cost_gate(conn, config, model_tier="score") is False
    conn.close()
