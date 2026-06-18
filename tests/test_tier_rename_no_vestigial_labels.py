"""Regression guard for the tier-name rename (haiku/sonnet/opus -> quick/score/triage).

Issue #448. The rename of job-cannon workload tiers is complete (Phase 39/40):
routing keys on the workload classes ``quick``/``score``/``triage`` and no
production module uses ``haiku``/``sonnet``/``opus`` as a routing/tier key.

The surviving ``haiku``/``sonnet``/``opus`` tokens in production code are NOT
tier labels — they are real Anthropic SDK model identifiers and the Claude
CLI's own ``--model`` shortnames. These tests pin both invariants so a future
reader cannot "finish the rename" by renaming those strings (which would break
live inference and cost accounting).
"""

from job_finder.web import claude_client, model_provider


def test_valid_workloads_are_quick_score_triage():
    """Routing tiers are the renamed workload classes, not haiku/sonnet/opus."""
    assert frozenset({"quick", "score", "triage"}) == model_provider._VALID_WORKLOADS


def test_provider_defaults_use_workload_keys_not_tier_labels():
    """Per-provider default maps key on workload classes, never on tier labels."""
    for provider, defaults in model_provider._PROVIDER_DEFAULTS.items():
        keys = set(defaults)
        assert keys <= {"quick", "score", "triage"}, (
            f"{provider} defaults carry non-workload key(s): {keys}"
        )
        assert not (keys & {"haiku", "sonnet", "opus"}), (
            f"{provider} defaults still use a vestigial tier label: {keys}"
        )

    # Anthropic specifically: quick + score only (no triage entry; no tier labels).
    assert set(model_provider._PROVIDER_DEFAULTS["anthropic"]) == {"quick", "score"}


def test_cli_model_aliases_preserved():
    """Class-B invariant: CLI --model shortnames are NOT renamed to low/mid/high.

    Renaming these values breaks the ``claude --model <alias>`` subprocess call.
    """
    assert claude_client._CLI_MODEL_ALIASES == {
        "claude-haiku-4-5": "haiku",
        "claude-sonnet-4-6": "sonnet",
        "claude-opus-4-6": "opus",
    }


def test_model_pricing_keys_are_sdk_model_ids():
    """Class-A invariant: pricing keys are SDK model-IDs, not tier labels.

    Renaming these keys to low/mid/high breaks cost accounting (lookups key off
    the model-ID).
    """
    assert set(claude_client.MODEL_PRICING) == {
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    }
