"""Unit tests for job_finder.web.model_provider.

Tests all five resolution paths for resolve_provider_config(),
frozen dataclass behavior of ModelResult, and abstract enforcement for BaseProvider.
"""

import pytest

from job_finder.web.model_provider import (
    BaseProvider,
    ModelResult,
    resolve_provider_config,
)


# ---------------------------------------------------------------------------
# ModelResult tests
# ---------------------------------------------------------------------------


def test_model_result_fields():
    result = ModelResult(
        data={"score": 75},
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.01
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.model == "claude-sonnet-4-6"
    assert result.provider == "anthropic"
    assert result.schema_valid is True


def test_model_result_is_frozen():
    from dataclasses import FrozenInstanceError

    result = ModelResult(
        data={"score": 75},
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )
    with pytest.raises(FrozenInstanceError):
        result.data = {"score": 99}


# ---------------------------------------------------------------------------
# BaseProvider tests
# ---------------------------------------------------------------------------


def test_base_provider_is_abstract():
    with pytest.raises(TypeError):
        BaseProvider()


def test_base_provider_subclass_must_implement_call():
    class IncompleteProvider(BaseProvider):
        pass

    with pytest.raises(TypeError):
        IncompleteProvider()


# ---------------------------------------------------------------------------
# resolve_provider_config tests
# ---------------------------------------------------------------------------


def test_resolve_provider_from_config():
    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.5-pro"}}}
    result = resolve_provider_config("sonnet", config)
    assert result == {"provider": "gemini", "model": "gemini-2.5-pro", "fallback": None}


def test_resolve_provider_with_fallback():
    config = {
        "providers": {
            "sonnet": {
                "provider": "gemini",
                "model": "gemini-2.5-pro",
                "fallback": "anthropic",
            }
        }
    }
    result = resolve_provider_config("sonnet", config)
    assert result["fallback"] == "anthropic"
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-2.5-pro"


def test_resolve_provider_missing_falls_back_to_anthropic():
    config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-6"}}}
    result = resolve_provider_config("sonnet", config)
    assert result == {"provider": "anthropic", "model": "claude-sonnet-4-6", "fallback": None}


def test_resolve_provider_no_providers_section():
    config = {}
    result = resolve_provider_config("sonnet", config)
    assert result == {"provider": "anthropic", "model": "claude-sonnet-4-6", "fallback": None}


def test_resolve_provider_tier_model_missing_uses_scoring_models():
    config = {
        "providers": {"sonnet": {"provider": "ollama"}},
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
    }
    result = resolve_provider_config("sonnet", config)
    assert result["model"] == "claude-sonnet-4-6"
    assert result["provider"] == "ollama"


def test_resolve_provider_haiku_tier():
    config = {}
    result = resolve_provider_config("haiku", config)
    assert result == {"provider": "anthropic", "model": "claude-haiku-4-5", "fallback": None}


def test_resolve_provider_opus_tier():
    config = {}
    result = resolve_provider_config("opus", config)
    assert result == {"provider": "anthropic", "model": "claude-opus-4-6", "fallback": None}
