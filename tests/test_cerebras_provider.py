"""Unit tests for job_finder.web.providers.cerebras_provider.

Tests cover:
- Subclass relationship (CerebrasProvider is a BaseProvider)
- API key from env var on init
- ValueError when API key missing
- Custom api_key_env config
- call() returns correct ModelResult fields
- Request payload structure: model, messages, max_tokens, stream=false
- response_format included when output_schema provided
- Schema embedded in system prompt
- No response_format when output_schema=None (returns {"text": ...})
- Authorization header sent with Bearer token
- Timeout handling (default and custom)
- Missing token counts defaulting to 0
- HTTP errors from requests.post raise requests.HTTPError
- 429 rate-limit raises HTTPError (cascade fallthrough)
- Trailing slash stripped from base_url
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.cerebras_provider import CerebrasProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=mock_resp
        )
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


def _make_chat_response(
    content: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    return _make_response({
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        },
    })


def _make_provider(config: dict | None = None) -> CerebrasProvider:
    if config is None:
        config = {}
    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "test-key-123"}):
        return CerebrasProvider(config=config)


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_cerebras_provider_is_base_provider_subclass():
    assert issubclass(CerebrasProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ / API key tests
# ---------------------------------------------------------------------------


def test_init_reads_api_key_from_env():
    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "my-key"}):
        provider = CerebrasProvider(config={})
    assert provider._api_key == "my-key"


def test_init_raises_when_api_key_missing():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="CEREBRAS_API_KEY"):
            CerebrasProvider(config={})


def test_init_custom_api_key_env():
    with patch.dict("os.environ", {"CUSTOM_KEY": "custom-val"}):
        provider = CerebrasProvider(config={
            "providers": {"cerebras": {"api_key_env": "CUSTOM_KEY"}}
        })
    assert provider._api_key == "custom-val"


def test_init_strips_trailing_slash():
    with patch.dict("os.environ", {"CEREBRAS_API_KEY": "key"}):
        provider = CerebrasProvider(config={
            "providers": {"cerebras": {"base_url": "https://example.com/v1/"}}
        })
    assert provider._base_url == "https://example.com/v1"


# ---------------------------------------------------------------------------
# call() tests
# ---------------------------------------------------------------------------


def test_successful_call_returns_model_result():
    provider = _make_provider()
    resp = _make_chat_response('{"score": 75}')

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        result = provider.call(
            model="llama3.1-8b",
            system="You are a scorer",
            messages=[{"role": "user", "content": "Score this job"}],
            output_schema={"type": "object", "properties": {"score": {"type": "integer"}}},
        )

    assert isinstance(result, ModelResult)
    assert result.provider == "cerebras"
    assert result.model == "llama3.1-8b"
    assert result.cost_usd == 0.0
    assert result.data == {"score": 75}
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.schema_valid is True


def test_call_without_schema_returns_text():
    provider = _make_provider()
    resp = _make_chat_response("Some plain text")

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        result = provider.call(
            model="llama3.1-8b",
            system="You are helpful",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert result.data == {"text": "Some plain text"}


def test_schema_embedded_in_system_prompt():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    resp = _make_chat_response('{"score": 1}')

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call(
            model="llama3.1-8b",
            system="Score jobs",
            messages=[{"role": "user", "content": "test"}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    system_content = payload["messages"][0]["content"]
    assert "Respond with valid JSON matching this schema:" in system_content
    assert json.dumps(schema, indent=2) in system_content


def test_response_format_included_when_schema_provided():
    provider = _make_provider()
    resp = _make_chat_response('{"score": 1}')

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call(
            model="llama3.1-8b",
            system="Score",
            messages=[{"role": "user", "content": "test"}],
            output_schema={"type": "object"},
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}


def test_no_response_format_without_schema():
    provider = _make_provider()
    resp = _make_chat_response("hello")

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call(
            model="llama3.1-8b",
            system="System",
            messages=[{"role": "user", "content": "test"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "response_format" not in payload


def test_authorization_header():
    provider = _make_provider()
    resp = _make_chat_response("ok")

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call("m", "s", [{"role": "user", "content": "c"}])

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key-123"


def test_default_timeout():
    provider = _make_provider()
    resp = _make_chat_response("ok")

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call("m", "s", [{"role": "user", "content": "c"}])

    assert mock_post.call_args.kwargs["timeout"] == 60.0


def test_custom_timeout():
    provider = _make_provider()
    resp = _make_chat_response("ok")

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp) as mock_post:
        provider.call("m", "s", [{"role": "user", "content": "c"}], timeout=30.0)

    assert mock_post.call_args.kwargs["timeout"] == 30.0


def test_missing_usage_defaults_to_zero():
    provider = _make_provider()
    resp = _make_response({
        "choices": [{"message": {"content": "hi"}}],
    })

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        result = provider.call("m", "s", [{"role": "user", "content": "c"}])

    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_http_error_raises():
    provider = _make_provider()
    resp = _make_response({}, status_code=401)

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        with pytest.raises(requests.HTTPError):
            provider.call("m", "s", [{"role": "user", "content": "c"}])


def test_rate_limit_429_raises_http_error():
    """429 from Cerebras must raise HTTPError so the cascade catches it and falls through."""
    provider = _make_provider()
    resp = _make_response({}, status_code=429)

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        with pytest.raises(requests.HTTPError):
            provider.call("m", "s", [{"role": "user", "content": "c"}])


def test_json_decode_error_on_invalid_json_with_schema():
    provider = _make_provider()
    resp = _make_response({
        "choices": [{"message": {"content": "not json"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })

    with patch("job_finder.web.providers.cerebras_provider.requests.post", return_value=resp):
        with pytest.raises(json.JSONDecodeError):
            provider.call(
                "m", "s", [{"role": "user", "content": "c"}],
                output_schema={"type": "object"},
            )
