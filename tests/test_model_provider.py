"""Unit tests for job_finder.web.model_provider.

Tests all five resolution paths for resolve_provider_config(),
frozen dataclass behavior of ModelResult, abstract enforcement for BaseProvider,
and the call_model() dispatcher (routing, schema retry, fallback, budget bypass,
cost recording).
"""

import sqlite3
from unittest.mock import MagicMock, patch, call

import pytest
import requests

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


# ---------------------------------------------------------------------------
# Daily rate limit tracker tests (TEST-03)
# ---------------------------------------------------------------------------

import job_finder.web.model_provider as _mp


@pytest.fixture(autouse=False)
def _reset_daily_state():
    """Reset module-level daily usage state before and after each test."""
    _mp._daily_usage = {}
    _mp._usage_date = ""
    yield
    _mp._daily_usage = {}
    _mp._usage_date = ""


def test_daily_limit_under_limit(_reset_daily_state):
    _mp._daily_usage = {"cerebras": 100}
    assert _mp._check_daily_limit("cerebras", {"cerebras": 350}) is True


def test_daily_limit_at_limit(_reset_daily_state):
    _mp._daily_usage = {"cerebras": 350}
    assert _mp._check_daily_limit("cerebras", {"cerebras": 350}) is False


def test_daily_limit_over_limit(_reset_daily_state):
    _mp._daily_usage = {"cerebras": 351}
    assert _mp._check_daily_limit("cerebras", {"cerebras": 350}) is False


def test_daily_limit_no_configured_limit(_reset_daily_state):
    assert _mp._check_daily_limit("ollama", {"cerebras": 350}) is True


def test_daily_limit_provider_not_in_usage(_reset_daily_state):
    """Provider with a configured limit but no usage yet -> allowed."""
    assert _mp._check_daily_limit("cerebras", {"cerebras": 350}) is True


def test_daily_increment(_reset_daily_state):
    _mp._increment_usage("cerebras")
    assert _mp._daily_usage["cerebras"] == 1
    _mp._increment_usage("cerebras")
    assert _mp._daily_usage["cerebras"] == 2


def test_daily_increment_existing(_reset_daily_state):
    _mp._daily_usage = {"cerebras": 5}
    _mp._increment_usage("cerebras")
    assert _mp._daily_usage["cerebras"] == 6


def test_daily_limit_resets_on_new_day(tmp_path, _reset_daily_state):
    from datetime import datetime, timezone
    conn = _migrated_conn(tmp_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job1", "test", "qwen", 100, 50, 0.0, now, "cerebras"),
    )
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job2", "test", "qwen", 100, 50, 0.0, now, "cerebras"),
    )
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job3", "test", "scout", 100, 50, 0.0, now, "groq"),
    )
    conn.commit()
    _mp._init_usage_from_db(conn)
    assert _mp._daily_usage.get("cerebras") == 2
    assert _mp._daily_usage.get("groq") == 1
    assert _mp._usage_date == _mp._date.today().isoformat()


def test_ensure_usage_current_triggers_on_date_change(tmp_path, _reset_daily_state):
    conn = _migrated_conn(tmp_path)
    _mp._usage_date = "2020-01-01"  # stale date
    _mp._daily_usage = {"cerebras": 999}  # stale data
    _mp._ensure_usage_current(conn)
    # After rollover, stale data should be gone (no scoring_costs rows for today in empty DB)
    assert _mp._daily_usage == {}
    assert _mp._usage_date == _mp._date.today().isoformat()


def test_ensure_usage_current_noop_same_day(tmp_path, _reset_daily_state):
    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._usage_date = today
    _mp._daily_usage = {"cerebras": 42}
    _mp._ensure_usage_current(conn)
    # Should NOT reset — same day
    assert _mp._daily_usage == {"cerebras": 42}


# ---------------------------------------------------------------------------
# Cascade execution tests (CASC-03, CASC-04, CASC-07, TEST-02)
# ---------------------------------------------------------------------------

# Shared config for cascade tests: cerebras primary -> groq -> anthropic
_CASCADE_CONFIG = {
    "providers": {
        "sonnet": {
            "provider": "cerebras",
            "model": "qwen-3-235b",
            "fallback_chain": [
                {"provider": "groq", "model": "scout"},
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
        },
        "daily_limits": {"cerebras": 350, "groq": 170},
    }
}


def test_cascade_skips_exhausted_provider(tmp_path, _reset_daily_state):
    """When primary provider is at its daily limit, call_model cascades to second provider."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {"cerebras": 350}  # at limit
    _mp._usage_date = today

    groq_result = _make_result(provider="groq")

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = groq_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # First call should be to groq (cerebras was at limit and skipped)
    mock_make_adapter.assert_called_once()
    first_call_provider = mock_make_adapter.call_args[0][0]
    assert first_call_provider == "groq"
    assert result.provider == "groq"


def test_cascade_skips_missing_api_key(tmp_path, _reset_daily_state):
    """When first provider raises ValueError (missing API key), cascade skips to second."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    groq_result = _make_result(provider="groq")

    mock_groq_adapter = MagicMock()
    mock_groq_adapter.call.return_value = groq_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "cerebras":
            raise ValueError("API key not set")
        return mock_groq_adapter

    with patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect) as mock_make_adapter, \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):

        result = call_model(
            "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # _make_adapter called twice: cerebras (ValueError) then groq
    assert mock_make_adapter.call_count == 2
    assert result.provider == "groq"


def test_cascade_429_marks_exhausted(tmp_path, _reset_daily_state):
    """A 429 from a provider marks it exhausted at its daily limit and cascades to next."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    # Build a requests.HTTPError with status_code 429
    mock_response = MagicMock()
    mock_response.status_code = 429
    http_429_error = requests.HTTPError(response=mock_response)

    groq_result = _make_result(provider="groq")

    mock_cerebras_adapter = MagicMock()
    mock_cerebras_adapter.call.side_effect = http_429_error

    mock_groq_adapter = MagicMock()
    mock_groq_adapter.call.return_value = groq_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "cerebras":
            return mock_cerebras_adapter
        return mock_groq_adapter

    with patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect), \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):

        result = call_model(
            "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # Cerebras should be marked exhausted at its configured limit
    assert _mp._daily_usage.get("cerebras") == 350
    assert result.provider == "groq"


def test_cascade_all_exhausted_raises(tmp_path, _reset_daily_state):
    """When all providers in chain are exhausted or unavailable, RuntimeError is raised."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    # cerebras and groq both at their daily limits; anthropic has no key
    _mp._daily_usage = {"cerebras": 350, "groq": 170}
    _mp._usage_date = today

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "anthropic":
            raise ValueError("no key")
        # cerebras and groq are blocked by limit check, never reach _make_adapter
        raise ValueError(f"unexpected call for {provider_name}")

    with patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect), \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):

        with pytest.raises(RuntimeError, match="exhausted"):
            call_model(
                "sonnet", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
            )


def test_cascade_preserves_original_messages(tmp_path, _reset_daily_state):
    """Each provider in cascade receives the original unaugmented messages (not previous provider's errors)."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    schema = {
        "type": "object",
        "required": ["score"],
        "properties": {"score": {"type": "integer"}},
    }

    # cerebras always returns schema-invalid data (no "score" key)
    bad_result = _make_result(data={"wrong_key": 1})
    # groq returns valid data
    good_result = _make_result(provider="groq", data={"score": 80})

    mock_cerebras_adapter = MagicMock()
    mock_cerebras_adapter.call.return_value = bad_result  # always invalid

    mock_groq_adapter = MagicMock()
    mock_groq_adapter.call.return_value = good_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "cerebras":
            return mock_cerebras_adapter
        return mock_groq_adapter

    original_messages = [{"role": "user", "content": "hi"}]

    with patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect), \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):

        result = call_model(
            "sonnet", "sys", original_messages, conn, _CASCADE_CONFIG,
            output_schema=schema,
        )

    # The groq adapter's first call should receive original messages (not augmented)
    groq_first_call_messages = mock_groq_adapter.call.call_args_list[0][0][2]
    assert "Schema validation errors" not in groq_first_call_messages[-1]["content"]
    assert result.data == {"score": 80}


# ---------------------------------------------------------------------------
# Cascade prompt variant injection tests (CASC-05)
# ---------------------------------------------------------------------------

# Config with prompt_variant on a chain entry
_CASCADE_VARIANT_CONFIG = {
    "providers": {
        "sonnet": {
            "provider": "cerebras",
            "model": "qwen-3-235b",
            "fallback_chain": [
                {"provider": "groq", "model": "scout", "prompt_variant": "fewshot-distribution"},
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            ],
        },
        "daily_limits": {"cerebras": 350, "groq": 170},
    }
}


def test_cascade_prompt_variant_overrides_system(tmp_path, _reset_daily_state):
    """When cascade entry has prompt_variant, adapter.call uses the variant's system prompt."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {"cerebras": 350}  # force cascade to groq
    _mp._usage_date = today

    groq_result = _make_result(provider="groq")

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = groq_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "sonnet", "original system prompt",
            [{"role": "user", "content": "hi"}], conn, _CASCADE_VARIANT_CONFIG,
        )

    # groq was selected (cerebras exhausted)
    assert result.provider == "groq"
    # adapter.call should have been called with the fewshot-distribution variant, not the original
    call_args = mock_adapter.call.call_args
    actual_system = call_args[0][1]  # positional arg: model, system, messages, ...
    assert "Expected Score Distribution" in actual_system, (
        "fewshot-distribution variant should include distribution instructions"
    )
    assert "original system prompt" not in actual_system, (
        "original system prompt should be replaced by prompt_variant"
    )


def test_cascade_primary_entry_uses_original_system(tmp_path, _reset_daily_state):
    """When primary cascade entry has no prompt_variant, adapter.call uses the caller's system prompt."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    cerebras_result = _make_result(provider="cerebras")

    with patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter, \
         patch("job_finder.web.model_provider._ensure_usage_current"), \
         patch("job_finder.web.model_provider.cost_gate", return_value=True), \
         patch("job_finder.web.model_provider.record_cost"):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = cerebras_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "sonnet", "custom system prompt",
            [{"role": "user", "content": "hi"}], conn, _CASCADE_VARIANT_CONFIG,
        )

    # cerebras was selected (primary, no prompt_variant=None)
    assert result.provider == "cerebras"
    # adapter.call should have been called with the original system prompt unchanged
    call_args = mock_adapter.call.call_args
    actual_system = call_args[0][1]  # positional arg: model, system, messages, ...
    assert actual_system == "custom system prompt", (
        f"Primary entry with no prompt_variant should use caller's system prompt unchanged, got: {actual_system!r}"
    )
