"""Unit tests for job_finder.web.providers.anthropic_provider.

Tests cover:
- Subclass relationship (AnthropicProvider is a BaseProvider)
- call() returns correct ModelResult fields
- All parameters forwarded correctly to call_claude()
- output_schema forwarding (with and without)
- BudgetExceededError propagation
- Generic exception propagation
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.anthropic_provider import AnthropicProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def mock_conn():
    return MagicMock(spec=sqlite3.Connection)


@pytest.fixture
def provider(mock_client, mock_conn):
    return AnthropicProvider(client=mock_client, conn=mock_conn, config={})


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_anthropic_provider_is_base_provider_subclass():
    assert issubclass(AnthropicProvider, BaseProvider)


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_returns_model_result(provider):
    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        return_value=({"score": 75}, 0.003),
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.003
    assert result.provider == "anthropic"
    assert result.schema_valid is True
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_call_passes_all_params_to_call_claude(provider, mock_client, mock_conn):
    messages = [{"role": "user", "content": "Test"}]
    schema = {"type": "object"}

    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        return_value=({"result": "ok"}, 0.001),
    ) as mock_call:
        provider.call(
            model="claude-sonnet-4-6",
            system="System prompt",
            messages=messages,
            output_schema=schema,
            max_tokens=2048,
            timeout=30.0,
        )

    mock_call.assert_called_once_with(
        client=mock_client,
        model="claude-sonnet-4-6",
        system="System prompt",
        messages=messages,
        output_schema=schema,
        conn=mock_conn,
        config={},
        max_tokens=2048,
        timeout=30.0,
    )


def test_call_with_schema(provider):
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}

    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        return_value=({"score": 90}, 0.005),
    ) as mock_call:
        result = provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=schema,
        )

    _, call_kwargs = mock_call.call_args
    assert mock_call.call_args.kwargs["output_schema"] == schema
    assert result.data == {"score": 90}


def test_call_without_schema(provider):
    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        return_value=({"text": "plain response"}, 0.002),
    ) as mock_call:
        provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema=None,
        )

    assert mock_call.call_args.kwargs["output_schema"] is None


# ---------------------------------------------------------------------------
# Error propagation tests
# ---------------------------------------------------------------------------


def test_call_propagates_budget_exceeded(provider):
    from job_finder.web.claude_client import BudgetExceededError

    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        side_effect=BudgetExceededError("Monthly budget cap reached"),
    ):
        with pytest.raises(BudgetExceededError):
            provider.call(
                model="claude-sonnet-4-6",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )


def test_call_propagates_api_error(provider):
    with patch(
        "job_finder.web.providers.anthropic_provider.call_claude",
        side_effect=RuntimeError("API connection failed"),
    ):
        with pytest.raises(RuntimeError, match="API connection failed"):
            provider.call(
                model="claude-sonnet-4-6",
                system="System",
                messages=[{"role": "user", "content": "Hi"}],
            )
