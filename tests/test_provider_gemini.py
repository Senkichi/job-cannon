"""Unit tests for job_finder.web.providers.gemini_provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.gemini_provider import GeminiProvider


def _make_mock_client() -> MagicMock:
    return MagicMock()


def _make_provider(client=None):
    if client is None:
        client = _make_mock_client()
    return GeminiProvider(config={}, client=client)


def _make_response(text="hello", prompt_token_count=10, candidates_token_count=5):
    resp = MagicMock()
    resp.text = text
    resp.usage_metadata = MagicMock()
    resp.usage_metadata.prompt_token_count = prompt_token_count
    resp.usage_metadata.candidates_token_count = candidates_token_count
    return resp


_SCHEMA: dict = {
    "type": "object",
    "required": ["score"],
    "properties": {"score": {"type": "integer"}},
}


def test_gemini_provider_is_base_provider_subclass():
    assert issubclass(GeminiProvider, BaseProvider)


def test_init_raises_import_error_when_sdk_unavailable():
    with patch("job_finder.web.providers.gemini_provider._GENAI_AVAILABLE", False):
        with pytest.raises(ImportError, match="google-genai"):
            GeminiProvider(config={})


def test_init_raises_value_error_when_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Gemini API key not set"):
        GeminiProvider(config={})


def test_init_with_injected_client_skips_key_resolution():
    mock_client = _make_mock_client()
    provider = GeminiProvider(config={}, client=mock_client)
    assert provider._client is mock_client


def test_init_builds_genai_client_from_env_var(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-xyz")
    with patch("job_finder.web.providers.gemini_provider.genai") as mock_genai:
        mock_genai.Client.return_value = MagicMock()
        provider = GeminiProvider(config={})
    mock_genai.Client.assert_called_once_with(api_key="test-key-xyz")
    assert provider._client is mock_genai.Client.return_value


def test_init_reads_retry_sleep_from_config():
    cfg = {"providers": {"gemini": {"retry_sleep_seconds": 3.0}}}
    provider = GeminiProvider(config=cfg, client=_make_mock_client())
    assert provider._retry_sleep == 3.0


def test_init_default_retry_sleep():
    assert _make_provider()._retry_sleep == 15.0


def test_call_freeform_returns_model_result_shape():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text="some text")
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}]
    )
    assert isinstance(result, ModelResult)
    assert result.provider == "gemini"
    assert result.model == "gemini-2.5-flash"
    assert result.cost_usd == 0.0
    assert result.schema_valid is True
    assert result.data == {"text": "some text"}


def test_call_freeform_token_counts():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(
        prompt_token_count=42, candidates_token_count=17
    )
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}]
    )
    assert result.input_tokens == 42
    assert result.output_tokens == 17


def test_call_handles_none_usage_metadata():
    resp = _make_response()
    resp.usage_metadata = None
    client = _make_mock_client()
    client.models.generate_content.return_value = resp
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}]
    )
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_call_with_schema_parses_json_response():
    payload = {"score": 85}
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text=json.dumps(payload))
    result = _make_provider(client).call(
        "gemini-2.5-pro",
        "sys",
        [{"role": "user", "content": "score this"}],
        output_schema=_SCHEMA,
    )
    assert result.data == payload
    assert result.schema_valid is True


def test_call_with_schema_raises_value_error_on_invalid_json():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text="NOT JSON")
    with pytest.raises(ValueError, match="Invalid JSON from Gemini"):
        _make_provider(client).call(
            "gemini-2.5-flash",
            "sys",
            [{"role": "user", "content": "q"}],
            output_schema=_SCHEMA,
        )


def test_call_passes_system_and_max_tokens_in_config():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response()
    provider = _make_provider(client)

    with patch(
        "job_finder.web.providers.gemini_provider.genai_types.GenerateContentConfig"
    ) as mock_cfg_cls:
        mock_cfg_cls.return_value = MagicMock()
        provider.call(
            "gemini-2.5-flash",
            "my system",
            [{"role": "user", "content": "hi"}],
            max_tokens=512,
        )

    kwargs = mock_cfg_cls.call_args.kwargs
    assert kwargs["system_instruction"] == "my system"
    assert kwargs["max_output_tokens"] == 512
    assert "response_mime_type" not in kwargs


def test_call_passes_schema_fields_in_config():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text=json.dumps({"score": 1}))

    with patch(
        "job_finder.web.providers.gemini_provider.genai_types.GenerateContentConfig"
    ) as mock_cfg_cls:
        mock_cfg_cls.return_value = MagicMock()
        _make_provider(client).call(
            "gemini-2.5-flash",
            "sys",
            [{"role": "user", "content": "q"}],
            output_schema=_SCHEMA,
        )

    kwargs = mock_cfg_cls.call_args.kwargs
    assert kwargs["response_mime_type"] == "application/json"
    assert kwargs["response_json_schema"] == _SCHEMA


def test_call_retries_once_on_429_api_error_then_succeeds():
    from google.genai import errors as genai_errors

    transient_exc = genai_errors.APIError(429, "rate limit", {"error": {}})
    good_resp = _make_response(text="ok")
    client = _make_mock_client()
    client.models.generate_content.side_effect = [transient_exc, good_resp]

    provider = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 0}}},
        client=client,
    )
    with patch("job_finder.web.providers.gemini_provider.time.sleep") as mock_sleep:
        result = provider.call("gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}])

    assert result.data == {"text": "ok"}
    mock_sleep.assert_called_once_with(0)
    assert client.models.generate_content.call_count == 2


def test_call_raises_immediately_on_non_transient_error():
    from google.genai import errors as genai_errors

    hard_exc = genai_errors.APIError(400, "bad request", {"error": {}})
    client = _make_mock_client()
    client.models.generate_content.side_effect = hard_exc

    with patch("job_finder.web.providers.gemini_provider.time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.APIError):
            _make_provider(client).call(
                "gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}]
            )

    mock_sleep.assert_not_called()
    assert client.models.generate_content.call_count == 1


def test_call_retries_on_string_429_in_generic_exception():
    good_resp = _make_response(text="retry worked")
    generic_exc = RuntimeError("HTTP 429 Too Many Requests")
    client = _make_mock_client()
    client.models.generate_content.side_effect = [generic_exc, good_resp]

    provider = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 0}}},
        client=client,
    )
    with patch("job_finder.web.providers.gemini_provider.time.sleep"):
        result = provider.call("gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}])
    assert result.data == {"text": "retry worked"}


def test_build_contents_user_role_unchanged():
    contents = _make_provider()._build_contents([{"role": "user", "content": "hello"}])
    assert len(contents) == 1
    assert contents[0].role == "user"


def test_build_contents_assistant_role_mapped_to_model():
    contents = _make_provider()._build_contents([{"role": "assistant", "content": "hi"}])
    assert contents[0].role == "model"


def test_build_contents_model_role_unchanged():
    contents = _make_provider()._build_contents([{"role": "model", "content": "hi"}])
    assert contents[0].role == "model"


def test_build_contents_defaults_missing_role_to_user():
    contents = _make_provider()._build_contents([{"content": "no role"}])
    assert contents[0].role == "user"


def test_build_contents_multiple_messages():
    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]
    contents = _make_provider()._build_contents(messages)
    assert len(contents) == 3
    assert [c.role for c in contents] == ["user", "model", "user"]
