"""Tests for cascade fall-through when Ollama is marked unavailable at startup.

Covers:
- ProviderUnavailable is a RuntimeError subclass (caught by cascade tuples)
- Cascade with _jf_ollama_unavailable=True skips Ollama and reaches the next
  provider without invoking OllamaProvider.__init__ (mock spy)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import ProviderUnavailable

# ---------------------------------------------------------------------------
# ProviderUnavailable hierarchy
# ---------------------------------------------------------------------------


def test_provider_unavailable_is_runtime_error_subclass():
    """ProviderUnavailable must be caught by the existing (ValueError, RuntimeError, ImportError)
    catch tuples in model_provider.py at lines ~315 and ~693 — no tuple changes needed."""
    assert issubclass(ProviderUnavailable, RuntimeError)


def test_provider_unavailable_is_exception():
    """Sanity: can be raised and caught as a RuntimeError."""
    with pytest.raises(RuntimeError):
        raise ProviderUnavailable("ollama marked unavailable at startup")


def test_provider_unavailable_message_preserved():
    exc = ProviderUnavailable("ollama marked unavailable at startup")
    assert "ollama" in str(exc)


# ---------------------------------------------------------------------------
# Cascade skips Ollama when _jf_ollama_unavailable=True
# ---------------------------------------------------------------------------


def test_make_adapter_raises_provider_unavailable_when_flagged():
    """_make_adapter('ollama', config={'_jf_ollama_unavailable': True}) must raise
    ProviderUnavailable without calling OllamaProvider.__init__."""
    from job_finder.web.model_provider import _make_adapter

    config = {"_jf_ollama_unavailable": True}

    # OllamaProvider is lazily imported inside _make_adapter; patch at source module.
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider",
        autospec=True,
    ) as mock_ollama_cls:
        with pytest.raises(ProviderUnavailable):
            _make_adapter("ollama", config=config)

        # OllamaProvider.__init__ must NOT have been called
        mock_ollama_cls.assert_not_called()


def test_make_adapter_calls_ollama_when_not_flagged():
    """Sanity: when _jf_ollama_unavailable is absent, OllamaProvider IS constructed."""
    from job_finder.web.model_provider import _make_adapter

    config: dict = {}

    mock_provider = MagicMock()

    # OllamaProvider is lazily imported inside _make_adapter; patch at source module.
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider",
        return_value=mock_provider,
    ) as mock_ollama_cls:
        result = _make_adapter("ollama", config=config)

    mock_ollama_cls.assert_called_once_with(config=config)
    assert result is mock_provider


def test_cascade_skips_ollama_reaches_next_provider():
    """The cascade must skip Ollama when _jf_ollama_unavailable=True and attempt the
    next adapter without invoking OllamaProvider.__init__ (mock spy).

    We test this via _make_adapter directly:
    - _make_adapter("ollama", config=flagged) raises ProviderUnavailable
    - _make_adapter("anthropic", config=flagged) succeeds (Ollama flag is irrelevant)
    This mirrors what the cascade loop does: it calls _make_adapter per entry, catches
    (ValueError, RuntimeError, ImportError) — which includes ProviderUnavailable via
    RuntimeError — and falls through to the next entry.
    """
    from job_finder.web.model_provider import ProviderUnavailable, _make_adapter

    flagged_config = {"_jf_ollama_unavailable": True}

    mock_provider = MagicMock()

    with (
        # Spy on OllamaProvider constructor — it must NOT be called
        patch(
            "job_finder.web.providers.ollama_provider.OllamaProvider",
            autospec=True,
        ) as mock_ollama_cls,
        # Stub out AnthropicProvider so we don't need real API keys
        patch(
            "job_finder.web.providers.anthropic_provider.AnthropicProvider",
            return_value=mock_provider,
        ) as mock_anthropic_cls,
        patch(
            "job_finder.web.model_provider.is_anthropic_available",
            return_value=True,
        ),
    ):
        # Step 1: Ollama is skipped — adapter raises ProviderUnavailable
        with pytest.raises(ProviderUnavailable):
            _make_adapter("ollama", config=flagged_config)
        mock_ollama_cls.assert_not_called()

        # Step 2: Cascade falls through — next adapter succeeds
        result = _make_adapter("anthropic", config=flagged_config)
        mock_anthropic_cls.assert_called_once()
        assert result is mock_provider
