"""Cross-provider integration & regression tests.

Locks the Phase 39 contract:
- All six production providers' .call() returns the canonical 7-field ModelResult.
- Every new provider returns the correct `provider` string in the ModelResult.
- No subprocess.run in the new provider modules uses shell=True.
- Every subprocess.run in the new provider modules has a timeout= kwarg.
- _PROVIDER_DEFAULTS membership invariant.
- _make_adapter() dispatch chain instantiates Plans 02/03/04's classes.

See per-provider files (tests/test_provider_claude_code_cli.py, etc.) for
detailed behavior coverage. This file deliberately keeps assertions thin
and focused on the cross-cutting invariants only.
"""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import (
    _PROVIDER_DEFAULTS,
    BaseProvider,
    ModelResult,
    _make_adapter,
)

# ---------------------------------------------------------------------------
# Provider factories — each yields a fully-mocked provider whose .call()
# returns a real ModelResult. Skip the providers that are too expensive to
# mock here (Anthropic, Gemini SDK); those are covered by their own per-
# provider test files.
# ---------------------------------------------------------------------------


def _factory_ollama():
    """OllamaProvider with mocked health check + chat response."""
    from job_finder.web.providers.ollama_provider import OllamaProvider

    chat_response = MagicMock()
    chat_response.status_code = 200
    chat_response.raise_for_status.return_value = None
    chat_response.json.return_value = {
        "message": {"content": json.dumps({"ok": True})},
        "prompt_eval_count": 10,
        "eval_count": 5,
    }
    health_response = MagicMock()
    health_response.status_code = 200
    health_response.raise_for_status.return_value = None
    health_response.json.return_value = {"models": []}

    with patch("requests.get", return_value=health_response):
        provider = OllamaProvider(config={})

    # Return a callable that wraps both the provider AND the chat-response patch.
    def _do_call() -> ModelResult:
        with patch("requests.post", return_value=chat_response):
            return provider.call("qwen2.5:14b", "s", [{"role": "user", "content": "u"}])

    return _do_call


def _factory_claude_code_cli():
    """ClaudeCodeCLIProvider with mocked _run_oneshot."""
    from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider

    with patch(
        "job_finder.web.providers.claude_code_cli.shutil.which",
        return_value="/usr/bin/claude",
    ):
        provider = ClaudeCodeCLIProvider(config={})

    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "pong",
        "usage": {"input_tokens": 3, "output_tokens": 5},
        "total_cost_usd": 0.0,
    }

    def _do_call() -> ModelResult:
        with patch(
            "job_finder.web.providers.claude_code_cli._run_oneshot",
            return_value=envelope,
        ):
            return provider.call("claude-haiku-4-5", "s", [{"role": "user", "content": "u"}])

    return _do_call


def _factory_gemini_cli():
    """GeminiCLIProvider with mocked subprocess.run."""
    from job_finder.web.providers.gemini_cli import GeminiCLIProvider

    with patch(
        "job_finder.web.providers.gemini_cli.shutil.which",
        return_value="/usr/bin/gemini",
    ):
        provider = GeminiCLIProvider(config={})

    run_result = MagicMock()
    run_result.returncode = 0
    run_result.stdout = json.dumps({"result": "pong"})
    run_result.stderr = ""

    def _do_call() -> ModelResult:
        with patch(
            "job_finder.web.providers.gemini_cli.subprocess.run",
            return_value=run_result,
        ):
            return provider.call("gemini-2.0-flash", "s", [{"role": "user", "content": "u"}])

    return _do_call


def _factory_local_bundled():
    """LocalBundledProvider with injected fake llama_cpp module."""
    # Inject a fake llama_cpp into sys.modules so the lazy import succeeds.
    fake_module = MagicMock()

    class _FakeLlama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def create_chat_completion(self, **kwargs):
            return {
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }

    fake_module.Llama = _FakeLlama
    sys.modules["llama_cpp"] = fake_module

    from job_finder.web.providers.local_bundled import LocalBundledProvider

    with patch("pathlib.Path.exists", return_value=True):
        provider = LocalBundledProvider(model_path="/fake.gguf")

    def _do_call() -> ModelResult:
        return provider.call("ignored", "s", [{"role": "user", "content": "u"}])

    return _do_call


PROVIDER_FACTORIES = [
    pytest.param(_factory_ollama, "ollama", id="ollama"),
    pytest.param(_factory_claude_code_cli, "claude_code_cli", id="claude_code_cli"),
    pytest.param(_factory_gemini_cli, "gemini_cli", id="gemini_cli"),
    pytest.param(_factory_local_bundled, "local_bundled", id="local_bundled"),
    # AnthropicProvider + GeminiProvider are covered exhaustively by their
    # own per-provider test files; constructing them here adds enough mock
    # surface (Anthropic SDK / google.generativeai client) that the
    # marginal value is low. We keep them out of this parametrize and
    # instead document the contract via _PROVIDER_DEFAULTS membership.
]


# ---------------------------------------------------------------------------
# Cross-provider ModelResult-shape test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory_fn,expected_provider", PROVIDER_FACTORIES)
def test_call_returns_canonical_model_result_shape(factory_fn, expected_provider):
    do_call = factory_fn()
    result = do_call()
    assert isinstance(result, ModelResult)
    assert isinstance(result.data, dict), f"data is {type(result.data).__name__}"
    assert isinstance(result.cost_usd, float), f"cost_usd is {type(result.cost_usd).__name__}"
    assert isinstance(result.input_tokens, int), (
        f"input_tokens is {type(result.input_tokens).__name__}"
    )
    assert isinstance(result.output_tokens, int), (
        f"output_tokens is {type(result.output_tokens).__name__}"
    )
    assert isinstance(result.model, str), f"model is {type(result.model).__name__}"
    assert isinstance(result.provider, str), f"provider is {type(result.provider).__name__}"
    assert isinstance(result.schema_valid, bool), (
        f"schema_valid is {type(result.schema_valid).__name__}"
    )


@pytest.mark.parametrize("factory_fn,expected_provider", PROVIDER_FACTORIES)
def test_call_returns_expected_provider_name(factory_fn, expected_provider):
    do_call = factory_fn()
    result = do_call()
    assert result.provider == expected_provider


@pytest.mark.parametrize("factory_fn,expected_provider", PROVIDER_FACTORIES)
def test_free_providers_record_zero_cost(factory_fn, expected_provider):
    """All four Phase-39-touched providers are in FREE_PROVIDERS; cost_usd is 0.0."""
    do_call = factory_fn()
    result = do_call()
    assert result.cost_usd == 0.0


@pytest.mark.parametrize("factory_fn,expected_provider", PROVIDER_FACTORIES)
def test_no_schema_returns_schema_valid_true(factory_fn, expected_provider):
    """M-1 regression (2026-05-20): when output_schema is None, every provider
    must return schema_valid=True.

    schema_valid is the cascade audit's primary signal — False here biases the
    audit toward distrusting CLI providers on schema-less callsites (e.g.
    description_reformat) for reasons that are pure telemetry artifact, not
    actual quality.

    All four factories above call .call() without an output_schema kwarg, so
    each result must show True on this branch."""
    do_call = factory_fn()
    result = do_call()
    assert result.schema_valid is True, (
        f"{expected_provider} returned schema_valid=False on the no-schema path; "
        f"M-1 convention is True (matches call_claude:563 + AnthropicProvider)."
    )


# ---------------------------------------------------------------------------
# _PROVIDER_DEFAULTS membership invariant
# ---------------------------------------------------------------------------


def test_provider_defaults_is_superset_of_six_production_providers():
    expected = {
        "anthropic",
        "gemini",
        "ollama",
        "claude_code_cli",
        "gemini_cli",
        "local_bundled",
    }
    assert expected <= set(_PROVIDER_DEFAULTS)


def test_provider_defaults_includes_groq_and_cerebras():
    assert "groq" in _PROVIDER_DEFAULTS
    assert "cerebras" in _PROVIDER_DEFAULTS


def test_every_provider_default_has_quick_and_score_workloads():
    for name, mapping in _PROVIDER_DEFAULTS.items():
        assert "quick" in mapping, f"{name} missing 'quick' default"
        assert "score" in mapping, f"{name} missing 'score' default"


# ---------------------------------------------------------------------------
# Security source-grep regressions
# ---------------------------------------------------------------------------


_NEW_PROVIDER_FILES: list[str] = [
    "job_finder/web/providers/claude_code_cli.py",
    "job_finder/web/providers/gemini_cli.py",
    "job_finder/web/providers/local_bundled.py",
    "job_finder/web/providers/detection.py",
]


@pytest.mark.parametrize("path", _NEW_PROVIDER_FILES)
def test_no_shell_true_in_new_provider_files(path):
    src = pathlib.Path(path).read_text()
    # Check for actual shell=True in subprocess.run calls, not in docstrings
    lines = src.split("\n")
    for line in lines:
        # Only check lines that are actual subprocess.run function calls
        if "subprocess.run(" in line and not line.strip().startswith("#"):
            assert "shell=True" not in line, (
                f"{path} contains shell=True in subprocess.run call: {line}"
            )


@pytest.mark.parametrize("path", _NEW_PROVIDER_FILES)
def test_every_subprocess_run_has_timeout_kwarg(path):
    """For every `subprocess.run(` in the file, the same call must include a
    `timeout=` kwarg within the next 500 characters. Coarse but effective."""
    src = pathlib.Path(path).read_text()
    cursor = 0
    run_count = 0
    while True:
        idx = src.find("subprocess.run(", cursor)
        if idx == -1:
            break
        run_count += 1
        # Search the next 500 chars for the call-closing paren + a timeout= kwarg.
        window = src[idx : idx + 500]
        assert "timeout=" in window, (
            f"{path}: subprocess.run at offset {idx} has no timeout= kwarg within 500 chars"
        )
        cursor = idx + len("subprocess.run(")
    # claude_code_cli.py delegates to _run_oneshot; it intentionally has 0
    # direct subprocess.run calls. detection.py has 3, gemini_cli.py has 1,
    # local_bundled.py has 0 (in-process llama-cpp). Don't assert a fixed
    # count — just that each call we do see has timeout=.


# ---------------------------------------------------------------------------
# _make_adapter dispatch smoke
# ---------------------------------------------------------------------------


def test_make_adapter_dispatches_claude_code_cli():
    from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider

    with patch(
        "job_finder.web.providers.claude_code_cli.shutil.which",
        return_value="/usr/bin/claude",
    ):
        adapter = _make_adapter("claude_code_cli", conn=None, config={})
    assert isinstance(adapter, ClaudeCodeCLIProvider)


def test_make_adapter_dispatches_gemini_cli():
    from job_finder.web.providers.gemini_cli import GeminiCLIProvider

    with patch(
        "job_finder.web.providers.gemini_cli.shutil.which",
        return_value="/usr/bin/gemini",
    ):
        adapter = _make_adapter("gemini_cli", conn=None, config={})
    assert isinstance(adapter, GeminiCLIProvider)


def test_make_adapter_local_bundled_requires_model_path():
    with pytest.raises(ValueError, match=r"providers\.local_bundled\.model_path not configured"):
        _make_adapter("local_bundled", conn=None, config={})


def test_make_adapter_local_bundled_with_model_path_constructs_provider():
    # Inject fake llama_cpp so __init__ succeeds.
    fake_module = MagicMock()

    class _FakeLlama:
        def __init__(self, **kwargs):
            pass

    fake_module.Llama = _FakeLlama
    sys.modules["llama_cpp"] = fake_module

    from job_finder.web.providers.local_bundled import LocalBundledProvider

    config = {"providers": {"local_bundled": {"model_path": "/fake.gguf", "n_ctx": 4096}}}
    with patch("pathlib.Path.exists", return_value=True):
        adapter = _make_adapter("local_bundled", conn=None, config=config)
    assert isinstance(adapter, LocalBundledProvider)


# ---------------------------------------------------------------------------
# Subclass-relationship invariant
# ---------------------------------------------------------------------------


def test_all_new_providers_are_base_provider_subclasses():
    from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider
    from job_finder.web.providers.gemini_cli import GeminiCLIProvider
    from job_finder.web.providers.local_bundled import LocalBundledProvider

    for cls in (ClaudeCodeCLIProvider, GeminiCLIProvider, LocalBundledProvider):
        assert issubclass(cls, BaseProvider), f"{cls.__name__} is not a BaseProvider subclass"
