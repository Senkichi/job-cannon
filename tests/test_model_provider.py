"""Unit tests for job_finder.web.model_provider.

Tests all five resolution paths for resolve_provider_config(),
frozen dataclass behavior of ModelResult, abstract enforcement for BaseProvider,
and the call_model() dispatcher (routing, schema retry, fallback, budget bypass,
cost recording).
"""

import sqlite3
from unittest.mock import MagicMock, patch, call

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
    assert result == {"provider": "gemini", "model": "gemini-2.5-pro", "fallback": None, "fallback_chain": [], "daily_limits": {}}


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
    assert result == {"provider": "anthropic", "model": "claude-sonnet-4-6", "fallback": None, "fallback_chain": [], "daily_limits": {}}


def test_resolve_provider_no_providers_section():
    config = {}
    result = resolve_provider_config("sonnet", config)
    assert result == {"provider": "anthropic", "model": "claude-sonnet-4-6", "fallback": None, "fallback_chain": [], "daily_limits": {}}


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
    assert result == {"provider": "anthropic", "model": "claude-haiku-4-5", "fallback": None, "fallback_chain": [], "daily_limits": {}}


def test_resolve_provider_opus_tier():
    config = {}
    result = resolve_provider_config("opus", config)
    assert result == {"provider": "anthropic", "model": "claude-opus-4-6", "fallback": None, "fallback_chain": [], "daily_limits": {}}


# --- Cascade config parsing tests (TEST-01) ---


def test_resolve_with_fallback_chain():
    config = {"providers": {"sonnet": {
        "provider": "cerebras",
        "model": "qwen-3-235b-a22b-instruct-2507",
        "fallback_chain": [
            {"provider": "groq", "model": "meta-llama/llama-4-scout-17b-16e-instruct"},
            {"provider": "ollama", "model": "qwen2.5:14b"},
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        ],
    }}}
    result = resolve_provider_config("sonnet", config)
    assert result["fallback_chain"] == config["providers"]["sonnet"]["fallback_chain"]
    assert result["provider"] == "cerebras"


def test_resolve_returns_daily_limits():
    config = {"providers": {"sonnet": {"provider": "cerebras", "model": "qwen-3-235b"}, "daily_limits": {"cerebras": 350, "groq": 170}}}
    result = resolve_provider_config("sonnet", config)
    assert result["daily_limits"] == {"cerebras": 350, "groq": 170}


def test_resolve_backward_compat_empty_chain():
    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    result = resolve_provider_config("sonnet", config)
    assert result["fallback_chain"] == []
    assert result["daily_limits"] == {}
    assert result["provider"] == "gemini"  # existing behavior preserved


def test_resolve_chain_with_daily_limits_combined():
    config = {"providers": {
        "sonnet": {"provider": "cerebras", "model": "qwen-3-235b",
                   "fallback_chain": [{"provider": "groq", "model": "scout"}]},
        "daily_limits": {"cerebras": 350},
    }}
    result = resolve_provider_config("sonnet", config)
    assert result["fallback_chain"] == [{"provider": "groq", "model": "scout"}]
    assert result["daily_limits"] == {"cerebras": 350}


# ---------------------------------------------------------------------------
# call_model() dispatcher tests
# ---------------------------------------------------------------------------

def _make_result(provider="gemini", data=None):
    """Return a ModelResult for use in call_model() tests."""
    return ModelResult(
        data=data or {"score": 80},
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="gemini-2.0-flash",
        provider=provider,
        schema_valid=True,
    )


def _migrated_conn(tmp_path):
    """Return an in-memory SQLite connection with the scoring_costs table."""
    from job_finder.web.db_migrate import run_migrations
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_call_model_routes_to_configured_provider(tmp_path):
    """call_model routes to GeminiProvider when config says providers.sonnet.provider=gemini."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    conn = _migrated_conn(tmp_path)
    expected_result = _make_result(provider="gemini")

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = expected_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_make_adapter.assert_called_once_with("gemini", None, conn, config, job_id=None, purpose="")
    assert result.provider == "gemini"


def test_call_model_retries_on_schema_failure(tmp_path):
    """call_model retries once with schema errors appended to prompt on first validation failure."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    conn = _migrated_conn(tmp_path)
    schema = {"type": "object", "required": ["score"], "properties": {"score": {"type": "integer"}}}

    # First call: missing required field — fails schema. Second call: passes.
    bad_result = _make_result(data={"wrong_key": 1})
    good_result = _make_result(data={"score": 80})

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.side_effect = [bad_result, good_result]
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config,
            output_schema=schema,
        )

    assert mock_adapter.call.call_count == 2
    # The second call must have augmented messages (error text appended)
    second_call_messages = mock_adapter.call.call_args_list[1][0][2]
    assert "Schema validation errors" in second_call_messages[-1]["content"]
    assert result.data == {"score": 80}


def test_call_model_fallback_to_anthropic(tmp_path):
    """call_model falls back to AnthropicProvider when retry also fails schema validation."""
    from job_finder.web.model_provider import call_model

    config = {
        "providers": {
            "sonnet": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "fallback": "anthropic",
            }
        }
    }
    conn = _migrated_conn(tmp_path)
    schema = {"type": "object", "required": ["score"], "properties": {"score": {"type": "integer"}}}

    bad_result = _make_result(data={"wrong_key": 1})
    anthropic_result = _make_result(provider="anthropic", data={"score": 70})
    anthropic_result = ModelResult(
        data={"score": 70},
        cost_usd=0.01,
        input_tokens=0,
        output_tokens=0,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )

    mock_client = MagicMock()

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"), \
         patch("job_finder.web.providers.anthropic_provider.AnthropicProvider") as mock_anthropic_cls:
        mock_gemini_adapter = MagicMock()
        mock_gemini_adapter.call.return_value = bad_result
        mock_make_adapter.return_value = mock_gemini_adapter

        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.call.return_value = anthropic_result
        mock_anthropic_cls.return_value = mock_anthropic_instance

        result = call_model(
            "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config,
            output_schema=schema,
            client=mock_client,
        )

    mock_anthropic_cls.assert_called_once_with(client=mock_client, conn=conn, config=config, job_id=None, purpose="")
    assert result.provider == "anthropic"
    assert result.data == {"score": 70}


def test_call_model_skips_budget_for_gemini(tmp_path):
    """call_model does NOT call cost_gate when provider is gemini."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="gemini")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_skips_budget_for_ollama(tmp_path):
    """call_model does NOT call cost_gate when provider is ollama."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "ollama", "model": "llama3"}}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="ollama")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_skips_budget_for_ollm(tmp_path):
    """call_model does NOT call cost_gate when provider is ollm."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "ollm", "model": "llama3-8B-chat"}}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="ollm")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_skips_budget_for_openrouter(tmp_path):
    """call_model does NOT call cost_gate when provider is openrouter."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "openrouter", "model": "qwen/qwen3-coder:free"}}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="openrouter")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_skips_budget_for_sambanova(tmp_path):
    """call_model does NOT call cost_gate when provider is sambanova."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "sambanova", "model": "Qwen3-235B-A22B"}}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="sambanova")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_checks_budget_for_anthropic(tmp_path):
    """call_model calls cost_gate when provider is anthropic."""
    from job_finder.web.model_provider import call_model

    config = {}  # default: anthropic
    conn = _migrated_conn(tmp_path)
    mock_client = MagicMock()

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True) as mock_cost_gate, \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="anthropic")
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config, client=mock_client)

    mock_cost_gate.assert_called_once_with(conn, config, "sonnet")


def test_call_model_raises_budget_exceeded(tmp_path):
    """call_model raises BudgetExceededError when cost_gate returns False for anthropic."""
    from job_finder.web.model_provider import call_model
    from job_finder.web.claude_client import BudgetExceededError

    config = {}  # default: anthropic
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider.cost_gate", return_value=False):
        with pytest.raises(BudgetExceededError):
            call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)


def test_call_model_no_record_cost_for_anthropic(tmp_path):
    """call_model does NOT call record_cost for anthropic provider (avoids double-recording)."""
    from job_finder.web.model_provider import call_model

    config = {}  # default: anthropic
    conn = _migrated_conn(tmp_path)
    mock_client = MagicMock()
    anthropic_result = ModelResult(
        data={"score": 70},
        cost_usd=0.01,
        input_tokens=0,
        output_tokens=0,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost") as mock_record_cost:
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = anthropic_result
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config, client=mock_client)

    mock_record_cost.assert_not_called()


def test_call_model_records_cost_for_gemini(tmp_path):
    """call_model calls record_cost with provider='gemini' for gemini calls."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    conn = _migrated_conn(tmp_path)
    gemini_result = _make_result(provider="gemini", data={"score": 80})

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost") as mock_record_cost:
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = gemini_result
        mock_make_adapter.return_value = mock_adapter

        call_model("sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_record_cost.assert_called_once()
    call_kwargs = mock_record_cost.call_args
    # record_cost(conn, job_id, purpose, model, input_tokens, output_tokens, provider=...)
    assert call_kwargs.kwargs.get("provider") == "gemini" or call_kwargs.args[-1] == "gemini"


def test_call_model_raises_on_no_fallback(tmp_path):
    """call_model raises RuntimeError when retry fails and no fallback configured."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}}}
    conn = _migrated_conn(tmp_path)
    schema = {"type": "object", "required": ["score"], "properties": {"score": {"type": "integer"}}}
    bad_result = _make_result(data={"wrong_key": 1})

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = bad_result
        mock_make_adapter.return_value = mock_adapter

        with pytest.raises(RuntimeError, match="no fallback"):
            call_model(
                "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, config,
                output_schema=schema,
            )
