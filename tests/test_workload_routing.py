import pytest

from job_finder.web.model_provider import (
    _PROVIDER_DEFAULTS,
    _SUPPORTED_PROVIDERS,
    _VALID_WORKLOADS,
    resolve_workload_routing,
)


def test_valid_workloads_are_quick_score_triage():
    assert {"quick", "score", "triage"} == _VALID_WORKLOADS


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
    routing_q = resolve_workload_routing(
        "quick", {"providers": {"primary": "ollama", "fallback_chain": []}}
    )
    routing_t = resolve_workload_routing(
        "triage", {"providers": {"primary": "ollama", "fallback_chain": []}}
    )
    assert routing_t["primary"]["model"] == routing_q["primary"]["model"]


def test_resolve_routing_cascade_per_workload():
    routing = resolve_workload_routing(
        workload="score",
        config={
            "providers": {"primary": "claude_code_cli", "fallback_chain": ["gemini", "anthropic"]}
        },
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


# ---------------------------------------------------------------------------
# Provider-registry invariants — drift guards
# ---------------------------------------------------------------------------


def test_provider_defaults_subset_of_supported_providers():
    """Every key in _PROVIDER_DEFAULTS must be in _SUPPORTED_PROVIDERS.

    Guards against a defaults entry for a provider that hasn't been wired
    into _make_adapter(), which would produce silent no-ops at dispatch time.
    """
    unknown = set(_PROVIDER_DEFAULTS) - _SUPPORTED_PROVIDERS
    assert not unknown, (
        f"_PROVIDER_DEFAULTS contains provider(s) not in _SUPPORTED_PROVIDERS: {unknown!r}. "
        "Add them to _SUPPORTED_PROVIDERS or remove the defaults entry."
    )


def test_openrouter_is_supported_but_not_in_defaults():
    """openrouter is an eval-judge-only adapter and must NOT appear in _PROVIDER_DEFAULTS.

    It is registered in _SUPPORTED_PROVIDERS so _make_adapter() can dispatch
    audit calls, but placing it in _PROVIDER_DEFAULTS would silently make it
    a candidate for the production cascade.  See openrouter_provider.py.
    """
    assert "openrouter" in _SUPPORTED_PROVIDERS, (
        "openrouter vanished from _SUPPORTED_PROVIDERS — update this test or openrouter_provider.py"
    )
    assert "openrouter" not in _PROVIDER_DEFAULTS, (
        "openrouter must NOT be in _PROVIDER_DEFAULTS (eval-judge only; excluded from prod cascade)"
    )
