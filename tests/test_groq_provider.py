"""Unit tests for Groq provider adapter (Phase 153)."""

import json
from unittest.mock import Mock, patch

import pytest

from job_finder.web.model_provider import ModelResult
from job_finder.web.providers.groq_provider import GroqProvider


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
            assert result.cost_usd == 0.0
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
    assert result.cost_usd == 0.0
