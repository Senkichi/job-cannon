"""Unit tests for job_finder.web.providers.gemini_provider.

Tests cover:
- Subclass relationship (GeminiProvider is a BaseProvider)
- Client injection (bypasses env var check)
- Client creation from env var
- Init raises ValueError without GEMINI_API_KEY
- call() returns correct ModelResult fields
- Structured output uses response_json_schema + response_mime_type
- Non-structured output omits JSON config fields
- system_instruction is forwarded via GenerateContentConfig
- Retry on 429 (single retry, then success)
- Raises ClientError after double 429
- Raises immediately on non-429 ClientError
- None usage_metadata fields default to 0
- retry_sleep_seconds configurable (default 15.0)
"""

import os
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("google.generativeai")
from google.genai import errors as genai_errors

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.gemini_provider import GeminiProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(text: str, prompt_tokens=100, candidates_tokens=50):
    """Build a mock generate_content response."""
    response = MagicMock()
    response.text = text
    response.usage_metadata.prompt_token_count = prompt_tokens
    response.usage_metadata.candidates_token_count = candidates_tokens
    return response


def _make_client(response=None):
    """Build a mock genai client with generate_content set up."""
    client = MagicMock()
    if response is not None:
        client.models.generate_content.return_value = response
    return client


def _make_429():
    """Create a real ClientError with code=429."""
    return genai_errors.ClientError(429, {}, None)


def _make_500():
    """Create a real ClientError with code=500."""
    return genai_errors.ClientError(500, {}, None)


# ---------------------------------------------------------------------------
# Subclass / interface
# ---------------------------------------------------------------------------


def test_gemini_provider_is_base_provider_subclass():
    assert issubclass(GeminiProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------


def test_init_with_injected_client():
    """GeminiProvider with injected client succeeds without GEMINI_API_KEY."""
    mock_client = MagicMock()
    # Remove GEMINI_API_KEY from env if present to confirm it's not needed
    env_without_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        provider = GeminiProvider(config={}, client=mock_client)
    assert provider._client is mock_client


def test_init_from_env_creates_client():
    """GeminiProvider reads GEMINI_API_KEY from env and creates genai.Client."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-api-key-123"}, clear=False):
        with patch("google.genai.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            provider = GeminiProvider(config={})
    mock_client_cls.assert_called_once_with(api_key="test-api-key-123")


def test_init_raises_without_api_key():
    """GeminiProvider raises ValueError when GEMINI_API_KEY is absent and no client injected."""
    env_without_key = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiProvider(config={})


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_returns_model_result():
    """call() returns ModelResult with correct fields."""
    response = _make_response('{"score": 75}', prompt_tokens=100, candidates_tokens=50)
    client = _make_client(response)
    provider = GeminiProvider(config={}, client=client)

    result = provider.call(
        model="gemini-2.0-flash",
        system="You are a scorer.",
        messages=[{"role": "user", "content": "Score this."}],
        output_schema={"type": "object", "properties": {"score": {"type": "integer"}}},
    )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.provider == "gemini"
    assert result.model == "gemini-2.0-flash"
    assert result.schema_valid is True


def test_call_structured_output_uses_response_json_schema():
    """With output_schema provided, generate_content config includes response_json_schema."""
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    response = _make_response('{"score": 80}')
    client = _make_client(response)
    provider = GeminiProvider(config={}, client=client)

    provider.call(
        model="gemini-2.0-flash",
        system="System",
        messages=[{"role": "user", "content": "Hi"}],
        output_schema=schema,
    )

    call_kwargs = client.models.generate_content.call_args.kwargs
    config_obj = call_kwargs["config"]
    assert config_obj.response_mime_type == "application/json"
    assert config_obj.response_json_schema == schema


def test_call_without_schema_no_json_mode():
    """Without output_schema, config does NOT include response_mime_type or response_json_schema."""
    response = _make_response("plain text response")
    client = _make_client(response)
    provider = GeminiProvider(config={}, client=client)

    result = provider.call(
        model="gemini-2.0-flash",
        system="System",
        messages=[{"role": "user", "content": "Hi"}],
        output_schema=None,
    )

    call_kwargs = client.models.generate_content.call_args.kwargs
    config_obj = call_kwargs["config"]
    assert not hasattr(config_obj, "response_mime_type") or config_obj.response_mime_type is None
    assert (
        not hasattr(config_obj, "response_json_schema") or config_obj.response_json_schema is None
    )
    assert result.data == {"text": "plain text response"}


def test_call_uses_system_instruction():
    """generate_content config includes system_instruction matching the system parameter."""
    response = _make_response('{"ok": true}')
    client = _make_client(response)
    provider = GeminiProvider(config={}, client=client)

    provider.call(
        model="gemini-2.0-flash",
        system="My system prompt",
        messages=[{"role": "user", "content": "Hi"}],
        output_schema={"type": "object"},
    )

    call_kwargs = client.models.generate_content.call_args.kwargs
    config_obj = call_kwargs["config"]
    assert config_obj.system_instruction == "My system prompt"


# ---------------------------------------------------------------------------
# Retry / error propagation tests
# ---------------------------------------------------------------------------


def test_call_retries_on_429():
    """On first 429, call() sleeps and retries; second call succeeds."""
    response = _make_response('{"score": 70}')
    client = MagicMock()
    client.models.generate_content.side_effect = [_make_429(), response]
    provider = GeminiProvider(config={}, client=client)

    with patch("time.sleep") as mock_sleep:
        result = provider.call(
            model="gemini-2.0-flash",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )

    assert isinstance(result, ModelResult)
    assert client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once()


def test_call_raises_after_double_429():
    """If both attempts raise 429, call() raises ClientError."""
    client = MagicMock()
    client.models.generate_content.side_effect = [_make_429(), _make_429()]
    provider = GeminiProvider(config={}, client=client)

    with patch("time.sleep"), pytest.raises(genai_errors.ClientError) as exc_info:
        provider.call(
            model="gemini-2.0-flash",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )

    assert exc_info.value.code == 429
    assert client.models.generate_content.call_count == 2


def test_call_raises_on_non_429_error():
    """Non-429 ClientError raises immediately without retry."""
    client = MagicMock()
    client.models.generate_content.side_effect = _make_500()
    provider = GeminiProvider(config={}, client=client)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.ClientError) as exc_info:
            provider.call(
                model="gemini-2.0-flash",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
                output_schema={"type": "object"},
            )

    assert exc_info.value.code == 500
    assert client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


def test_call_handles_none_usage_metadata():
    """When usage_metadata token counts are None, ModelResult has 0 for both."""
    response = _make_response('{"result": "ok"}', prompt_tokens=None, candidates_tokens=None)
    client = _make_client(response)
    provider = GeminiProvider(config={}, client=client)

    result = provider.call(
        model="gemini-2.0-flash",
        system="System",
        messages=[{"role": "user", "content": "Hi"}],
        output_schema={"type": "object"},
    )

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_retry_sleep_configurable():
    """retry_sleep_seconds reads from config with default 15.0."""
    client_default = MagicMock()
    client_default.models.generate_content.side_effect = [_make_429(), _make_response('{"ok": 1}')]
    provider_default = GeminiProvider(config={}, client=client_default)

    with patch("time.sleep") as mock_sleep:
        provider_default.call(
            model="gemini-2.0-flash",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )
    mock_sleep.assert_called_once_with(15.0)

    # Custom sleep
    client_custom = MagicMock()
    client_custom.models.generate_content.side_effect = [_make_429(), _make_response('{"ok": 1}')]
    provider_custom = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 5.0}}},
        client=client_custom,
    )

    with patch("time.sleep") as mock_sleep_custom:
        provider_custom.call(
            model="gemini-2.0-flash",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )
    mock_sleep_custom.assert_called_once_with(5.0)
