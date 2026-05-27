"""Unit tests for job_finder.web.model_provider.

Tests all five resolution paths for resolve_provider_config(),
frozen dataclass behavior of ModelResult, abstract enforcement for BaseProvider,
and the call_model() dispatcher (routing, schema retry, fallback, budget bypass,
cost recording).
"""

import os
import sqlite3
from unittest.mock import MagicMock, patch

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
    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    result = resolve_provider_config("score", config)
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-2.5-pro"


def test_resolve_provider_with_fallback():
    config = {
        "providers": {
            "primary": "gemini",
            "fallback_chain": ["ollama"],
        }
    }
    result = resolve_provider_config("score", config)
    assert result["provider"] == "gemini"
    assert len(result["fallback_chain"]) == 1
    assert result["fallback_chain"][0]["provider"] == "ollama"


def test_resolve_provider_missing_primary_raises():
    """2026-05-17 hotfix Fix 4a: the silent 'anthropic' default was the
    symptom that masked the Phase 40 schema regression. Missing
    providers.primary now raises ValueError so misconfiguration fails loud.
    """
    config = {}
    with pytest.raises(ValueError, match="providers.primary is not configured"):
        resolve_provider_config("score", config)


def test_resolve_provider_no_providers_section_raises():
    """Same as above — no providers section at all also raises (Fix 4a)."""
    config = {}
    with pytest.raises(ValueError, match="providers.primary is not configured"):
        resolve_provider_config("score", config)


def test_resolve_provider_tier_model_missing_uses_score_models():
    config = {
        "providers": {"primary": "ollama", "fallback_chain": []},
    }
    result = resolve_provider_config("score", config)
    assert result["provider"] == "ollama"
    assert result["model"] == "qwen2.5:14b"


def test_resolve_provider_score_tier_default():
    """v3.0 single tier name 'score' falls back to the Sonnet default
    when providers.primary is anthropic and no override is present.
    (Phase 40 hotfix 2026-05-17 removed the implicit anthropic default —
    the test now sets primary explicitly.)
    """
    config = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    result = resolve_provider_config("score", config)
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-4-6"


def test_resolve_provider_score_tier():
    """Phase 39: 'score' tier now translates to 'score' workload (sonnet), not opus.
    (Phase 40 hotfix 2026-05-17: explicit providers.primary required.)
    """
    config = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    result = resolve_provider_config("score", config)
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-4-6"


# --- Cascade config parsing tests (TEST-01) ---


def test_resolve_with_fallback_chain():
    config = {
        "providers": {
            "primary": "ollama",
            "fallback_chain": ["gemini", "anthropic"],
        }
    }
    result = resolve_provider_config("score", config)
    assert result["provider"] == "ollama"
    assert len(result["fallback_chain"]) == 2


def test_resolve_returns_daily_limits():
    config = {
        "providers": {
            "primary": "ollama",
            "fallback_chain": [],
            "daily_limits": {"ollama": 350, "gemini": 170},
        }
    }
    result = resolve_provider_config("score", config)
    assert result["daily_limits"] == {"ollama": 350, "gemini": 170}


def test_resolve_backward_compat_empty_chain():
    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    result = resolve_provider_config("score", config)
    assert result["fallback_chain"] == []
    assert result["daily_limits"] == {}
    assert result["provider"] == "gemini"


def test_resolve_chain_with_daily_limits_combined():
    config = {
        "providers": {
            "primary": "ollama",
            "fallback_chain": ["gemini"],
            "daily_limits": {"ollama": 350},
        }
    }
    result = resolve_provider_config("score", config)
    assert len(result["fallback_chain"]) == 1
    assert result["daily_limits"] == {"ollama": 350}


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
    """call_model routes to GeminiProvider when config says providers.score.provider=gemini."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)
    expected_result = _make_result(provider="gemini")

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = expected_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_make_adapter.assert_called_once_with(
        "gemini", conn, config, job_id=None, purpose=""
    )
    assert result.provider == "gemini"


def test_call_model_retries_on_schema_failure(tmp_path):
    """call_model retries once with schema errors appended to prompt on first validation failure."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)
    schema = {
        "type": "object",
        "required": ["score"],
        "properties": {"score": {"type": "integer"}},
    }

    # First call: missing required field — fails schema. Second call: passes.
    bad_result = _make_result(data={"wrong_key": 1})
    good_result = _make_result(data={"score": 80})

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.side_effect = [bad_result, good_result]
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "score",
            "sys",
            [{"role": "user", "content": "hi"}],
            conn,
            config,
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
            "primary": "gemini",
            "fallback_chain": ["anthropic"],
        }
    }
    conn = _migrated_conn(tmp_path)
    schema = {
        "type": "object",
        "required": ["score"],
        "properties": {"score": {"type": "integer"}},
    }

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

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "gemini":
            mock_gemini_adapter = MagicMock()
            mock_gemini_adapter.call.return_value = bad_result
            return mock_gemini_adapter
        elif provider_name == "anthropic":
            mock_anthropic_adapter = MagicMock()
            mock_anthropic_adapter.call.return_value = anthropic_result
            return mock_anthropic_adapter
        raise ValueError(f"unexpected call for {provider_name}")

    with (
        patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        result = call_model(
            "score",
            "sys",
            [{"role": "user", "content": "hi"}],
            conn,
            config,
            output_schema=schema,
        )

    assert result.provider == "anthropic"
    assert result.provider == "anthropic"
    assert result.data == {"score": 70}


@pytest.mark.parametrize(
    "provider_name,model_name",
    [
        ("gemini", "gemini-2.0-flash"),
        ("ollama", "llama3"),
        # Polish-review F2 (2026-05-26) moved anthropic into FREE_PROVIDERS
        # — the cascade dispatch uses the subscription-funded `claude -p` CLI.
        ("anthropic", "claude-haiku-4-5"),
    ],
)
def test_call_model_skips_budget_for_free_provider(provider_name, model_name, tmp_path):
    """call_model does NOT call cost_gate when provider is free."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": provider_name, "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate,
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider=provider_name)
        mock_make_adapter.return_value = mock_adapter

        call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


def test_call_model_checks_budget_for_paid_provider(tmp_path):
    """call_model calls cost_gate when provider is paid (not in FREE_PROVIDERS).

    Polish-review F2 (2026-05-26) moved anthropic into FREE_PROVIDERS, so the
    representative paid provider here is cerebras (in the production fallback
    chain; has a default score model unlike openrouter, which is config-only).
    """
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "cerebras", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True) as mock_cost_gate,
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider="cerebras")
        mock_make_adapter.return_value = mock_adapter

        call_model(
            "score", "sys", [{"role": "user", "content": "hi"}], conn, config
        )

    mock_cost_gate.assert_called_once_with(conn, config, "score")


def test_call_model_raises_cascade_exhausted_when_budget_blocks_only_provider(tmp_path):
    """Single-provider paid-provider cascade with budget gate closed exhausts the
    chain and raises ProviderCascadeExhaustedError.
    (Phase 40 hotfix 2026-05-17 Fix 4b: the back-compat path that raised
    BudgetExceededError directly was removed. The cascade skips over-budget
    providers — the canonical 'no provider' signal is exhaustion.)

    Polish-review F2 (2026-05-26) — uses cerebras because anthropic moved
    into FREE_PROVIDERS and never trips the budget gate any more.
    """
    from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model

    config = {"providers": {"primary": "cerebras", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)

    with patch("job_finder.web.model_provider.cost_gate", return_value=False):
        with pytest.raises(ProviderCascadeExhaustedError):
            call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, config)


def test_call_model_no_record_cost_for_anthropic(tmp_path):
    """call_model does NOT call the ``record_cost`` helper for anthropic results.

    Polish-review F2 (2026-05-26) — pre-F2, ``record_cost`` was skipped to
    avoid double-recording (AnthropicProvider went through call_claude
    which recorded internally). Post-F2 the adapter no longer touches
    call_claude; ``_maybe_record_cost`` writes the single cost row via a
    direct ``conn.execute(INSERT ...)`` rather than calling ``record_cost``.
    Either way, the public ``record_cost`` function should never be invoked
    from this cascade path.
    """
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)
    anthropic_result = ModelResult(
        data={"score": 70},
        cost_usd=0.01,
        input_tokens=0,
        output_tokens=0,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost") as mock_record_cost,
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = anthropic_result
        mock_make_adapter.return_value = mock_adapter

        call_model(
            "score", "sys", [{"role": "user", "content": "hi"}], conn, config
        )

    mock_record_cost.assert_not_called()


def test_call_model_records_cost_for_gemini(tmp_path):
    """call_model records $0 cost row for free providers like gemini."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)
    gemini_result = _make_result(provider="gemini", data={"score": 80})

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = gemini_result
        mock_make_adapter.return_value = mock_adapter

        call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, config)

    # Free providers record cost directly in DB at $0 (not via record_cost/compute_cost)
    row = conn.execute(
        "SELECT provider, cost_usd FROM scoring_costs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["provider"] == "gemini"
    assert row["cost_usd"] == 0.0


def test_call_model_raises_cascade_exhausted_on_single_provider_schema_failure(tmp_path):
    """Single-provider chain where the provider's output fails schema after
    retry exhausts the cascade and raises ProviderCascadeExhaustedError.
    (Phase 40 hotfix 2026-05-17 Fix 4b: the back-compat 'plain RuntimeError
    when no fallback' branch was removed; the cascade loop handles a
    one-entry chain the same way as a longer one.)
    """
    from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model

    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)
    schema = {
        "type": "object",
        "required": ["score"],
        "properties": {"score": {"type": "integer"}},
    }
    bad_result = _make_result(data={"wrong_key": 1})

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = bad_result
        mock_make_adapter.return_value = mock_adapter

        with pytest.raises(ProviderCascadeExhaustedError):
            call_model(
                "score",
                "sys",
                [{"role": "user", "content": "hi"}],
                conn,
                config,
                output_schema=schema,
            )


# ---------------------------------------------------------------------------
# Daily rate limit tracker tests (TEST-03)
# ---------------------------------------------------------------------------

from datetime import UTC

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
    _mp._daily_usage = {"ollama": 100}
    assert _mp._check_daily_limit("ollama", {"ollama": 350}) is True


def test_daily_limit_at_limit(_reset_daily_state):
    _mp._daily_usage = {"ollama": 350}
    assert _mp._check_daily_limit("ollama", {"ollama": 350}) is False


def test_daily_limit_over_limit(_reset_daily_state):
    _mp._daily_usage = {"ollama": 351}
    assert _mp._check_daily_limit("ollama", {"ollama": 350}) is False


def test_daily_limit_no_configured_limit(_reset_daily_state):
    assert _mp._check_daily_limit("gemini", {"ollama": 350}) is True


def test_daily_limit_provider_not_in_usage(_reset_daily_state):
    """Provider with a configured limit but no usage yet -> allowed."""
    assert _mp._check_daily_limit("ollama", {"ollama": 350}) is True


def test_daily_increment(_reset_daily_state):
    _mp._increment_usage("ollama")
    assert _mp._daily_usage["ollama"] == 1
    _mp._increment_usage("ollama")
    assert _mp._daily_usage["ollama"] == 2


def test_daily_increment_existing(_reset_daily_state):
    _mp._daily_usage = {"ollama": 5}
    _mp._increment_usage("ollama")
    assert _mp._daily_usage["ollama"] == 6


def test_daily_limit_resets_on_new_day(tmp_path, _reset_daily_state):
    from datetime import datetime

    conn = _migrated_conn(tmp_path)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job1", "test", "qwen2.5:14b", 100, 50, 0.0, now, "ollama"),
    )
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job2", "test", "qwen2.5:14b", 100, 50, 0.0, now, "ollama"),
    )
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job3", "test", "gemini-2.0-flash", 100, 50, 0.0, now, "gemini"),
    )
    conn.commit()
    _mp._init_usage_from_db(conn)
    assert _mp._daily_usage.get("ollama") == 2
    assert _mp._daily_usage.get("gemini") == 1
    assert _mp._usage_date == _mp._date.today().isoformat()


def test_ensure_usage_current_triggers_on_date_change(tmp_path, _reset_daily_state):
    conn = _migrated_conn(tmp_path)
    _mp._usage_date = "2020-01-01"  # stale date
    _mp._daily_usage = {"ollama": 999}  # stale data
    _mp._ensure_usage_current(conn)
    # After rollover, stale data should be gone (no scoring_costs rows for today in empty DB)
    assert _mp._daily_usage == {}
    assert _mp._usage_date == _mp._date.today().isoformat()


def test_ensure_usage_current_noop_same_day(tmp_path, _reset_daily_state):
    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._usage_date = today
    _mp._daily_usage = {"ollama": 42}
    _mp._ensure_usage_current(conn)
    # Should NOT reset — same day
    assert _mp._daily_usage == {"ollama": 42}


# ---------------------------------------------------------------------------
# Cascade execution tests (CASC-03, CASC-04, CASC-07, TEST-02)
# ---------------------------------------------------------------------------

# Shared config for cascade tests: ollama primary -> gemini -> anthropic
_CASCADE_CONFIG = {
    "providers": {
        "primary": "ollama",
        "fallback_chain": ["gemini", "anthropic"],
        "daily_limits": {"ollama": 350, "gemini": 170},
    }
}


def test_cascade_skips_exhausted_provider(tmp_path, _reset_daily_state):
    """When primary provider is at its daily limit, call_model cascades to second provider."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {"ollama": 350}  # at limit
    _mp._usage_date = today

    gemini_result = _make_result(provider="gemini")

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = gemini_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "score", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # First call should be to gemini (ollama was at limit and skipped)
    mock_make_adapter.assert_called_once()
    first_call_provider = mock_make_adapter.call_args[0][0]
    assert first_call_provider == "gemini"
    assert result.provider == "gemini"


def test_cascade_skips_missing_api_key(tmp_path, _reset_daily_state):
    """When first provider raises ValueError (missing API key), cascade skips to second."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    gemini_result = _make_result(provider="gemini")

    mock_gemini_adapter = MagicMock()
    mock_gemini_adapter.call.return_value = gemini_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "ollama":
            raise ValueError("Ollama unreachable")
        return mock_gemini_adapter

    with (
        patch(
            "job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect
        ) as mock_make_adapter,
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        result = call_model(
            "score", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # _make_adapter called twice: ollama (ValueError) then gemini
    assert mock_make_adapter.call_count == 2
    assert result.provider == "gemini"


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

    gemini_result = _make_result(provider="gemini")

    mock_ollama_adapter = MagicMock()
    mock_ollama_adapter.call.side_effect = http_429_error

    mock_gemini_adapter = MagicMock()
    mock_gemini_adapter.call.return_value = gemini_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "ollama":
            return mock_ollama_adapter
        return mock_gemini_adapter

    with (
        patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect),
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        result = call_model(
            "score", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG
        )

    # Ollama should be marked exhausted at its configured limit
    assert _mp._daily_usage.get("ollama") == 350
    assert result.provider == "gemini"


def test_cascade_all_exhausted_raises(tmp_path, _reset_daily_state):
    """When all providers in chain are exhausted or unavailable, RuntimeError is raised."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    # ollama and gemini both at their daily limits; anthropic has no key
    _mp._daily_usage = {"ollama": 350, "gemini": 170}
    _mp._usage_date = today

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "anthropic":
            raise ValueError("no key")
        # ollama and gemini are blocked by limit check, never reach _make_adapter
        raise ValueError(f"unexpected call for {provider_name}")

    with (
        patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect),
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        with pytest.raises(RuntimeError, match="exhausted"):
            call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG)


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

    # ollama always returns schema-invalid data (no "score" key)
    bad_result = _make_result(data={"wrong_key": 1})
    # gemini returns valid data
    good_result = _make_result(provider="gemini", data={"score": 80})

    mock_ollama_adapter = MagicMock()
    mock_ollama_adapter.call.return_value = bad_result  # always invalid

    mock_gemini_adapter = MagicMock()
    mock_gemini_adapter.call.return_value = good_result

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "ollama":
            return mock_ollama_adapter
        return mock_gemini_adapter

    original_messages = [{"role": "user", "content": "hi"}]

    with (
        patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect),
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        result = call_model(
            "score",
            "sys",
            original_messages,
            conn,
            _CASCADE_CONFIG,
            output_schema=schema,
        )

    # The gemini adapter's first call should receive original messages (not augmented)
    gemini_first_call_messages = mock_gemini_adapter.call.call_args_list[0][0][2]
    assert "Schema validation errors" not in gemini_first_call_messages[-1]["content"]
    assert result.data == {"score": 80}


# ---------------------------------------------------------------------------
# Cascade prompt variant injection tests (CASC-05)
# ---------------------------------------------------------------------------

# Config with prompt_variant on a chain entry
_CASCADE_VARIANT_CONFIG = {
    "providers": {
        "primary": "ollama",
        "fallback_chain": ["gemini", "anthropic"],
        "daily_limits": {"ollama": 350, "gemini": 170},
    }
}


def test_cascade_prompt_variant_no_longer_overrides_system(tmp_path, _reset_daily_state):
    """Plan 4 Commit E removed PROMPT_VARIANTS along with score_evaluator.

    The prompt_variant cascade key is now ignored -- every entry uses the
    caller's system prompt verbatim. This test pins the new behavior so a
    future regression that re-introduces variant lookups is caught.
    """
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {"ollama": 350}  # force cascade to gemini
    _mp._usage_date = today

    gemini_result = _make_result(provider="gemini")

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = gemini_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "score",
            "original system prompt",
            [{"role": "user", "content": "hi"}],
            conn,
            _CASCADE_VARIANT_CONFIG,
        )

    assert result.provider == "gemini"
    call_args = mock_adapter.call.call_args
    actual_system = call_args[0][1]  # positional: model, system, messages, ...
    assert actual_system == "original system prompt"


# ---------------------------------------------------------------------------
# Phase 39: _PROVIDER_DEFAULTS + _VALID_WORKLOADS membership + legacy-tier
# translation regression tests (STRANGE-PROV-01).
# ---------------------------------------------------------------------------
from job_finder.web.model_provider import (
    _PROVIDER_DEFAULTS,
    _VALID_WORKLOADS,
)


def test_provider_defaults_contains_all_eight_providers():
    assert set(_PROVIDER_DEFAULTS) >= {
        "claude_code_cli",
        "anthropic",
        "gemini",
        "gemini_cli",
        "ollama",
        "local_bundled",
        "groq",
        "cerebras",
    }


def test_valid_workloads_set_is_quick_score_triage():
    assert _VALID_WORKLOADS == frozenset({"quick", "score", "triage"})


def test_provider_defaults_has_no_legacy_tier_keys_in_inner_maps():
    flat_keys: set[str] = set()
    for mapping in _PROVIDER_DEFAULTS.values():
        flat_keys.update(mapping.keys())
    assert flat_keys.isdisjoint(
        {"low", "mid", "high", "scoring", "haiku", "sonnet", "opus"}
    )


@pytest.mark.xfail(reason="Fixed in Plan 40-02: _LEGACY_TIER_MAP removed in Phase 40")
def test_legacy_tier_map_translates_quick_to_quick():
    assert _LEGACY_TIER_MAP["quick"] == "quick"
    assert _LEGACY_TIER_MAP["score"] == "score"
    assert _LEGACY_TIER_MAP["score"] == "score"
    assert _LEGACY_TIER_MAP["score"] == "score"
    # Forward-compat passthrough
    assert _LEGACY_TIER_MAP["quick"] == "quick"
    assert _LEGACY_TIER_MAP["score"] == "score"
    assert _LEGACY_TIER_MAP["triage"] == "triage"


@pytest.mark.xfail(reason="Fixed in Plan 40-02: legacy tier names removed in Phase 40")
def test_resolve_quick_tier_returns_quick_workload_model_for_anthropic():
    cfg = {"providers": {"quick": {"provider": "anthropic", "fallback_chain": []}}}
    result = resolve_provider_config("quick", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["quick"]


@pytest.mark.xfail(reason="Fixed in Plan 40-02: legacy tier names removed in Phase 40")
def test_resolve_score_tier_returns_score_workload_model_for_anthropic():
    cfg = {"providers": {"score": {"provider": "anthropic", "fallback_chain": []}}}
    result = resolve_provider_config("score", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["score"]


def test_resolve_forward_compat_quick_passes_through():
    cfg = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    result = resolve_provider_config("quick", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["quick"]


def test_resolve_with_explicit_model_still_overrides_defaults():
    # Sanity: caller-provided model wins over _PROVIDER_DEFAULTS lookup.
    cfg = {
        "providers": {
            "primary": "anthropic",
            "overrides": {"anthropic": {"quick": "claude-custom-99"}},
            "fallback_chain": [],
        }
    }
    result = resolve_provider_config("quick", cfg)
    assert result["model"] == "claude-custom-99"


def test_cascade_primary_entry_uses_original_system(tmp_path, _reset_daily_state):
    """When primary cascade entry has no prompt_variant, adapter.call uses the caller's system prompt."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    ollama_result = _make_result(provider="ollama")

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = ollama_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "score",
            "custom system prompt",
            [{"role": "user", "content": "hi"}],
            conn,
            _CASCADE_VARIANT_CONFIG,
        )

    # ollama was selected (primary, no prompt_variant)
    assert result.provider == "ollama"
    # adapter.call should have been called with the original system prompt unchanged
    call_args = mock_adapter.call.call_args
    actual_system = call_args[0][1]  # positional arg: model, system, messages, ...
    assert actual_system == "custom system prompt", (
        f"Primary entry with no prompt_variant should use caller's system prompt unchanged, got: {actual_system!r}"
    )


# ---------------------------------------------------------------------------
# _SUPPORTED_PROVIDERS / _FREE_PROVIDERS / is_supported_provider_name tests
# ---------------------------------------------------------------------------

from job_finder.web.model_provider import (
    _SUPPORTED_PROVIDERS,
    ProviderCascadeExhaustedError,
    _make_adapter,
    is_supported_provider_name,
    tier_has_configured_provider,
)


def test_is_supported_provider_name_typo():
    assert is_supported_provider_name("gorq") is False


def test_is_supported_provider_name_unknown():
    assert is_supported_provider_name("deepseek") is False


# ---------------------------------------------------------------------------
# _make_adapter registration tests
# ---------------------------------------------------------------------------


def test_make_adapter_unknown_provider_raises():
    """_make_adapter raises ValueError for unknown provider names."""
    with pytest.raises(ValueError, match="Unknown provider"):
        _make_adapter("deepseek", conn=None, config={})


def test_make_adapter_conn_none_for_non_anthropic():
    """conn=None is accepted for non-Anthropic providers."""
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider.__init__", return_value=None
    ):
        adapter = _make_adapter("ollama", conn=None, config={})
    assert adapter is not None


# ---------------------------------------------------------------------------
# _SUPPORTED_PROVIDERS sync-enforcement test (mandatory per plan)
# ---------------------------------------------------------------------------


def test_supported_providers_all_wired_in_make_adapter():
    """Every name in _SUPPORTED_PROVIDERS must be wired into _make_adapter().

    Patch all required provider env vars so supported providers don't fail
    for unrelated credential reasons. The assertion is: _make_adapter(name)
    must not raise ValueError("Unknown provider: ...").
    """
    env_vars = {
        "GEMINI_API_KEY": "test",
        # Anthropic is reachable when ANTHROPIC_API_KEY is set
        # (post-2026-05-21 the SDK is not constructed; the predicate is env-only).
        "ANTHROPIC_API_KEY": "test",
    }

    for provider_name in _SUPPORTED_PROVIDERS:
        with patch.dict("os.environ", env_vars, clear=False):
            try:
                adapter = _make_adapter(provider_name, conn=None, config={})
            except ValueError as exc:
                if "Unknown provider" in str(exc):
                    pytest.fail(
                        f"{provider_name!r} is in _SUPPORTED_PROVIDERS but not wired "
                        f"into _make_adapter() dispatch chain"
                    )
                # Other ValueError (e.g. missing API key for a provider not covered
                # in env_vars) is acceptable — the provider IS wired in but has a
                # constructor-time prerequisite we didn't mock.
                continue
            except (RuntimeError, ImportError):
                # Constructor-time readiness check (e.g. Ollama unreachable) — provider
                # IS wired in, just not locally available.
                continue
            # If we reach here, _make_adapter returned without error —
            # adapter must not be None (would mean missing dispatch branch).
            assert adapter is not None, (
                f"{provider_name!r} is in _SUPPORTED_PROVIDERS and _make_adapter() "
                f"did not raise, but returned None — missing dispatch branch"
            )


# ---------------------------------------------------------------------------
# ProviderCascadeExhaustedError tests
# ---------------------------------------------------------------------------


def test_cascade_exhausted_error_is_runtime_error():
    assert issubclass(ProviderCascadeExhaustedError, RuntimeError)


def test_cascade_raises_exhausted_error_not_runtime_error(tmp_path, _reset_daily_state):
    """When all cascade providers are exhausted, ProviderCascadeExhaustedError is raised."""
    from job_finder.web.model_provider import call_model

    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {"ollama": 350, "gemini": 170}
    _mp._usage_date = today

    def make_adapter_side_effect(provider_name, *args, **kwargs):
        if provider_name == "anthropic":
            raise ValueError("no key")
        raise ValueError(f"unexpected call for {provider_name}")

    with (
        patch("job_finder.web.model_provider._make_adapter", side_effect=make_adapter_side_effect),
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
    ):
        with pytest.raises(ProviderCascadeExhaustedError):
            call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, _CASCADE_CONFIG)


# Deleted in 2026-05-17 hotfix (Fix 4b):
# test_non_cascade_schema_failure_raises_plain_runtime_error pinned the
# back-compat path's "plain RuntimeError, not ProviderCascadeExhaustedError"
# distinction. That path no longer exists — every call_model invocation now
# goes through the cascade loop, and schema failure on a one-entry chain
# raises ProviderCascadeExhaustedError (covered by
# test_call_model_raises_cascade_exhausted_on_single_provider_schema_failure
# above).


# ---------------------------------------------------------------------------
# tier_has_configured_provider tests
# ---------------------------------------------------------------------------


def test_tier_has_provider_non_anthropic_with_key():
    """Non-Anthropic primary + valid constructor -> True."""
    config = {"providers": {"primary": "ollama", "fallback_chain": []}}
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider.__init__", return_value=None
    ):
        assert tier_has_configured_provider("quick", config) is True


def test_tier_has_provider_anthropic_only_no_client():
    """Anthropic-only chain + no ANTHROPIC_API_KEY env -> False.

    Post-2026-05-21 DASHBOARD-SDK-REFACTOR: availability is detected via
    ``is_anthropic_available()`` (env var check). Clearing both
    ``ANTHROPIC_API_KEY`` and ``JF_ANTHROPIC_API_KEY`` is the canonical
    "Anthropic not configured" state.
    """
    config = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "JF_ANTHROPIC_API_KEY")
    }
    with patch.dict("os.environ", env, clear=True):
        assert tier_has_configured_provider("quick", config) is False


def test_tier_has_provider_anthropic_only_with_client():
    """Anthropic-only chain + ANTHROPIC_API_KEY set -> True.

    Post-2026-05-21 DASHBOARD-SDK-REFACTOR: availability is detected via
    ``is_anthropic_available()`` (env var check), not a passed SDK client.
    """
    config = {"providers": {"primary": "anthropic", "fallback_chain": []}}
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
        assert tier_has_configured_provider("quick", config) is True


def test_tier_has_provider_typo_no_client():
    """Typo provider name -> False.
    The function's contract is a predicate; under 2026-05-17 hotfix
    tier_has_configured_provider catches the ValueError that
    resolve_provider_config raises for unknown providers and returns False
    (the docstring's stated behavior). Prior code raised; the test body now
    matches the function's documented predicate semantics.
    """
    config = {"providers": {"primary": "gorq", "fallback_chain": []}}
    assert tier_has_configured_provider("quick", config) is False


def test_tier_has_provider_missing_api_key():
    """Recognized provider name but missing required API key -> False."""
    config = {"providers": {"primary": "gemini", "fallback_chain": []}}
    with patch.dict("os.environ", {}, clear=True):
        assert tier_has_configured_provider("quick", config) is False


def test_tier_has_provider_mixed_chain_primary_bad_fallback_good():
    """Mixed chain where primary is misconfigured but fallback is locally valid -> True.

    Post-2026-05-21 DASHBOARD-SDK-REFACTOR: the Anthropic-fallback branch now
    consults ``is_anthropic_available()`` (env var) instead of a passed SDK
    client. Clear all env vars except ANTHROPIC_API_KEY so Gemini fails to
    instantiate (no GEMINI_API_KEY) but Anthropic resolves.
    """
    config = {
        "providers": {
            "primary": "gemini",
            "fallback_chain": ["anthropic"],
        }
    }
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        assert tier_has_configured_provider("quick", config) is True


def test_tier_has_provider_conn_none_accepted():
    """conn=None accepted without error (validates signature change)."""
    config = {"providers": {"primary": "ollama", "fallback_chain": []}}
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider.__init__", return_value=None
    ):
        result = tier_has_configured_provider("quick", config, conn=None)
    assert result is True


def test_tier_has_provider_ollama_unreachable():
    """Ollama configured but unreachable -> False (operational check)."""
    config = {"providers": {"primary": "ollama", "fallback_chain": []}}
    with patch(
        "job_finder.web.providers.ollama_provider.OllamaProvider.__init__",
        side_effect=RuntimeError("Connection refused"),
    ):
        assert tier_has_configured_provider("quick", config) is False


# ---------------------------------------------------------------------------
# Cascade + Ollama/Gemini integration tests
# ---------------------------------------------------------------------------


def test_cascade_ollama_primary_gemini_fallback(tmp_path, _reset_daily_state):
    """Ollama primary -> Gemini fallback -> Anthropic last-resort routes correctly."""
    from job_finder.web.model_provider import call_model

    config = {
        "providers": {
            "primary": "ollama",
            "fallback_chain": ["gemini", "anthropic"],
        }
    }
    conn = _migrated_conn(tmp_path)
    today = _mp._date.today().isoformat()
    _mp._daily_usage = {}
    _mp._usage_date = today

    ollama_result = ModelResult(
        data={"score": 80},
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider._ensure_usage_current"),
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = ollama_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "quick",
            "sys",
            [{"role": "user", "content": "hi"}],
            conn,
            config,
        )

    assert result.provider == "ollama"
    mock_make_adapter.assert_called_once()
    assert mock_make_adapter.call_args[0][0] == "ollama"


def test_backward_compat_single_ollama_no_cascade(tmp_path):
    """Single-provider Ollama config with no fallback_chain routes correctly."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": "ollama", "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)

    ollama_result = ModelResult(
        data={"score": 80},
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate", return_value=True),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = ollama_result
        mock_make_adapter.return_value = mock_adapter

        result = call_model(
            "quick",
            "sys",
            [{"role": "user", "content": "hi"}],
            conn,
            config,
        )

    assert result.provider == "ollama"
    # Budget gate should be skipped (ollama is free)


@pytest.mark.parametrize(
    "provider_name,model_name",
    [
        ("ollama", "qwen2.5:14b"),
        ("gemini", "gemini-2.0-flash"),
    ],
)
def test_call_model_skips_budget_for_free_providers(provider_name, model_name, tmp_path):
    """call_model does NOT call cost_gate for ollama and gemini."""
    from job_finder.web.model_provider import call_model

    config = {"providers": {"primary": provider_name, "fallback_chain": []}}
    conn = _migrated_conn(tmp_path)

    with (
        patch("job_finder.web.model_provider._make_adapter") as mock_make_adapter,
        patch("job_finder.web.model_provider.cost_gate") as mock_cost_gate,
        patch("job_finder.web.model_provider.record_cost"),
    ):
        mock_adapter = MagicMock()
        mock_adapter.call.return_value = _make_result(provider=provider_name)
        mock_make_adapter.return_value = mock_adapter

        call_model("score", "sys", [{"role": "user", "content": "hi"}], conn, config)

    mock_cost_gate.assert_not_called()


# ---------------------------------------------------------------------------
# U6 guard: _maybe_record_cost rejects empty provider
# ---------------------------------------------------------------------------


def test_maybe_record_cost_rejects_empty_provider(tmp_path):
    """U6 guard: _maybe_record_cost raises on ModelResult.provider='' to
    prevent default-leak rows in scoring_costs.

    scoring_costs.provider has DEFAULT 'anthropic' (m018), which is in
    FREE_PROVIDERS post-F2 — so an INSERT that fails to set provider
    would silently disappear from cost rollups.
    """
    from job_finder.web.model_provider import _maybe_record_cost

    conn = _migrated_conn(tmp_path)
    try:
        bad_result = ModelResult(
            data={"x": 1},
            cost_usd=0.0,
            input_tokens=10,
            output_tokens=5,
            model="some-model",
            provider="",  # ← the trap
            schema_valid=True,
        )
        with pytest.raises(ValueError, match="provider must be"):
            _maybe_record_cost(bad_result, conn, "j1", "purpose")
    finally:
        conn.close()
