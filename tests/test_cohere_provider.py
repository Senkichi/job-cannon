"""Unit tests for job_finder.web.providers.cohere_provider.

Tests cover:
- Subclass relationship (CohereProvider is a BaseProvider)
- API key from env var on init
- ValueError when API key missing
- Custom api_key_env config
- call() returns correct ModelResult fields
- Request payload structure: model, messages, max_tokens, stream=false
- JSON schema via response_format when output_schema provided
- No response_format when output_schema=None (returns {"text": ...})
- Messages format (system first, then user messages)
- Authorization header sent with Bearer token
- Timeout handling (default and custom)
- Missing token counts defaulting to 0
- HTTP errors from requests.post raise requests.HTTPError
- Trailing slash stripped from base_url
- Cohere v2 content block parsing
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.cohere_provider import CohereProvider


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
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Create a mock Cohere v2 /v2/chat response."""
    return _make_response({
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
        },
        "finish_reason": "COMPLETE",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    })


def _make_provider(config: dict | None = None) -> CohereProvider:
    """Create CohereProvider with mocked env var."""
    if config is None:
        config = {}
    with patch.dict("os.environ", {"CO_API_KEY": "test-key-123"}):
        return CohereProvider(config=config)


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_cohere_provider_is_base_provider_subclass():
    assert issubclass(CohereProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ / API key tests
# ---------------------------------------------------------------------------


def test_init_reads_api_key_from_env():
    with patch.dict("os.environ", {"CO_API_KEY": "my-key"}):
        provider = CohereProvider(config={})
    assert provider._api_key == "my-key"


def test_init_raises_when_api_key_missing():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="CO_API_KEY"):
            CohereProvider(config={})


def test_init_custom_api_key_env():
    config = {"providers": {"cohere": {"api_key_env": "MY_COHERE_KEY"}}}
    with patch.dict("os.environ", {"MY_COHERE_KEY": "custom-key"}):
        provider = CohereProvider(config=config)
    assert provider._api_key == "custom-key"


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
            model="command-a-03-2025",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
            output_schema={"type": "object", "properties": {"score": {"type": "integer"}}},
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.provider == "cohere"
    assert result.schema_valid is True
    assert result.model == "command-a-03-2025"


def test_call_returns_text_dict_without_schema():
    provider = _make_provider()

    with patch(
        "requests.post",
        return_value=_make_chat_response("Hello, world!"),
    ):
        result = provider.call(
            model="command-a-03-2025",
            system="You are helpful.",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=None,
        )

    assert result.data == {"text": "Hello, world!"}


# ---------------------------------------------------------------------------
# Request payload tests
# ---------------------------------------------------------------------------


def test_call_sends_json_schema_in_response_format():
    provider = _make_provider()
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}

    with patch("requests.post", return_value=_make_chat_response(json.dumps({"score": 80}))) as mock_post:
        provider.call(
            model="command-a-03-2025",
            system="Rate this.",
            messages=[{"role": "user", "content": "Job desc."}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["response_format"] == {
        "type": "json_object",
        "json_schema": schema,
    }


def test_call_no_response_format_without_schema():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("text")) as mock_post:
        provider.call(
            model="command-a-03-2025",
            system="System.",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=None,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "response_format" not in payload


def test_call_request_has_stream_false():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-a-03-2025",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["stream"] is False


def test_call_request_has_correct_model():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-r7b-12-2024",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "command-r7b-12-2024"


def test_call_request_has_max_tokens():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-a-03-2025",
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
            model="command-a-03-2025",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key-123"


# ---------------------------------------------------------------------------
# Messages format tests
# ---------------------------------------------------------------------------


def test_call_messages_format():
    provider = _make_provider()
    user_messages = [
        {"role": "user", "content": "First."},
        {"role": "assistant", "content": "Response."},
        {"role": "user", "content": "Follow-up."},
    ]

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-a-03-2025",
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
            model="command-a-03-2025",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=None,
        )

    assert mock_post.call_args.kwargs["timeout"] == 120.0


def test_call_uses_custom_timeout():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-a-03-2025",
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
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps({"r": 1})}],
        },
        "finish_reason": "COMPLETE",
    })

    with patch("requests.post", return_value=mock_resp):
        result = provider.call(
            model="command-a-03-2025",
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
                model="command-a-03-2025",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash():
    config = {"providers": {"cohere": {"base_url": "https://api.cohere.com/"}}}
    with patch.dict("os.environ", {"CO_API_KEY": "key"}):
        provider = CohereProvider(config=config)
    assert provider._base_url == "https://api.cohere.com"


def test_call_posts_to_correct_endpoint():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response("ok")) as mock_post:
        provider.call(
            model="command-a-03-2025",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args[0][0]
    assert called_url == "https://api.cohere.com/v2/chat"
