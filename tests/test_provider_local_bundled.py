"""Unit tests for job_finder.web.providers.local_bundled.

Critical design property: the module must import successfully even when
llama-cpp-python is NOT installed (lazy import inside __init__). This
test file therefore does NOT use `pytest.importorskip("llama_cpp")` at
module top — that would silently skip the entire file on every dev/CI
machine without [local-ai]. Instead, the tests that exercise call()
inject a fake llama_cpp module into sys.modules before construction.

Tests cover:
- Module import succeeds without llama-cpp-python (LAZY IMPORT INVARIANT)
- LocalBundledProvider is a BaseProvider subclass
- Empty model_path -> FileNotFoundError
- Non-existent GGUF file -> FileNotFoundError
- llama_cpp absent -> ImportError on construction (NOT on module import)
- call() forwards messages + response_format to create_chat_completion
- call() returns canonical 7-field ModelResult
- data is {"text": ...} when output_schema=None and content is plain text
- data is parsed dict when output_schema is provided
- ModelResult.model is model_path (the GGUF path IS the model identity)
- input_tokens / output_tokens from response["usage"]
"""

from __future__ import annotations

import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult

# ---------------------------------------------------------------------------
# Lazy-import invariant — MUST run without llama_cpp installed
# ---------------------------------------------------------------------------


def test_module_imports_without_llama_cpp_installed():
    """The module must be importable on a vanilla venv (no [local-ai] extra).
    Only LocalBundledProvider(...) construction may raise ImportError."""
    mod = importlib.import_module("job_finder.web.providers.local_bundled")
    assert hasattr(mod, "LocalBundledProvider")


def test_local_bundled_provider_is_base_provider_subclass():
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    assert issubclass(LocalBundledProvider, BaseProvider)


def test_init_raises_import_error_when_llama_cpp_absent():
    """When llama_cpp is genuinely not installed (the default project state),
    construction raises ImportError with install instruction."""
    # Ensure llama_cpp is absent. If it IS installed (e.g. via [local-ai]),
    # this test is meaningless — skip it. Otherwise the import inside
    # __init__ must raise ImportError.
    try:
        import llama_cpp  # noqa: F401
        pytest.skip("llama_cpp is installed; this test only runs without it")
    except ImportError:
        pass

    from job_finder.web.providers.local_bundled import LocalBundledProvider
    with pytest.raises(ImportError, match="llama-cpp-python is required"):
        LocalBundledProvider(model_path="/fake.gguf")


# ---------------------------------------------------------------------------
# Helpers: inject a fake llama_cpp module so construction succeeds
# ---------------------------------------------------------------------------


class _FakeLlama:
    """Stand-in for llama_cpp.Llama; records construction args + returns
    canned create_chat_completion responses."""

    last_init_kwargs: dict | None = None
    next_response: dict = {
        "choices": [{"message": {"content": "pong"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }

    def __init__(self, **kwargs) -> None:
        type(self).last_init_kwargs = kwargs

    def create_chat_completion(self, **kwargs) -> dict:
        self.last_call_kwargs = kwargs
        return type(self).next_response


@pytest.fixture
def fake_llama_cpp(monkeypatch):
    """Inject a fake llama_cpp module into sys.modules so the lazy import
    inside __init__ succeeds without actually loading the C++ extension."""
    fake_module = MagicMock()
    fake_module.Llama = _FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_module)
    # Reset class state between tests
    _FakeLlama.last_init_kwargs = None
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": "pong"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    yield fake_module


def _make_provider(model_path: str = "/fake/model.gguf"):
    """Construct LocalBundledProvider with mocked Llama + mocked path existence."""
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    with patch("pathlib.Path.exists", return_value=True):
        return LocalBundledProvider(model_path=model_path)


# ---------------------------------------------------------------------------
# FileNotFoundError path tests
# ---------------------------------------------------------------------------


def test_init_raises_file_not_found_when_model_path_empty(fake_llama_cpp):
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    with pytest.raises(FileNotFoundError, match="not configured"):
        LocalBundledProvider(model_path="")


def test_init_raises_file_not_found_when_gguf_missing(fake_llama_cpp):
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    with patch("pathlib.Path.exists", return_value=False):
        with pytest.raises(FileNotFoundError, match="GGUF model not found"):
            LocalBundledProvider(model_path="/nope.gguf")


def test_init_constructs_llama_with_expected_kwargs(fake_llama_cpp):
    provider = _make_provider(model_path="/m.gguf")
    assert _FakeLlama.last_init_kwargs is not None
    assert _FakeLlama.last_init_kwargs["model_path"] == "/m.gguf"
    assert _FakeLlama.last_init_kwargs["n_ctx"] == 4096
    assert _FakeLlama.last_init_kwargs["verbose"] is False
    assert _FakeLlama.last_init_kwargs["n_threads"] >= 1


def test_init_accepts_custom_n_ctx(fake_llama_cpp):
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    with patch("pathlib.Path.exists", return_value=True):
        LocalBundledProvider(model_path="/m.gguf", n_ctx=8192)
    assert _FakeLlama.last_init_kwargs["n_ctx"] == 8192


# ---------------------------------------------------------------------------
# ModelResult shape + call() forwarding tests
# ---------------------------------------------------------------------------


def test_call_returns_seven_field_model_result_with_correct_types(fake_llama_cpp):
    provider = _make_provider()
    result = provider.call(
        model="ignored-for-local",
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(result, ModelResult)
    assert isinstance(result.data, dict)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.model, str)
    assert isinstance(result.provider, str)
    assert isinstance(result.schema_valid, bool)


def test_call_returns_zero_cost_and_local_bundled_provider(fake_llama_cpp):
    provider = _make_provider(model_path="/m.gguf")
    result = provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert result.cost_usd == 0.0
    assert result.provider == "local_bundled"
    # model field reflects GGUF path, not the `model` arg
    assert result.model == "/m.gguf"


def test_call_forwards_messages_with_system_prepended(fake_llama_cpp):
    provider = _make_provider()
    provider.call(
        "ignored",
        "you are concise",
        [{"role": "user", "content": "hello"}],
    )
    msgs = provider._llm.last_call_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "you are concise"}
    assert msgs[1] == {"role": "user", "content": "hello"}


def test_call_with_output_schema_passes_response_format(fake_llama_cpp):
    provider = _make_provider()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": json.dumps({"x": 42})}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    result = provider.call(
        "ignored", "s", [{"role": "user", "content": "u"}], output_schema=schema
    )
    rf = provider._llm.last_call_kwargs["response_format"]
    assert rf == {"type": "json_object", "schema": schema}
    assert result.data == {"x": 42}
    assert result.schema_valid is True


def test_call_freeform_plain_text_returns_text_dict(fake_llama_cpp):
    """M-1 (2026-05-20): no-schema path returns schema_valid=True (nothing to
    validate against). Previously False, which would have biased the cascade-
    audit signal against local_bundled on schema-less callsites."""
    provider = _make_provider()
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": "just text"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    result = provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert result.data == {"text": "just text"}
    assert result.schema_valid is True


def test_call_freeform_json_content_returns_parsed_dict(fake_llama_cpp):
    """M-1: caller did not request schema enforcement → schema_valid=True."""
    provider = _make_provider()
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": '{"k": "v"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    result = provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert result.data == {"k": "v"}
    assert result.schema_valid is True


def test_call_tokens_from_usage_block(fake_llama_cpp):
    provider = _make_provider()
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 23},
    }
    result = provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert result.input_tokens == 17
    assert result.output_tokens == 23


def test_call_missing_usage_block_defaults_to_zero(fake_llama_cpp):
    provider = _make_provider()
    _FakeLlama.next_response = {
        "choices": [{"message": {"content": "ok"}}],
        # no usage block
    }
    result = provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_call_does_not_pass_response_format_when_schema_none(fake_llama_cpp):
    provider = _make_provider()
    provider.call("ignored", "s", [{"role": "user", "content": "u"}])
    assert "response_format" not in provider._llm.last_call_kwargs
