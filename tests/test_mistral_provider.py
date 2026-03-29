"""Unit tests for job_finder.web.providers.mistral_provider.

Tests cover:
- Subclass relationship (MistralProvider is a BaseProvider)
- API key from env var on init
- ValueError when API key missing
- Custom api_key_env config
- call() returns correct ModelResult fields
- Request payload structure: model, messages, max_tokens
- Native JSON schema via response_format when output_schema provided
- No response_format when output_schema=None (returns {"text": ...})
- Messages format (system first, then user messages)
- Authorization header sent with Bearer token
- Timeout handling (default and custom)
- Missing token counts defaulting to 0
- HTTP errors from requests.post raise requests.HTTPError
- Trailing slash stripped from base_url
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.mistral_provider import MistralProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock requests.Response with json() and raise_for_status()."""
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
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    """Create a mock /v1/chat/completions response (OpenAI format)."""
    return _make_response({
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


def _make_provider(config: dict | None = None) -> MistralProvider:
    """Create MistralProvider with mocked env var."""
    if config is None:
        config = {}
    with patch.dict("os.environ", {"MISTRAL_API_KEY": "test-key-123"}):
        return MistralProvider(config=config)


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_mistral_provider_is_base_provider_subclass():
    assert issubclass(MistralProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ / API key tests
# ---------------------------------------------------------------------------


def test_init_reads_api_key_from_env():
    with patch.dict("os.environ", {"MISTRAL_API_KEY": "my-key"}):
        provider = MistralProvider(config={})
    assert provider._api_key == "my-key"


def test_init_raises_when_api_key_missing():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
            MistralProvider(config={})


def test_init_custom_api_key_env():
    config = {"providers": {"mistral": {"api_key_env": "MY_MISTRAL_KEY"}}}
    with patch.dict("os.environ", {"MY_MISTRAL_KEY": "custom-key"}):
        provider = MistralProvider(config=config)
    assert provider._api_key == "custom-key"


def test_init_custom_api_key_env_missing():
    config = {"providers": {"mistral": {"api_key_env": "MY_MISTRAL_KEY"}}}
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="MY_MISTRAL_KEY"):
            MistralProvider(config=config)


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_returns_model_result_with_schema():
    provider = _make_provider()

    with patch(
        "requests.post",
        return_value=_make_chat_response(json.dumps({"score": 75})),
    ):
        result = provider.call(
            model="mistral-small-latest",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
            output_schema={"type": "object", "properties": {"score": {"type": "integer"}}},
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.provider == "mistral"
    assert result.schema_valid is True
    assert result.model == "mistral-small-latest"


def test_call_returns_text_dict_without_schema():
    provider = _make_provider()

    with patch(
        "requests.post",
        return_value=_make_chat_response("Hello, world!"),
    ):
        result = provider.call(
            model="mistral-small-latest",
            system="You are helpful.",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=None,
        )

    assert result.data == {"text": "Hello, world!"}


# ---------------------------------------------------------------------------
# Request payload tests
# ---------------------------------------------------------------------------


def test_call_sends_native_json_schema():
    """When output_schema provided, payload should use response_format with json_schema."""
    provider = _make_provider()
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}

    with patch("requests.post", return_value=_make_chat_response(json.dumps({"score": 80}))) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="Rate this.",
            messages=[{"role": "user", "content": "Job desc."}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "response"
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert rf["json_schema"]["schema"]["required"] == ["score"]
    assert rf["json_schema"]["schema"]["properties"] == {"score": {"type": "integer"}}


def test_call_no_response_format_without_schema():
    """No response_format key when output_schema=None."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("text")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System.",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=None,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "response_format" not in payload


def test_call_request_payload_has_correct_model():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response(json.dumps({"r": 1}))) as mock_post:
        provider.call(
            model="mistral-large-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "mistral-large-latest"


def test_call_request_payload_has_max_tokens():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=2048,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# Auth header tests
# ---------------------------------------------------------------------------


def test_call_sends_auth_header():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key-123"


# ---------------------------------------------------------------------------
# Messages format tests
# ---------------------------------------------------------------------------


def test_call_messages_format():
    """Messages list should start with system role, followed by user messages."""
    provider = _make_provider()
    user_messages = [
        {"role": "user", "content": "First."},
        {"role": "assistant", "content": "Response."},
        {"role": "user", "content": "Follow-up."},
    ]

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System prompt.",
            messages=user_messages,
        )

    payload = mock_post.call_args.kwargs["json"]
    msgs = payload["messages"]
    assert msgs[0] == {"role": "system", "content": "System prompt."}
    assert msgs[1:] == user_messages


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


def test_call_uses_default_timeout():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=None,
        )

    assert mock_post.call_args.kwargs["timeout"] == 120.0


def test_call_uses_custom_timeout():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=30.0,
        )

    assert mock_post.call_args.kwargs["timeout"] == 30.0


# ---------------------------------------------------------------------------
# Token count edge cases
# ---------------------------------------------------------------------------


def test_call_handles_missing_usage():
    provider = _make_provider()
    mock_resp = _make_response({
        "choices": [{"message": {"role": "assistant", "content": json.dumps({"r": 1})}}],
    })

    with patch("requests.post", return_value=mock_resp):
        result = provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_call_raises_on_http_error():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_response({}, status_code=500)):
        with pytest.raises(requests.HTTPError):
            provider.call(
                model="mistral-small-latest",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash():
    config = {"providers": {"mistral": {"base_url": "https://api.mistral.ai/"}}}
    with patch.dict("os.environ", {"MISTRAL_API_KEY": "key"}):
        provider = MistralProvider(config=config)
    assert provider._base_url == "https://api.mistral.ai"


def test_call_posts_to_correct_endpoint():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="mistral-small-latest",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args[0][0]
    assert called_url == "https://api.mistral.ai/v1/chat/completions"
