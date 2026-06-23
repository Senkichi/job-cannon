"""Single source of truth for the LLM provider roster + per-provider properties.

The provider roster used to be re-enumerated in five places that had to be kept
in sync by hand: ``model_provider._SUPPORTED_PROVIDERS``,
``model_provider._PROVIDER_DEFAULTS``, the ``_make_adapter`` dispatch chain,
``claude_client.FREE_PROVIDERS``, and ``settings._PROVIDER_KEY_FIELDS``. Adding a
provider meant editing all of them; forgetting one failed silently — most
dangerously ``FREE_PROVIDERS`` (the Issue-303 under-reported-spend incident:
a paid provider mis-tagged free, or vice versa, with no error).

This module is the one table. It sits BELOW ``claude_client`` /
``model_provider`` / ``settings`` (it imports nothing from them) so all three
can derive their enumerations from here without an import cycle. Adding a
provider is one ``ProviderSpec`` row. The ``_make_adapter`` construction chain
stays hand-written (each provider's instantiation genuinely differs) but is
pinned to ``SUPPORTED_PROVIDERS`` by an existing guard test
(test_model_provider.test_supported_providers_all_wired_in_make_adapter), and
the derivations here are pinned by test_provider_catalog_single_source.
"""

from __future__ import annotations

from typing import NamedTuple


class ProviderSpec(NamedTuple):
    """One provider's roster-level facts.

    Attributes:
        name: Provider key used across config, cost rows, and dispatch.
        is_free: True if calls incur no per-call cost (subscription / local /
            CLI). Members become part of the budget-exclusion FREE set.
        defaults: Per-workload model defaults ({"quick": ..., "score": ...},
            a value may be None when a workload is unsupported). None means the
            provider has NO production default and is omitted from
            PROVIDER_DEFAULTS — e.g. ``openrouter`` is dispatchable for the
            eval judge but is intentionally NOT part of the scoring cascade.
        key_label: Settings UI label for the BYO API-key field
            (providers.api_keys.<name>), or None for providers with no
            user-entered key (CLI / local / subscription-OAuth transports).
    """

    name: str
    is_free: bool
    defaults: dict[str, str | None] | None = None
    key_label: str | None = None


# THE roster. Order matters only for PROVIDER_KEY_FIELDS (rendered in the
# Settings UI in this relative order): anthropic, gemini, groq, cerebras,
# openrouter. SUPPORTED_PROVIDERS / FREE set are unordered; PROVIDER_DEFAULTS
# is keyed.
PROVIDERS: tuple[ProviderSpec, ...] = (
    # subscription OAuth transport ($0) — API-key transport is the separate
    # "anthropic_api" row below; both share the one "Anthropic API key" field.
    ProviderSpec(
        "anthropic",
        is_free=True,
        defaults={"quick": "claude-haiku-4-5", "score": "claude-sonnet-4-6"},
        key_label="Anthropic API key",
    ),
    # Issue 303: API-key transport (billed per token). Same model defaults as
    # "anthropic"; NOT free, so cost_gate / budget accounting apply.
    ProviderSpec(
        "anthropic_api",
        is_free=False,
        defaults={"quick": "claude-haiku-4-5", "score": "claude-sonnet-4-6"},
    ),
    ProviderSpec(
        "gemini",
        is_free=True,
        defaults={"quick": "gemini-2.5-flash", "score": "gemini-2.5-pro"},
        key_label="Gemini API key",
    ),
    ProviderSpec(
        "gemini_cli",
        is_free=True,
        defaults={"quick": "gemini-2.5-flash", "score": "gemini-2.5-pro"},
    ),
    ProviderSpec(
        "ollama",
        is_free=True,
        defaults={"quick": "qwen2.5:14b", "score": "qwen2.5:14b"},
    ),
    ProviderSpec(
        "local_bundled",
        is_free=True,
        defaults={"quick": "Qwen2.5-3B-Instruct-Q4_K_M", "score": None},
    ),
    ProviderSpec(
        "claude_code_cli",
        is_free=True,
        defaults={"quick": "claude-haiku-4-5", "score": "claude-sonnet-4-6"},
    ),
    ProviderSpec(
        "groq",
        is_free=False,
        defaults={"quick": "llama-3.1-8b-instant", "score": "llama-3.3-70b-versatile"},
        key_label="Groq API key",
    ),
    ProviderSpec(
        "cerebras",
        is_free=False,
        defaults={"quick": "llama3.1-8b", "score": "llama-3.3-70b"},
        key_label="Cerebras API key",
    ),
    # Dispatchable (eval judge) but NOT in the scoring cascade → defaults=None
    # so it is excluded from PROVIDER_DEFAULTS. Adding a defaults dict here would
    # silently enable it as a cascade fallback.
    ProviderSpec("openrouter", is_free=False, key_label="OpenRouter API key"),
)

# Free cost-attribution labels that are NOT adapter-dispatchable providers, so
# they have no ProviderSpec row but must still be excluded from the budget:
#   - "claude_cli"  — legacy call_claude() internal path (back-compat label).
#   - "google_cse"  — Google Programmable Search source (Stage 3), a search
#                     provider, not an LLM provider.
_EXTRA_FREE_LABELS: frozenset[str] = frozenset({"claude_cli", "google_cse"})


# ── Derived views — every consumer imports one of these instead of re-listing ──

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(p.name for p in PROVIDERS)

PROVIDER_DEFAULTS: dict[str, dict[str, str | None]] = {
    p.name: dict(p.defaults) for p in PROVIDERS if p.defaults is not None
}

# Budget-exclusion set: free adapter providers PLUS the non-adapter free labels.
FREE_PROVIDER_NAMES: frozenset[str] = (
    frozenset(p.name for p in PROVIDERS if p.is_free) | _EXTRA_FREE_LABELS
)

# Settings BYO-key fields, in roster order: (provider_name, ui_label).
PROVIDER_KEY_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (p.name, p.key_label) for p in PROVIDERS if p.key_label is not None
)
