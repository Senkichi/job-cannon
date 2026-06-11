"""Tests for Issue 303 — anthropic API-key transport treated as paid.

Acceptance criteria:
  AC-1. With an API key set, a cascade call records cost_usd > 0 attributed
        to "anthropic_api" (a paid provider name).
  AC-2. cost_gate trips once the daily budget is exceeded (BudgetExceededError
        reachable via the cascade when anthropic_api is the only provider and
        daily spend >= cap).
  AC-3. Subscription-CLI transport (no API key, claude binary on PATH) still
        records $0 and provider="anthropic" (FREE_PROVIDERS member).
  AC-4. is_anthropic_api_key_transport() returns True iff an API key env var
        is present.
  AC-5. is_anthropic_available() returns True for API-key transport OR when
        claude binary is on PATH (subscription path), False when neither.
  AC-6. AnthropicProvider constructed with provider_name="anthropic_api"
        emits a ModelResult with provider="anthropic_api".
  AC-7. _make_adapter("anthropic") with API key set → provider="anthropic_api";
        without API key but with claude on PATH → provider="anthropic".
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_conn(tmp_path):
    """Build a migrated test DB and return an open connection."""
    from job_finder.web.db_migrate import run_migrations

    db_path = str(tmp_path / "test_303.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _now_ts() -> str:
    # Real "now" in naive UTC ISO (production storage format) so cost rows land
    # inside cost_gate's local-day window on any timezone. A hardcoded
    # "...T12:00:00Z" on now(UTC).date() falls outside the window in the evening
    # Pacific, when UTC has already rolled to the next calendar day.
    from job_finder.json_utils import utc_now_iso

    return utc_now_iso()


# ---------------------------------------------------------------------------
# AC-4: is_anthropic_api_key_transport()
# ---------------------------------------------------------------------------


class TestIsAnthropicApiKeyTransport:
    def test_true_when_anthropic_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("JF_ANTHROPIC_API_KEY", raising=False)
        from job_finder.web.claude_client import is_anthropic_api_key_transport

        assert is_anthropic_api_key_transport() is True

    def test_true_when_jf_key_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("JF_ANTHROPIC_API_KEY", "sk-jf-test")
        from job_finder.web.claude_client import is_anthropic_api_key_transport

        assert is_anthropic_api_key_transport() is True

    def test_false_when_neither_key_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JF_ANTHROPIC_API_KEY", raising=False)
        from job_finder.web.claude_client import is_anthropic_api_key_transport

        assert is_anthropic_api_key_transport() is False


# ---------------------------------------------------------------------------
# AC-5: is_anthropic_available()
# ---------------------------------------------------------------------------


class TestIsAnthropicAvailable:
    def test_true_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from job_finder.web.claude_client import is_anthropic_available

        assert is_anthropic_available() is True

    def test_true_when_claude_binary_on_path(self, monkeypatch):
        """Subscription-only path: no API key but claude CLI is installed."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JF_ANTHROPIC_API_KEY", raising=False)
        with patch(
            "job_finder.web.claude_client.shutil.which", return_value="/usr/local/bin/claude"
        ):
            from job_finder.web.claude_client import is_anthropic_available

            assert is_anthropic_available() is True

    def test_false_when_neither_key_nor_binary(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JF_ANTHROPIC_API_KEY", raising=False)
        with patch("job_finder.web.claude_client.shutil.which", return_value=None):
            from job_finder.web.claude_client import is_anthropic_available

            assert is_anthropic_available() is False


# ---------------------------------------------------------------------------
# AC-6: AnthropicProvider emits correct provider name
# ---------------------------------------------------------------------------


class TestAnthropicProviderName:
    def _envelope(self):
        return {
            "structured_output": {"score": 80},
            "usage": {"input_tokens": 100, "output_tokens": 40},
        }

    def test_default_provider_name_is_subscription(self):
        """Default constructor → provider='anthropic' (FREE_PROVIDERS member)."""
        from job_finder.web.providers.anthropic_provider import (
            ANTHROPIC_SUBSCRIPTION_PROVIDER,
            AnthropicProvider,
        )

        p = AnthropicProvider()
        with patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            return_value=self._envelope(),
        ):
            result = p.call(
                model="claude-haiku-4-5",
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert result.provider == ANTHROPIC_SUBSCRIPTION_PROVIDER

    def test_api_key_provider_name(self):
        """Constructed with 'anthropic_api' → result.provider='anthropic_api'."""
        from job_finder.web.providers.anthropic_provider import (
            ANTHROPIC_API_KEY_PROVIDER,
            AnthropicProvider,
        )

        p = AnthropicProvider(provider_name=ANTHROPIC_API_KEY_PROVIDER)
        with patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            return_value=self._envelope(),
        ):
            result = p.call(
                model="claude-haiku-4-5",
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert result.provider == ANTHROPIC_API_KEY_PROVIDER


# ---------------------------------------------------------------------------
# AC-7: _make_adapter selects transport-aware provider name
# ---------------------------------------------------------------------------


class TestMakeAdapterTransportSelection:
    def test_api_key_set_yields_anthropic_api_adapter(self, monkeypatch):
        """_make_adapter("anthropic") with API key → AnthropicProvider("anthropic_api")."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from job_finder.web.model_provider import _make_adapter
        from job_finder.web.providers.anthropic_provider import ANTHROPIC_API_KEY_PROVIDER

        adapter = _make_adapter("anthropic")
        assert adapter._provider_name == ANTHROPIC_API_KEY_PROVIDER

    def test_no_api_key_yields_subscription_adapter(self, monkeypatch):
        """_make_adapter("anthropic") without API key but with claude on PATH → 'anthropic'."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JF_ANTHROPIC_API_KEY", raising=False)
        with patch(
            "job_finder.web.claude_client.shutil.which", return_value="/usr/local/bin/claude"
        ):
            from job_finder.web.model_provider import _make_adapter
            from job_finder.web.providers.anthropic_provider import ANTHROPIC_SUBSCRIPTION_PROVIDER

            adapter = _make_adapter("anthropic")
            assert adapter._provider_name == ANTHROPIC_SUBSCRIPTION_PROVIDER

    def test_anthropic_api_name_also_dispatches(self, monkeypatch):
        """_make_adapter("anthropic_api") with API key → same paid adapter."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        from job_finder.web.model_provider import _make_adapter
        from job_finder.web.providers.anthropic_provider import ANTHROPIC_API_KEY_PROVIDER

        adapter = _make_adapter("anthropic_api")
        assert adapter._provider_name == ANTHROPIC_API_KEY_PROVIDER


# ---------------------------------------------------------------------------
# AC-1: API-key transport records cost_usd > 0 with provider="anthropic_api"
# ---------------------------------------------------------------------------


class TestApiKeyTransportRecordsCost:
    def _paid_envelope(self, input_tokens: int = 1000, output_tokens: int = 500) -> dict:
        return {
            "structured_output": {"score": 75},
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }

    def test_adapter_computes_real_cost_for_api_key_transport(self):
        """AnthropicProvider('anthropic_api').call() computes cost_usd > 0 from token counts.

        This is the honest AC-1 path: the adapter itself must compute real cost
        (via compute_cost / MODEL_PRICING) so that _maybe_record_cost receives a
        non-zero cost_usd and records it.  Previously parse_oneshot_envelope hardcoded
        cost_usd=0.0 and the adapter never overrode it.
        """
        from job_finder.web.claude_client import compute_cost
        from job_finder.web.providers.anthropic_provider import (
            ANTHROPIC_API_KEY_PROVIDER,
            AnthropicProvider,
        )

        model = "claude-sonnet-4-6"
        input_tokens = 1000
        output_tokens = 500
        expected_cost = compute_cost(model, input_tokens, output_tokens)
        assert expected_cost > 0, "MODEL_PRICING must have entry for claude-sonnet-4-6"

        p = AnthropicProvider(provider_name=ANTHROPIC_API_KEY_PROVIDER)
        with patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            return_value=self._paid_envelope(input_tokens, output_tokens),
        ):
            result = p.call(
                model=model,
                system="sys",
                messages=[{"role": "user", "content": "score this"}],
                output_schema={"type": "object"},
            )

        assert result.provider == ANTHROPIC_API_KEY_PROVIDER
        assert result.cost_usd == pytest.approx(expected_cost)
        assert result.input_tokens == input_tokens
        assert result.output_tokens == output_tokens

    def test_adapter_api_key_result_lands_in_db_with_nonzero_cost(self, migrated_conn):
        """End-to-end: adapter result for anthropic_api → _maybe_record_cost → cost_usd > 0 in DB.

        Verifies the full path: AnthropicProvider computes real cost, then
        _maybe_record_cost (which uses result.cost_usd for paid providers) writes it.
        """
        from job_finder.web.claude_client import compute_cost
        from job_finder.web.model_provider import _maybe_record_cost
        from job_finder.web.providers.anthropic_provider import (
            ANTHROPIC_API_KEY_PROVIDER,
            AnthropicProvider,
        )

        model = "claude-haiku-4-5"
        input_tokens = 2000
        output_tokens = 300
        expected_cost = compute_cost(model, input_tokens, output_tokens)

        p = AnthropicProvider(provider_name=ANTHROPIC_API_KEY_PROVIDER)
        with patch(
            "job_finder.web.providers.anthropic_provider._run_oneshot",
            return_value=self._paid_envelope(input_tokens, output_tokens),
        ):
            result = p.call(
                model=model,
                system="sys",
                messages=[{"role": "user", "content": "score this"}],
                output_schema={"type": "object"},
            )

        _maybe_record_cost(result, migrated_conn, job_id="j-e2e", purpose="scoring")

        row = migrated_conn.execute(
            "SELECT provider, cost_usd FROM scoring_costs WHERE job_id = ?", ("j-e2e",)
        ).fetchone()
        assert row is not None
        assert row["provider"] == ANTHROPIC_API_KEY_PROVIDER
        assert row["cost_usd"] == pytest.approx(expected_cost)
        assert row["cost_usd"] > 0

    def test_cost_recorded_for_api_key_transport(self, migrated_conn):
        """_maybe_record_cost uses result.cost_usd as-is for paid providers (anthropic_api)."""
        from job_finder.web.claude_client import FREE_PROVIDERS
        from job_finder.web.model_provider import ModelResult, _maybe_record_cost
        from job_finder.web.providers.anthropic_provider import ANTHROPIC_API_KEY_PROVIDER

        assert ANTHROPIC_API_KEY_PROVIDER not in FREE_PROVIDERS, (
            "anthropic_api must NOT be in FREE_PROVIDERS — that would make cost $0"
        )

        # claude-sonnet-4-6: $3.00/M input + $15.00/M output
        # 1000 input + 500 output = $0.003 + $0.0075 = $0.0105
        result = ModelResult(
            data={"score": 75},
            cost_usd=0.0105,
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-6",
            provider=ANTHROPIC_API_KEY_PROVIDER,
            schema_valid=True,
        )
        _maybe_record_cost(result, migrated_conn, job_id="j-test", purpose="scoring")

        row = migrated_conn.execute(
            "SELECT provider, cost_usd FROM scoring_costs WHERE job_id = ?", ("j-test",)
        ).fetchone()
        assert row is not None
        assert row["provider"] == ANTHROPIC_API_KEY_PROVIDER
        assert row["cost_usd"] == pytest.approx(0.0105)

    def test_subscription_transport_records_zero(self, migrated_conn):
        """Subscription transport (provider='anthropic') records cost_usd=0."""
        from job_finder.web.model_provider import ModelResult, _maybe_record_cost

        result = ModelResult(
            data={"score": 75},
            cost_usd=0.0,
            input_tokens=1000,
            output_tokens=500,
            model="claude-sonnet-4-6",
            provider="anthropic",
            schema_valid=True,
        )
        _maybe_record_cost(result, migrated_conn, job_id="j-sub", purpose="scoring")

        row = migrated_conn.execute(
            "SELECT provider, cost_usd FROM scoring_costs WHERE job_id = ?", ("j-sub",)
        ).fetchone()
        assert row is not None
        assert row["provider"] == "anthropic"
        assert row["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# AC-2: cost_gate trips for anthropic_api spend; subscription spend is ignored
# ---------------------------------------------------------------------------


class TestCostGateAnthropicApi:
    def test_cost_gate_trips_on_anthropic_api_spend(self, migrated_conn):
        """Daily spend from 'anthropic_api' rows counts toward the budget cap."""
        from job_finder.web.claude_client import cost_gate

        ts = _now_ts()
        migrated_conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
            "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "scoring", "claude-sonnet-4-6", 1000, 500, 5.00, ts, "anthropic_api"),
        )
        migrated_conn.commit()

        # $5 spend > $1 cap → gate should block
        config = {"scoring": {"daily_budget_usd": 1.00}}
        assert cost_gate(migrated_conn, config, model_tier="score") is False

    def test_cost_gate_ignores_subscription_anthropic_rows(self, migrated_conn):
        """Subscription rows (provider='anthropic') do not count toward the cap."""
        from job_finder.web.claude_client import cost_gate

        ts = _now_ts()
        migrated_conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
            "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "scoring", "claude-sonnet-4-6", 1000, 500, 99.00, ts, "anthropic"),
        )
        migrated_conn.commit()

        # $99 subscription spend — must not trip a $1 cap
        config = {"scoring": {"daily_budget_usd": 1.00}}
        assert cost_gate(migrated_conn, config, model_tier="score") is True

    def test_cost_gate_quick_tier_always_passes(self, migrated_conn):
        """quick-tier calls bypass the budget gate regardless of spend."""
        from job_finder.web.claude_client import cost_gate

        ts = _now_ts()
        migrated_conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
            "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "scoring", "claude-haiku-4-5", 1000, 500, 100.00, ts, "anthropic_api"),
        )
        migrated_conn.commit()

        config = {"scoring": {"daily_budget_usd": 1.00}}
        assert cost_gate(migrated_conn, config, model_tier="quick") is True


# ---------------------------------------------------------------------------
# AC-3 (broader): FREE_PROVIDERS membership invariants
# ---------------------------------------------------------------------------


class TestFreeProvidersInvariants:
    def test_anthropic_api_not_in_free_providers(self):
        """anthropic_api must NOT be in FREE_PROVIDERS (Issue 303 core fix)."""
        from job_finder.web.claude_client import FREE_PROVIDERS

        assert "anthropic_api" not in FREE_PROVIDERS

    def test_anthropic_still_in_free_providers(self):
        """anthropic (subscription path) stays in FREE_PROVIDERS."""
        from job_finder.web.claude_client import FREE_PROVIDERS

        assert "anthropic" in FREE_PROVIDERS

    def test_anthropic_api_in_supported_providers(self):
        """anthropic_api must be registered in _SUPPORTED_PROVIDERS."""
        from job_finder.web.model_provider import _SUPPORTED_PROVIDERS

        assert "anthropic_api" in _SUPPORTED_PROVIDERS

    def test_anthropic_api_in_provider_defaults(self):
        """anthropic_api must have model defaults for quick and score workloads."""
        from job_finder.web.model_provider import _PROVIDER_DEFAULTS

        assert "anthropic_api" in _PROVIDER_DEFAULTS
        assert "quick" in _PROVIDER_DEFAULTS["anthropic_api"]
        assert "score" in _PROVIDER_DEFAULTS["anthropic_api"]
