"""Unit tests for ``job_finder.web.providers.anthropic_provider``.

Polish-review F2 (2026-05-26) rewired ``AnthropicProvider`` to call
``_run_oneshot`` directly rather than ``call_claude``. The tests now
mock ``_run_oneshot`` and assert against the envelope-parsing /
ModelResult shape (mirroring ``ClaudeCodeCLIProvider``'s tests).

Cost recording, budget gating, schema validation, and the schema-failure
retry all moved out of this adapter into the cascade layer
(``model_provider.call_model`` / ``_maybe_record_cost``); the tests for
those behaviors live in ``test_model_provider.py`` and
``test_anthropic_cost_attribution.py``.
"""

from unittest.mock import patch

import pytest

from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers.anthropic_provider import AnthropicProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return AnthropicProvider()


# ---------------------------------------------------------------------------
# Subclass / interface tests
# ---------------------------------------------------------------------------


def test_anthropic_provider_is_base_provider_subclass():
    assert issubclass(AnthropicProvider, BaseProvider)


# ---------------------------------------------------------------------------
# call() return value tests
# ---------------------------------------------------------------------------


def test_call_with_schema_uses_structured_output(provider):
    """When output_schema is provided and the envelope has structured_output, use it."""
    envelope = {
        "structured_output": {"score": 75},
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="You are a scorer.",
            messages=[{"role": "user", "content": "Score this."}],
            output_schema={"type": "object"},
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.0  # cost recording is the cascade layer's job
    assert result.provider == "anthropic"
    assert result.schema_valid is True
    assert result.input_tokens == 120
    assert result.output_tokens == 30
    assert result.model == "claude-haiku-4-5"


def test_call_with_schema_falls_back_to_result_string(provider):
    """When structured_output is absent, parse envelope['result'] as JSON."""
    envelope = {
        "result": '{"score": 80}',
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )

    assert result.data == {"score": 80}


def test_call_without_schema_wraps_freeform_text(provider):
    """No schema + non-JSON result → data is wrapped as {'text': ...}."""
    envelope = {"result": "Some freeform text reply.", "usage": {}}
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert result.data == {"text": "Some freeform text reply."}
    assert result.schema_valid is True


def test_call_without_schema_returns_dict_when_result_is_json_dict(provider):
    """No schema + JSON-dict result → data is the parsed dict (not wrapped)."""
    envelope = {"result": '{"key": "value"}', "usage": {}}
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert result.data == {"key": "value"}


def test_call_forwards_messages_last_content(provider):
    """Only messages[-1]['content'] is forwarded to _run_oneshot."""
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value={"structured_output": {"ok": True}, "usage": {}},
    ) as mock_oneshot:
        provider.call(
            model="claude-sonnet-4-6",
            system="System prompt",
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third — this is what gets sent"},
            ],
            output_schema={"type": "object"},
            timeout=30.0,
        )

    mock_oneshot.assert_called_once_with(
        model="claude-sonnet-4-6",
        system="System prompt",
        user_message="third — this is what gets sent",
        json_schema={"type": "object"},
        timeout=30.0,
    )


def test_call_uses_default_timeout_when_none(provider):
    """timeout=None → adapter falls back to 120s default."""
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value={"structured_output": {"ok": True}, "usage": {}},
    ) as mock_oneshot:
        provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )

    kwargs = mock_oneshot.call_args.kwargs
    assert kwargs["timeout"] == 120.0


def test_call_returns_zero_tokens_when_usage_missing(provider):
    """Missing usage dict → input/output_tokens default to 0."""
    envelope = {"structured_output": {"ok": True}}  # no "usage" key
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = provider.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
            output_schema={"type": "object"},
        )

    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_call_raises_on_empty_messages(provider):
    """Empty messages list → ValueError, no subprocess invocation."""
    with patch("job_finder.web.providers.anthropic_provider._run_oneshot") as mock_oneshot:
        with pytest.raises(ValueError, match="messages list must contain"):
            provider.call(
                model="claude-haiku-4-5",
                system="System",
                messages=[],
            )

    mock_oneshot.assert_not_called()


# ---------------------------------------------------------------------------
# Error propagation tests
# ---------------------------------------------------------------------------


def test_call_propagates_budget_exceeded(provider):
    """BudgetExceededError from _run_oneshot bubbles up unchanged."""
    from job_finder.web.claude_client import BudgetExceededError

    with (
        patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            side_effect=BudgetExceededError("credit exhausted"),
        ),
        pytest.raises(BudgetExceededError),
    ):
        provider.call(
            model="claude-sonnet-4-6",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )


def test_call_propagates_runtime_error(provider):
    """RuntimeError from _run_oneshot bubbles up unchanged."""
    with (
        patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            side_effect=RuntimeError("CLI failed"),
        ),
        pytest.raises(RuntimeError, match="CLI failed"),
    ):
        provider.call(
            model="claude-sonnet-4-6",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )


def test_call_propagates_timeout_error(provider):
    """TimeoutError from _run_oneshot bubbles up unchanged."""
    with (
        patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            side_effect=TimeoutError("subprocess timeout"),
        ),
        pytest.raises(TimeoutError, match="subprocess timeout"),
    ):
        provider.call(
            model="claude-sonnet-4-6",
            system="System",
            messages=[{"role": "user", "content": "Hi"}],
        )


# ---------------------------------------------------------------------------
# Constructor signature guard (U4)
# ---------------------------------------------------------------------------


def test_anthropic_provider_init_params():
    """Issue 303 (2026-06-10): AnthropicProvider accepts exactly one optional
    constructor param — ``provider_name`` — used to distinguish API-key
    transport (billed, "anthropic_api") from subscription-OAuth transport
    ($0, "anthropic").  No other args should be present; the dead-arg
    surface from U4 must not reappear.
    """
    import inspect

    from job_finder.web.providers.anthropic_provider import (
        ANTHROPIC_SUBSCRIPTION_PROVIDER,
    )

    sig = inspect.signature(AnthropicProvider.__init__)
    params = [p for p in sig.parameters if p != "self"]
    assert params == ["provider_name"], f"Unexpected constructor params: {params}"
    # Default should be the free subscription name.
    default = sig.parameters["provider_name"].default
    assert default == ANTHROPIC_SUBSCRIPTION_PROVIDER
