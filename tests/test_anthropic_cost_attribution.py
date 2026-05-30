"""Cost-attribution regression guard for the polish-review F2 change.

Before F2 (2026-05-26), a cascade Anthropic call wrote a ``scoring_costs``
row with ``provider="claude_cli"`` (via ``call_claude``'s internal
``record_cost``). After F2 the adapter dispatches directly to
``_run_oneshot`` and the cascade's ``_maybe_record_cost`` writes a single
row with ``provider="anthropic"``. ``"anthropic"`` was added to
``FREE_PROVIDERS`` so the budget gate continues to treat the
CLI-subscription transport as $0.

These tests pin the post-F2 contract so a future refactor cannot quietly
revert the attribution name or accidentally re-introduce double-recording.
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

    db_path = str(tmp_path / "anthropic_cost_attribution.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# FREE_PROVIDERS membership
# ---------------------------------------------------------------------------


def test_anthropic_is_in_free_providers():
    """F2 (2026-05-26) — the cascade Anthropic transport is the subscription
    CLI ($0), so the budget gate must exclude it."""
    from job_finder.web.claude_client import FREE_PROVIDERS

    assert "anthropic" in FREE_PROVIDERS


def test_claude_cli_remains_in_free_providers():
    """The legacy call_claude path still records provider='claude_cli';
    it must also stay exempt."""
    from job_finder.web.claude_client import FREE_PROVIDERS

    assert "claude_cli" in FREE_PROVIDERS


# ---------------------------------------------------------------------------
# Cascade-routed Anthropic call writes exactly one row with provider="anthropic"
# ---------------------------------------------------------------------------


def test_cascade_anthropic_writes_single_row_with_anthropic_provider(migrated_conn):
    """After F2: one cascade Anthropic call → exactly one scoring_costs row
    with provider='anthropic' and cost_usd=0.0 (FREE_PROVIDERS membership)."""
    from job_finder.web.model_provider import _maybe_record_cost
    from job_finder.web.providers.anthropic_provider import AnthropicProvider

    # _run_oneshot envelope shape mirrors what Anthropic CLI returns.
    envelope = {
        "structured_output": {"score": 80},
        "usage": {"input_tokens": 150, "output_tokens": 40},
    }

    adapter = AnthropicProvider()
    with patch(
        "job_finder.web.providers.anthropic_provider._run_oneshot",
        return_value=envelope,
    ):
        result = adapter.call(
            model="claude-haiku-4-5",
            system="System",
            messages=[{"role": "user", "content": "Score this."}],
            output_schema={"type": "object"},
        )

    # AnthropicProvider returns provider="anthropic" (was "anthropic" pre-F2 too;
    # what changed is the row attribution downstream).
    assert result.provider == "anthropic"
    assert result.input_tokens == 150
    assert result.output_tokens == 40

    # No row should have been written yet — adapter no longer records cost.
    pre = migrated_conn.execute("SELECT COUNT(*) AS n FROM scoring_costs").fetchone()
    assert pre["n"] == 0

    # The cascade layer writes the cost row.
    _maybe_record_cost(result, migrated_conn, job_id="j1", purpose="score_job")

    rows = migrated_conn.execute(
        "SELECT job_id, purpose, model, input_tokens, output_tokens, cost_usd, provider "
        "FROM scoring_costs"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "j1"
    assert row["purpose"] == "score_job"
    assert row["model"] == "claude-haiku-4-5"
    assert row["input_tokens"] == 150
    assert row["output_tokens"] == 40
    # FREE_PROVIDERS membership → cost_usd is forced to 0.0
    assert row["cost_usd"] == 0.0
    # The key invariant: provider attribution flipped from "claude_cli" to "anthropic".
    assert row["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# cost_gate excludes anthropic spend
# ---------------------------------------------------------------------------


def test_cost_gate_excludes_anthropic_rows(migrated_conn):
    """cost_gate must not count anthropic-row spend toward the budget cap."""
    from datetime import UTC, datetime

    from job_finder.web.claude_client import cost_gate

    ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
    # Stuff a hypothetical $99 anthropic row in (would never happen post-F2
    # because _maybe_record_cost forces 0.0 for free providers — but if it
    # did, the budget gate must still ignore it).
    migrated_conn.executemany(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
        "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("j1", "score_job", "x", 1, 1, 99.00, ts, "anthropic"),
        ],
    )
    migrated_conn.commit()

    # 99 > 1.00 budget cap would block — but anthropic is excluded, so it passes.
    config = {"scoring": {"daily_budget_usd": 1.00}}
    assert cost_gate(migrated_conn, config, model_tier="score") is True


# ---------------------------------------------------------------------------
# get_monthly_provider_breakdown excludes anthropic
# ---------------------------------------------------------------------------


def test_monthly_provider_breakdown_excludes_anthropic(migrated_conn):
    """The paid-providers table must not list anthropic rows post-F2."""
    from datetime import UTC, datetime

    from job_finder.web.claude_client import get_monthly_provider_breakdown

    ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
    migrated_conn.executemany(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, "
        "cost_usd, timestamp, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("j1", "score_job", "x", 1, 1, 0.50, ts, "anthropic"),
            ("j2", "score_job", "x", 1, 1, 0.25, ts, "openrouter"),
        ],
    )
    migrated_conn.commit()

    result = get_monthly_provider_breakdown(migrated_conn)
    providers = {r["provider"] for r in result}
    assert "anthropic" not in providers
    assert "openrouter" in providers
