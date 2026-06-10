"""Unit tests for OpenRouter provider adapter (Phase 36 + Issue 292)."""

import json
from unittest.mock import Mock, patch

import pytest

from job_finder.web.model_provider import ModelResult
from job_finder.web.providers.openrouter_provider import (
    _OPENROUTER_PRICING,
    OpenRouterProvider,
    _openrouter_cost,
)


def test_openrouter_provider_init():
    """Test OpenRouterProvider initialization with API key."""
    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        provider = OpenRouterProvider(config={})
        assert provider._api_key == "test-key"
        assert provider._base_url == "https://openrouter.ai/api/v1"


def test_openrouter_provider_init_no_key():
    """Test OpenRouterProvider initialization fails without API key."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="OpenRouter API key not set"):
            OpenRouterProvider(config={})


def test_openrouter_provider_call():
    """Test OpenRouterProvider.call() with mocked HTTP response."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"winner": "A", "rationale": "Test", "confidence": 0.9})
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = OpenRouterProvider(config={})
            result = provider.call(
                model="deepseek/deepseek-v4-flash:free",
                system="Test system",
                messages=[{"role": "user", "content": "Test"}],
            )

            # Verify HTTP call was made correctly
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "https://openrouter.ai/api/v1/chat/completions" in call_args[0][0]
            assert call_args[1]["json"]["model"] == "deepseek/deepseek-v4-flash:free"
            assert call_args[1]["json"]["temperature"] == 0
            assert call_args[1]["headers"]["Authorization"] == "Bearer test-key"

            # Verify ModelResult structure
            assert isinstance(result, ModelResult)
            assert result.provider == "openrouter"
            # :free model → $0 (no usage.cost field in response)
            assert result.cost_usd == 0.0
            assert result.input_tokens == 100
            assert result.output_tokens == 50
            assert result.schema_valid is True
            assert result.data == {"winner": "A", "rationale": "Test", "confidence": 0.9}


def test_openrouter_provider_call_with_output_schema():
    """Test OpenRouterProvider.call() with output_schema parameter."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"test": "value"})}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 25},
    }
    mock_response.raise_for_status = Mock()

    output_schema = {"type": "object", "properties": {"test": {"type": "string"}}}

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response) as mock_post:
            provider = OpenRouterProvider(config={})
            result = provider.call(
                model="deepseek/deepseek-v4-flash:free",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
                output_schema=output_schema,
            )

            # Verify response_format was added
            call_args = mock_post.call_args
            assert "response_format" in call_args[1]["json"]
            assert call_args[1]["json"]["response_format"]["type"] == "json_object"
            assert call_args[1]["json"]["response_format"]["json_schema"] == output_schema


# ---------------------------------------------------------------------------
# Issue 292 — pricing table + cost computation
# ---------------------------------------------------------------------------


def test_openrouter_cost_free_model_is_zero():
    """:free variant always $0."""
    cost = _openrouter_cost("deepseek/deepseek-v4-flash:free", 1_000_000, 1_000_000)
    assert cost == 0.0


def test_openrouter_cost_paid_model():
    """deepseek/deepseek-v4-flash: 1M in + 1M out = $0.0983 + $0.1966 = $0.2949."""
    cost = _openrouter_cost("deepseek/deepseek-v4-flash", 1_000_000, 1_000_000)
    assert abs(cost - 0.2949) < 1e-9


def test_openrouter_cost_unknown_model_uses_most_expensive_fallback():
    """Unknown model falls back to the most expensive entry in _OPENROUTER_PRICING."""
    most_expensive = max(_OPENROUTER_PRICING.values(), key=lambda p: p["input"] + p["output"])
    cost_unknown = _openrouter_cost("unknown/model:paid", 1_000_000, 1_000_000)
    expected = most_expensive["input"] + most_expensive["output"]
    assert abs(cost_unknown - expected) < 1e-9


def test_openrouter_prefers_api_reported_cost():
    """When usage.cost is present, the adapter uses it directly."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cost": 0.042,  # API-reported cost
        },
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response):
            provider = OpenRouterProvider(config={})
            result = provider.call(
                model="deepseek/deepseek-v4-flash",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
            )

    assert abs(result.cost_usd - 0.042) < 1e-9


def test_openrouter_falls_back_to_static_table_when_no_api_cost():
    """When usage.cost is absent, the static table fallback is used."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            # no "cost" key
        },
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response):
            provider = OpenRouterProvider(config={})
            result = provider.call(
                model="deepseek/deepseek-v4-flash",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
            )

    # Static table: $0.0983 + $0.1966 = $0.2949
    assert abs(result.cost_usd - 0.2949) < 1e-9


def test_openrouter_api_cost_none_falls_back_to_static_table():
    """When usage.cost is explicitly null/None, the static table fallback is used."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "cost": None,  # API returned null
        },
    }
    mock_response.raise_for_status = Mock()

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
        with patch("requests.post", return_value=mock_response):
            provider = OpenRouterProvider(config={})
            result = provider.call(
                model="deepseek/deepseek-v4-flash",
                system="Test",
                messages=[{"role": "user", "content": "Test"}],
            )

    # cost=None → fallback to static table
    assert abs(result.cost_usd - 0.2949) < 1e-9
