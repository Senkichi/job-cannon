"""Unit tests for job_finder.web.providers.ollm_provider.

Tests cover:
- Subclass relationship (OllmProvider is a BaseProvider)
- Health check on init via GET /v1/models
- RuntimeError on connection failure/timeout/HTTP error
- Custom base_url via config
- call() returns correct ModelResult fields
- Request payload structure: stream=false, model, max_tokens
- Schema embedded in system prompt when output_schema provided
- No schema appended when output_schema=None
- Messages format (system first, then user messages)
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
from job_finder.web.providers.ollm_provider import OllmProvider


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
    content_dict: dict,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    """Create a mock /v1/chat/completions response (OpenAI format)."""
    return _make_response({
        "choices": [{"message": {"role": "assistant", "content": json.dumps(content_dict)}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


def _make_provider(config: dict | None = None) -> OllmProvider:
    """Create OllmProvider with mocked health check (always succeeds)."""
    if config is None:
        config = {}
    with patch("requests.get", return_value=_make_response({"data": [{"id": "llama3-8B-chat"}]})):
        return OllmProvider(config=config)


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_ollm_provider_is_base_provider_subclass():
    assert issubclass(OllmProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ / health check tests
# ---------------------------------------------------------------------------


def test_init_calls_health_check():
    """Health check should hit http://localhost:8000/v1/models with timeout=5.0."""
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllmProvider(config={})

    mock_get.assert_called_once_with(
        "http://localhost:8000/v1/models",
        timeout=5.0,
    )


def test_init_raises_on_connection_error():
    with patch("requests.get", side_effect=requests.ConnectionError("Connection refused")):
        with pytest.raises(RuntimeError, match="oLLM server unreachable"):
            OllmProvider(config={})


def test_init_raises_on_timeout():
    with patch("requests.get", side_effect=requests.Timeout("Timed out")):
        with pytest.raises(RuntimeError, match="oLLM server unreachable"):
            OllmProvider(config={})


def test_init_raises_on_http_error():
    with patch("requests.get", return_value=_make_response({}, status_code=500)):
        with pytest.raises(RuntimeError):
            OllmProvider(config={})


def test_init_custom_base_url():
    """Custom base_url from config should be used for health check."""
    config = {"providers": {"ollm": {"base_url": "http://myhost:9999"}}}
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllmProvider(config=config)

    mock_get.assert_called_once_with(
        "http://myhost:9999/v1/models",
        timeout=5.0,
    )


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_returns_model_result():
    provider = _make_provider()

    with patch(
        "requests.post",
        return_value=_make_chat_response({"score": 75}, prompt_tokens=100, completion_tokens=50),
    ):
        result = provider.call(
            model="llama3-8B-chat",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.provider == "ollm"
    assert result.schema_valid is True
    assert result.model == "llama3-8B-chat"


# ---------------------------------------------------------------------------
# Request payload tests
# ---------------------------------------------------------------------------


def test_call_request_payload_has_stream_false():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"score": 50})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["stream"] is False


def test_call_request_payload_has_correct_model():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="gemma3-12B",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "gemma3-12B"


def test_call_request_payload_has_max_tokens():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=512,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["max_tokens"] == 512


def test_call_posts_to_correct_endpoint():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    called_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args[0][0]
    assert called_url == "http://localhost:8000/v1/chat/completions"


# ---------------------------------------------------------------------------
# Schema embedding tests
# ---------------------------------------------------------------------------


def test_call_embeds_schema_in_system():
    """Schema should be appended to the system message content."""
    provider = _make_provider()
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}

    with patch("requests.post", return_value=_make_chat_response({"score": 80})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="Rate this job.",
            messages=[{"role": "user", "content": "Job desc here."}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    system_content = payload["messages"][0]["content"]
    schema_str = json.dumps(schema, indent=2)
    assert schema_str in system_content
    assert "Rate this job." in system_content


def test_call_without_schema_no_schema_in_system():
    """No schema appended when output_schema=None."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="Rate this job.",
            messages=[{"role": "user", "content": "Job desc here."}],
            output_schema=None,
        )

    payload = mock_post.call_args.kwargs["json"]
    system_content = payload["messages"][0]["content"]
    assert system_content == "Rate this job."


# ---------------------------------------------------------------------------
# Messages format tests
# ---------------------------------------------------------------------------


def test_call_messages_format():
    """Messages list should start with system role, followed by user messages."""
    provider = _make_provider()
    user_messages = [
        {"role": "user", "content": "First message."},
        {"role": "assistant", "content": "Response."},
        {"role": "user", "content": "Follow-up."},
    ]

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
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
    """When timeout=None, requests.post should be called with timeout=300.0."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=None,
        )

    assert mock_post.call_args.kwargs["timeout"] == 300.0


def test_call_uses_custom_timeout():
    """When timeout=60.0, requests.post should be called with timeout=60.0."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=60.0,
        )

    assert mock_post.call_args.kwargs["timeout"] == 60.0


# ---------------------------------------------------------------------------
# Token count edge cases
# ---------------------------------------------------------------------------


def test_call_handles_missing_token_counts():
    """When usage key is absent, ModelResult should have input_tokens=0, output_tokens=0."""
    provider = _make_provider()
    mock_resp = _make_response({
        "choices": [{"message": {"role": "assistant", "content": json.dumps({"result": "ok"})}}],
    })

    with patch("requests.post", return_value=mock_resp):
        result = provider.call(
            model="llama3-8B-chat",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_call_raises_on_http_error():
    """HTTP 500 from /v1/chat/completions should raise requests.HTTPError."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_response({}, status_code=500)):
        with pytest.raises(requests.HTTPError):
            provider.call(
                model="llama3-8B-chat",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash():
    """Trailing slash in base_url config should not produce double slashes in URLs."""
    config = {"providers": {"ollm": {"base_url": "http://localhost:8000/"}}}
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllmProvider(config=config)

    if mock_get.call_args.args:
        called_url = mock_get.call_args.args[0]
    else:
        called_url = mock_get.call_args[0][0]
    assert "//" not in called_url.replace("http://", "").replace("https://", "")
