import pytest
from job_finder.web.model_provider import (
    _PROVIDER_DEFAULTS,
    _VALID_WORKLOADS,
    resolve_workload_routing,
)


def test_valid_workloads_are_quick_score_triage():
    assert _VALID_WORKLOADS == {"quick", "score", "triage"}


def test_provider_defaults_cover_all_workloads_for_all_providers():
    expected = {"claude_code_cli", "gemini", "gemini_cli", "ollama", "anthropic", "local_bundled"}
    assert set(_PROVIDER_DEFAULTS) >= expected
    for provider, mapping in _PROVIDER_DEFAULTS.items():
        assert set(mapping) >= {"quick", "score"}, f"{provider} missing quick/score"


def test_resolve_routing_claude_code_cli_quick_returns_haiku():
    routing = resolve_workload_routing(
        workload="quick",
        config={"providers": {"primary": "claude_code_cli", "fallback_chain": []}},
    )
    assert routing["primary"]["provider"] == "claude_code_cli"
    assert "haiku" in routing["primary"]["model"]


def test_resolve_routing_claude_code_cli_score_returns_sonnet():
    routing = resolve_workload_routing(
        workload="score",
        config={"providers": {"primary": "claude_code_cli", "fallback_chain": []}},
    )
    assert "sonnet" in routing["primary"]["model"]


def test_resolve_routing_triage_uses_quick_model():
    routing_q = resolve_workload_routing("quick", {"providers": {"primary": "ollama", "fallback_chain": []}})
    routing_t = resolve_workload_routing("triage", {"providers": {"primary": "ollama", "fallback_chain": []}})
    assert routing_t["primary"]["model"] == routing_q["primary"]["model"]


def test_resolve_routing_cascade_per_workload():
    routing = resolve_workload_routing(
        workload="score",
        config={"providers": {"primary": "claude_code_cli", "fallback_chain": ["gemini", "anthropic"]}},
    )
    chain = [routing["primary"]] + routing["fallback"]
    assert [e["provider"] for e in chain] == ["claude_code_cli", "gemini", "anthropic"]
    # Each entry uses its provider's `score` default
    assert all(e["model"] for e in chain)


def test_resolve_routing_honors_overrides():
    routing = resolve_workload_routing(
        workload="score",
        config={
            "providers": {
                "primary": "ollama",
                "fallback_chain": [],
                "overrides": {"ollama": {"score": "qwen2.5:32b"}},
            }
        },
    )
    assert routing["primary"]["model"] == "qwen2.5:32b"


def test_unknown_workload_raises():
    with pytest.raises(ValueError, match="Unknown workload"):
        resolve_workload_routing("low", {"providers": {"primary": "ollama"}})


def test_legacy_tier_names_no_longer_in_defaults():
    # Sanity: the haiku/sonnet/opus/low/mid/high aliases are gone.
    flat_keys: set[str] = set()
    for mapping in _PROVIDER_DEFAULTS.values():
        flat_keys.update(mapping.keys())
    assert flat_keys.isdisjoint({"low", "mid", "high", "haiku", "sonnet", "opus"})
