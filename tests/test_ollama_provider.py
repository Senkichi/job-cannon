"""Unit tests for job_finder.web.providers.ollama_provider.

Tests cover:
- Subclass relationship (OllamaProvider is a BaseProvider)
- Health check on init via GET /api/tags
- RuntimeError on connection failure/timeout/HTTP error
- Custom base_url via config
- call() returns correct ModelResult fields
- Request payload structure: stream=False, format=<"json"|schema-dict>, model, num_predict
- Schema embedded in system prompt ONLY when output_schema is None (legacy path)
- Schema dict forwarded via payload.format unchanged when output_schema is a dict (v3.0)
- Messages format (system first, then user messages)
- Timeout handling (default and custom)
- Missing token counts defaulting to 0
- HTTP errors from requests.post raise requests.HTTPError
- Trailing slash stripped from base_url
- Deterministic default inference options (temperature=0, seed=42, num_ctx=8192,
  top_p=0.9, num_predict from max_tokens, repeat_penalty=1.05)
- Per-call options override merge without cross-call state leak
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.ollama_provider import OllamaProvider

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
    content_dict: dict, prompt_eval_count: int = 100, eval_count: int = 50
) -> MagicMock:
    """Create a mock /api/chat response."""
    return _make_response(
        {
            "message": {"content": json.dumps(content_dict)},
            "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count,
        }
    )


def _make_provider(config: dict | None = None) -> OllamaProvider:
    """Create OllamaProvider with mocked health check (always succeeds)."""
    if config is None:
        config = {}
    with patch("requests.get", return_value=_make_response({"models": []})):
        return OllamaProvider(config=config)


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_ollama_provider_is_base_provider_subclass():
    assert issubclass(OllamaProvider, BaseProvider)


# ---------------------------------------------------------------------------
# __init__ / health check tests
# ---------------------------------------------------------------------------


def test_init_calls_health_check():
    """Health check should hit http://localhost:11434/api/tags with timeout=2.0."""
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllamaProvider(config={})

    mock_get.assert_called_once_with(
        "http://localhost:11434/api/tags",
        timeout=2.0,
    )


def test_init_raises_on_connection_error():
    with patch("requests.get", side_effect=requests.ConnectionError("Connection refused")):
        with pytest.raises(RuntimeError, match="Ollama service unreachable"):
            OllamaProvider(config={})


def test_init_raises_on_timeout():
    with patch("requests.get", side_effect=requests.Timeout("Timed out")):
        with pytest.raises(RuntimeError, match="Ollama service unreachable"):
            OllamaProvider(config={})


def test_init_raises_on_http_error():
    with patch("requests.get", return_value=_make_response({}, status_code=500)):
        with pytest.raises(RuntimeError):
            OllamaProvider(config={})


def test_init_custom_base_url():
    """Custom base_url from config should be used for health check."""
    config = {"providers": {"ollama": {"base_url": "http://myhost:9999"}}}
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllamaProvider(config=config)

    mock_get.assert_called_once_with(
        "http://myhost:9999/api/tags",
        timeout=2.0,
    )


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_returns_model_result():
    provider = _make_provider()

    with patch(
        "requests.post",
        return_value=_make_chat_response({"score": 75}, prompt_eval_count=100, eval_count=50),
    ):
        result = provider.call(
            model="qwen2.5:32b",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.provider == "ollama"
    assert result.schema_valid is True
    assert result.model == "qwen2.5:32b"


# ---------------------------------------------------------------------------
# Request payload tests (legacy format='json' string path)
# ---------------------------------------------------------------------------


def test_call_request_payload_has_stream_false_and_format_json():
    """Legacy path: output_schema=None → format='json' string in payload."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"score": 50})) as mock_post:
        provider.call(
            model="qwen2.5:32b",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    call_kwargs = mock_post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["stream"] is False
    assert payload["format"] == "json"


def test_call_request_payload_has_correct_model():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="llama3.1:8b",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "llama3.1:8b"


def test_call_request_payload_has_num_predict():
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="qwen2.5:32b",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=512,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["options"]["num_predict"] == 512


# ---------------------------------------------------------------------------
# Schema embedding tests — legacy path (output_schema is None)
# ---------------------------------------------------------------------------


def test_call_embeds_schema_in_system():
    """Schema should be embedded as field instructions and example in system message.

    Legacy path: this only applies when the caller passes output_schema BUT the
    v3.0 branch is not taken — historically all callers passed a schema and got
    the string prompt injection. We keep this branch live for the schema-present
    legacy string-format path (format='json') until Phase 34 Plan 4 deletes it.
    However, the v3.0 refactor unifies the rule: schema present → forward as dict.
    So the old field-instruction path is only reachable when test harnesses
    explicitly ask for it. This test now asserts the CURRENT post-v3 behavior:
    when output_schema is a dict, payload.format IS the dict and the system
    prompt is NOT polluted with the "EXACTLY these fields" block (grammar-
    constrained decoding handles field names at the token level).
    """
    provider = _make_provider()
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}

    with patch("requests.post", return_value=_make_chat_response({"score": 80})) as mock_post:
        provider.call(
            model="qwen2.5:32b",
            system="Rate this job.",
            messages=[{"role": "user", "content": "Job desc here."}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    system_content = payload["messages"][0]["content"]
    # v3.0: system prompt is NOT polluted — grammar handles the schema.
    assert "Rate this job." in system_content
    assert "EXACTLY these fields" not in system_content
    assert "Example structure" not in system_content
    # And payload.format IS the schema dict
    assert payload["format"] == schema


def test_call_without_schema_no_schema_in_system():
    """No schema appended when output_schema=None."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"result": "ok"})) as mock_post:
        provider.call(
            model="qwen2.5:32b",
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
            model="qwen2.5:32b",
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
            model="qwen2.5:32b",
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
            model="qwen2.5:32b",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            timeout=60.0,
        )

    assert mock_post.call_args.kwargs["timeout"] == 60.0


# ---------------------------------------------------------------------------
# Token count edge cases
# ---------------------------------------------------------------------------


def test_call_handles_missing_token_counts():
    """When token count keys are absent, ModelResult should have input_tokens=0, output_tokens=0."""
    provider = _make_provider()
    # Response with no prompt_eval_count or eval_count
    mock_resp = _make_response(
        {
            "message": {"content": json.dumps({"result": "ok"})},
        }
    )

    with patch("requests.post", return_value=mock_resp):
        result = provider.call(
            model="qwen2.5:32b",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_call_raises_on_http_error():
    """HTTP 500 from /api/chat should raise requests.HTTPError."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_response({}, status_code=500)):
        with pytest.raises(requests.HTTPError):
            provider.call(
                model="qwen2.5:32b",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash():
    """Trailing slash in base_url config should not produce double slashes in URLs."""
    config = {"providers": {"ollama": {"base_url": "http://localhost:11434/"}}}
    with patch("requests.get", return_value=_make_response({})) as mock_get:
        OllamaProvider(config=config)

    # URL should NOT have double slashes
    called_url = (
        mock_get.call_args.args[0]
        if mock_get.call_args.args
        else mock_get.call_args.kwargs.get("url", "")
    )
    # Use the first positional arg
    if mock_get.call_args.args:
        called_url = mock_get.call_args.args[0]
    else:
        called_url = mock_get.call_args[0][0]
    assert "//" not in called_url.replace("http://", "").replace("https://", "")


# ---------------------------------------------------------------------------
# v3.0 upgrade: schema-dict forwarding via format=<dict>
# ---------------------------------------------------------------------------


def test_call_forwards_schema_dict_via_format_unchanged():
    """When output_schema is a dict, payload.format IS that dict (NOT 'json', NOT re-serialized)."""
    provider = _make_provider()
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }

    with patch("requests.post", return_value=_make_chat_response({"x": 1})) as mock_post:
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            output_schema=schema,
        )

    payload = mock_post.call_args.kwargs["json"]
    # format must be the dict — same object reference (not stringified, not 'json' literal)
    assert payload["format"] == schema
    assert payload["format"] is schema
    # And it is emphatically NOT the string literal
    assert payload["format"] != "json"


def test_call_legacy_format_json_string_when_schema_none():
    """Backward compat: output_schema=None → payload.format == 'json' string."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"ok": True})) as mock_post:
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            output_schema=None,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["format"] == "json"
    assert isinstance(payload["format"], str)


# ---------------------------------------------------------------------------
# v3.0 upgrade: deterministic default inference options
# ---------------------------------------------------------------------------


def test_call_default_options_are_deterministic():
    """Every call sends temperature=0, seed=42, num_ctx=8192, top_p=0.9,
    num_predict (from max_tokens), repeat_penalty=1.05 by default.
    """
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"ok": True})) as mock_post:
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=777,
        )

    payload = mock_post.call_args.kwargs["json"]
    opts = payload["options"]
    assert opts["temperature"] == 0
    assert opts["seed"] == 42
    assert opts["num_ctx"] == 8192
    assert opts["top_p"] == 0.9
    assert opts["num_predict"] == 777
    assert opts["repeat_penalty"] == 1.05


def test_call_per_call_options_override_merges_into_defaults():
    """Passing options={...} merges into defaults; overridden keys win, unspecified keys retain defaults."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"ok": True})) as mock_post:
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.3, "seed": 99},
        )

    payload = mock_post.call_args.kwargs["json"]
    opts = payload["options"]
    # overrides win
    assert opts["temperature"] == 0.3
    assert opts["seed"] == 99
    # defaults for unspecified keys still present
    assert opts["num_ctx"] == 8192
    assert opts["top_p"] == 0.9
    assert opts["repeat_penalty"] == 1.05


def test_call_options_override_does_not_leak_across_calls():
    """Two sequential calls with different options produce independent payloads — no instance state mutation."""
    provider = _make_provider()

    with patch("requests.post", return_value=_make_chat_response({"ok": True})) as mock_post:
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "a"}],
            options={"temperature": 0.8, "num_ctx": 4096},
        )
        provider.call(
            model="qwen2.5:14b",
            system="s",
            messages=[{"role": "user", "content": "b"}],
        )

    # Two calls → two payloads
    assert mock_post.call_count == 2
    first_opts = mock_post.call_args_list[0].kwargs["json"]["options"]
    second_opts = mock_post.call_args_list[1].kwargs["json"]["options"]

    # First call used overrides
    assert first_opts["temperature"] == 0.8
    assert first_opts["num_ctx"] == 4096
    # Second call used defaults — no leak from first call
    assert second_opts["temperature"] == 0
    assert second_opts["num_ctx"] == 8192
    assert second_opts["seed"] == 42
