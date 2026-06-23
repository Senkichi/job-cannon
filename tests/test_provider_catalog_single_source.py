"""Guard: the LLM provider roster has ONE source of truth (provider_catalog).

_SUPPORTED_PROVIDERS, _PROVIDER_DEFAULTS, FREE_PROVIDERS, and
settings._PROVIDER_KEY_FIELDS used to be parallel hand-maintained enumerations
of the same roster — the FREE one caused the Issue-303 spend-accounting
incident (a transport mis-flagged free). They now derive from
job_finder.web.provider_catalog. These tests fail if a consumer stops deriving,
or if the derivation relationships break.

(test_model_provider already pins _make_adapter <-> _SUPPORTED_PROVIDERS and
_PROVIDER_DEFAULTS <= _SUPPORTED_PROVIDERS; those become structural here.)
"""

from __future__ import annotations

from job_finder.web import provider_catalog as cat


def test_consumers_derive_from_catalog():
    """Each of the four enumerations equals its catalog-derived view."""
    from job_finder.web.blueprints.settings import _PROVIDER_KEY_FIELDS
    from job_finder.web.claude_client import FREE_PROVIDERS
    from job_finder.web.model_provider import _PROVIDER_DEFAULTS, _SUPPORTED_PROVIDERS

    assert _SUPPORTED_PROVIDERS == cat.SUPPORTED_PROVIDERS
    assert _PROVIDER_DEFAULTS == cat.PROVIDER_DEFAULTS
    assert FREE_PROVIDERS == cat.FREE_PROVIDER_NAMES
    assert _PROVIDER_KEY_FIELDS == cat.PROVIDER_KEY_FIELDS  # order is UI-significant


def test_defaults_are_subset_of_roster():
    assert set(cat.PROVIDER_DEFAULTS) <= set(cat.SUPPORTED_PROVIDERS)


def test_key_fields_reference_roster_providers():
    names = {name for name, _label in cat.PROVIDER_KEY_FIELDS}
    assert names <= set(cat.SUPPORTED_PROVIDERS)


def test_free_minus_roster_is_exactly_the_nonadapter_labels():
    """The only FREE names that are not adapter providers are the documented
    non-adapter cost labels (claude_cli, google_cse)."""
    assert (cat.FREE_PROVIDER_NAMES - cat.SUPPORTED_PROVIDERS) == cat._EXTRA_FREE_LABELS


def test_cost_correctness_flag_pins():
    """Issue 303: a provider mis-flagged free/paid silently mis-accounts spend.
    Pin the dangerous flags explicitly so a careless edit trips a test."""
    is_free = {p.name: p.is_free for p in cat.PROVIDERS}
    assert is_free["anthropic"] is True  # subscription OAuth ($0)
    assert is_free["anthropic_api"] is False  # API-key transport (paid)
    assert is_free["groq"] is False
    assert is_free["cerebras"] is False
    assert is_free["openrouter"] is False
    assert is_free["ollama"] is True
    assert is_free["gemini"] is True


def test_openrouter_dispatchable_but_not_a_cascade_default():
    """openrouter is in the roster (eval-judge dispatch) but intentionally has
    no production default (defaults=None -> absent from PROVIDER_DEFAULTS)."""
    assert "openrouter" in cat.SUPPORTED_PROVIDERS
    assert "openrouter" not in cat.PROVIDER_DEFAULTS
