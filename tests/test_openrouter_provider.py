"""Unit tests for OpenRouter provider adapter (Phase 36)."""

import json
from unittest.mock import Mock, patch

import pytest

from job_finder.web.model_provider import ModelResult
from job_finder.web.providers.openrouter_provider import OpenRouterProvider


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
