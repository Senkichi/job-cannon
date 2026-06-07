"""Multi-provider model routing — types, config resolution, and dispatcher.

Phase 24 deliverables: ModelResult, BaseProvider, resolve_provider_config().
Phase 26 deliverable: call_model() dispatcher.
Phase 29 deliverable: daily rate limit tracker (_check_daily_limit, _increment_usage,
    _init_usage_from_db, _ensure_usage_current).
Phase 32 fix: prompt_variant for primary provider, inter-request throttling, 429 retry.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import date as _date

import requests
from jsonschema import ValidationError, validate

# DEFAULT_MODEL_* imports removed in Phase 39 (replaced by _PROVIDER_DEFAULTS)
from job_finder.json_utils import local_day_utc_window
from job_finder.web.claude_client import (  # noqa: F401 — record_cost + BudgetExceededError re-exported for callers/tests
    FREE_PROVIDERS,
    BudgetExceededError,
    cost_gate,
    is_anthropic_available,
    record_cost,
)

logger = logging.getLogger(__name__)

_VALID_WORKLOADS: frozenset[str] = frozenset({"quick", "score", "triage"})

# Workload-class model defaults per provider.
# - quick:  every non-scoring LLM call (extraction, parsing, navigation, research, reformatting, agentic enricher).
# - score:  full ordinal-rubric job scoring.
# - triage: pre-scoring gate; uses the `quick` model with a triage-specific prompt.
#
# Triage entries are absent here (resolved as identical to `quick` at lookup time).
#
# NOTE: `openrouter` is intentionally absent from this dict. It is registered in
# _SUPPORTED_PROVIDERS (so _make_adapter can dispatch it) but is eval-judge only —
# it is not part of the production scoring cascade. Adding an openrouter entry here
# would silently enable it as a cascade fallback. See providers/openrouter_provider.py.
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "claude_code_cli": {"quick": "claude-haiku-4-5", "score": "claude-sonnet-4-6"},
    "anthropic": {"quick": "claude-haiku-4-5", "score": "claude-sonnet-4-6"},
    "gemini": {"quick": "gemini-2.5-flash", "score": "gemini-2.5-pro"},
    "gemini_cli": {"quick": "gemini-2.5-flash", "score": "gemini-2.5-pro"},
    "ollama": {"quick": "qwen2.5:14b", "score": "qwen2.5:14b"},
    "local_bundled": {"quick": "Qwen2.5-3B-Instruct-Q4_K_M", "score": None},
    "groq": {"quick": "llama-3.1-8b-instant", "score": "llama-3.3-70b-versatile"},
    "cerebras": {"quick": "llama3.1-8b", "score": "llama-3.3-70b"},
}


def resolve_workload_routing(workload: str, config: dict) -> dict:
    """Resolve workload-class -> {primary, fallback} routing.

    Returns:
        {
          "primary":  {"provider": str, "model": str},
          "fallback": [{"provider": str, "model": str}, ...],
        }

    `triage` resolves to the same model as `quick` for the same provider —
    the gate is a prompt+schema choice, not a capability tier.
    """
    if workload not in _VALID_WORKLOADS:
        raise ValueError(f"Unknown workload: {workload!r}. Valid: {sorted(_VALID_WORKLOADS)}")

    providers_cfg = config.get("providers", {})
    primary_name = providers_cfg.get("primary")
    if not primary_name:
        # Fail fast — the silent default to "anthropic" was the prior
        # symptom that masked the Phase 40 schema regression (2026-05-17).
        # ValueError (not ConfigError) avoids a circular import with
        # job_finder.config; the error propagates to call_model's caller,
        # which logs it.
        raise ValueError(
            "providers.primary is not configured. See config.example.yaml for the Phase 40 schema."
        )
    overrides = providers_cfg.get("overrides", {})
    fallback_names = providers_cfg.get("fallback_chain", [])

    def lookup_model(provider: str) -> str | None:
        # triage uses the quick model
        lookup_key = "quick" if workload == "triage" else workload
        override = overrides.get(provider, {}).get(lookup_key)
        if override:
            return override
        return _PROVIDER_DEFAULTS.get(provider, {}).get(lookup_key)

    primary_model = lookup_model(primary_name)
    if primary_model is None:
        raise ValueError(f"Provider {primary_name!r} has no model for workload {workload!r}")

    return {
        "primary": {"provider": primary_name, "model": primary_model},
        "fallback": [
            {"provider": name, "model": model}
            for name in fallback_names
            if (model := lookup_model(name)) is not None
        ],
    }


# Daily usage tracking — module-level state for rate limiting.
# Resets automatically on date rollover; bootstraps from scoring_costs DB.
_daily_usage: dict[str, int] = {}
_usage_date: str = ""


def _check_daily_limit(provider: str, daily_limits: dict[str, int]) -> bool:
    """Return True if provider is under its daily limit or has no configured limit.

    Args:
        provider: Provider name (e.g., "ollama", "gemini").
        daily_limits: Dict of {provider_name: max_requests_per_day}.

    Returns:
        True if the provider may be used, False if exhausted.
    """
    if provider not in daily_limits:
        return True
    return _daily_usage.get(provider, 0) < daily_limits[provider]


def _increment_usage(provider: str) -> None:
    """Increment the daily usage counter for a provider by 1."""
    global _daily_usage
    _daily_usage[provider] = _daily_usage.get(provider, 0) + 1


def _init_usage_from_db(conn: sqlite3.Connection) -> None:
    """Bootstrap _daily_usage from scoring_costs for today.

    Called on date rollover to recover counts for providers that were
    already used today (e.g., after an app restart mid-day).

    Args:
        conn: Open SQLite connection.
    """
    global _daily_usage, _usage_date
    _daily_usage = {}
    day_start, day_end = local_day_utc_window()
    rows = conn.execute(
        "SELECT provider, COUNT(*) as cnt "
        "FROM scoring_costs "
        "WHERE timestamp >= ? AND timestamp < ? "
        "GROUP BY provider",
        (day_start, day_end),
    ).fetchall()
    for row in rows:
        _daily_usage[row[0]] = row[1]
    _usage_date = _date.today().isoformat()


def _ensure_usage_current(conn: sqlite3.Connection) -> None:
    """Reset and bootstrap daily usage counters if the date has rolled over.

    Should be called at the start of call_model() so _check_daily_limit
    and _increment_usage operate on today's data.

    Args:
        conn: Open SQLite connection for bootstrap query.
    """
    today = _date.today().isoformat()
    if _usage_date != today:
        _init_usage_from_db(conn)


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Result from a provider adapter call."""

    data: dict
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    schema_valid: bool


class BaseProvider(ABC):
    """Abstract base for provider adapters."""

    @abstractmethod
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a model call and return structured result."""
        ...


def resolve_provider_config(tier: str, config: dict) -> dict:
    """Resolve logical tier name to provider + model + fallback.

    Args:
        tier: Workload name: "quick", "score", or "triage".
        config: Full application config dict.

    Returns:
        Dict with keys:
            provider (str): Provider name
            model (str): Provider-specific model identifier
            prompt_variant (str | None): Prompt variant for primary provider
            fallback (str | None): Fallback provider name, or None
            fallback_chain (list[dict]): Ordered list of {provider, model} dicts for cascade, or []
            daily_limits (dict[str, int]): Per-provider daily request caps, or {}
            throttle_delays (dict[str, float]): Per-provider seconds between requests, or {}
    """
    providers_cfg = config.get("providers", {})
    daily_limits = providers_cfg.get("daily_limits", {})
    throttle_delays = providers_cfg.get("throttle_delays", {})

    # Use new workload routing
    routing = resolve_workload_routing(tier, config)

    # Build fallback chain, skipping providers that have no model for this workload.
    # resolve_workload_routing already filtered out unsupported (provider, workload)
    # pairs, so the fallback list is canonical here.
    fallback_chain = list(routing["fallback"])

    return {
        "provider": routing["primary"]["provider"],
        "model": routing["primary"]["model"],
        "prompt_variant": None,
        "fallback": None,
        "fallback_chain": fallback_chain,
        "daily_limits": daily_limits,
        "throttle_delays": throttle_delays,
    }


# ---------------------------------------------------------------------------
# call_model() dispatcher — Phase 26
# ---------------------------------------------------------------------------

# Single source of truth for all registered provider names.
# _make_adapter() derives its validation from this set — adding a new provider
# requires updating both _SUPPORTED_PROVIDERS and the dispatch chain in _make_adapter().
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "cerebras",
        "claude_code_cli",
        "gemini",
        "gemini_cli",
        "groq",
        "local_bundled",
        "ollama",
        "openrouter",
    }
)


class ProviderCascadeExhaustedError(RuntimeError):
    """Raised when every configured provider in a cascade has been exhausted.

    Only raised by the cascade path in call_model() after all entries have been
    tried and skipped/failed. Not raised for schema-validation failures or
    non-cascade paths.
    """


class ProviderUnavailable(RuntimeError):
    """Raised when a provider is marked unavailable at startup.

    Caught by the existing cascade catch tuples at lines ~315 and ~693 via
    the ``RuntimeError`` base class — no catch-tuple changes needed.

    Currently used to signal that Ollama was probed at startup and found
    neither running nor installable (``_jf_ollama_unavailable=True`` in
    live config). The cascade skips the provider and falls through to the
    next entry.
    """


def is_supported_provider_name(name: str) -> bool:
    """Return True if name is a registered provider in _SUPPORTED_PROVIDERS."""
    return name in _SUPPORTED_PROVIDERS


def tier_has_configured_provider(
    tier: str,
    config: dict,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Return True if the tier has at least one operationally-routable provider entry.

    This validates provider names plus constructor-time readiness checks (e.g.,
    API-key presence and existing adapter health checks such as Ollama reachability),
    but does not probe model correctness or perform a live inference call.

    conn is accepted for API symmetry with call_model() callers but is not used
    during validation — _make_adapter() does not need conn at construction time
    after the 2026-05-21 DASHBOARD-SDK-REFACTOR removed the AnthropicProvider's
    constructor-time conn requirement.

    Returns False (not raises) when providers.primary is unset — the predicate's
    contract is "is there a routable provider?" and the honest answer to that on
    an unconfigured config is no. resolve_provider_config raises ValueError in
    that case per Fix 4a (2026-05-17 hotfix); we translate it back to False here.
    """
    try:
        resolved = resolve_provider_config(tier, config)
    except ValueError:
        return False
    primary = resolved["provider"]
    chain = resolved["fallback_chain"]
    all_providers = [primary] + [entry["provider"] for entry in chain]

    for provider_name in all_providers:
        if provider_name == "anthropic":
            if is_anthropic_available():
                return True
            continue

        if not is_supported_provider_name(provider_name):
            continue

        try:
            _make_adapter(provider_name, conn, config)
            return True
        except (ValueError, RuntimeError, ImportError):
            continue

    return False


def _validate_schema(data: dict, schema: dict | None) -> list[str]:
    """Return a list of validation error messages; empty list means valid.

    Guards against schema=None before calling jsonschema.validate (Pitfall 4).

    Args:
        data: The dict to validate.
        schema: JSON schema dict, or None (no validation).

    Returns:
        List of error message strings.  Empty if valid or schema is None.
    """
    if schema is None:
        return []
    try:
        validate(instance=data, schema=schema)
        return []
    except ValidationError as exc:
        return [exc.message]


def _coerce_enum(value: str, enum_values: list[str]) -> str:
    """Best-effort coercion of a verbose model string to the closest enum value.

    Ollama frequently returns explanatory text instead of bare enum tokens,
    e.g. ``"Partial fit: The roles are..."`` instead of ``"partial"``.

    Strategy (ordered by confidence):
      1. Exact match (case-insensitive).
      2. Value starts with an enum token (e.g. ``"partial fit: ..."`` → ``"partial"``).
      3. Enum token appears anywhere in value.
    Falls back to the original string if nothing matches (lets schema
    validation report the real error).
    """
    lower = value.lower().strip()

    # 1. Exact match
    for ev in enum_values:
        if lower == ev.lower():
            return ev

    # 2. Starts-with (longest enum first to prefer "unknown" over "un")
    for ev in sorted(enum_values, key=len, reverse=True):
        if lower.startswith(ev.lower()):
            return ev

    # 3. Contains
    for ev in sorted(enum_values, key=len, reverse=True):
        if ev.lower() in lower:
            return ev

    return value


def _sanitize_output(data: dict, schema: dict | None) -> dict:
    """Best-effort sanitization of model output before schema validation.

    Local models (Ollama) frequently add extra keys, use slightly wrong
    types, or omit required array fields. This function:
    - Strips extra keys when additionalProperties is false
    - Coerces string→int for integer fields
    - Coerces verbose strings to enum values when schema has an enum constraint
    - Backfills missing required array fields with empty lists
    - Recurses into nested objects

    Returns a NEW dict (does not mutate input).
    """
    if schema is None or not isinstance(data, dict):
        return data

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    additional = schema.get("additionalProperties", True)

    result = {}
    for key, value in data.items():
        if not additional and key not in props:
            continue  # strip extra keys
        spec = props.get(key, {})
        # Coerce string→int for integer fields (common Ollama issue)
        if spec.get("type") == "integer" and isinstance(value, str):
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                pass
        # Coerce verbose strings to enum values
        if "enum" in spec and isinstance(value, str) and value not in spec["enum"]:
            value = _coerce_enum(value, spec["enum"])
        # Recurse into nested objects
        if spec.get("type") == "object" and isinstance(value, dict):
            value = _sanitize_output(value, spec)
        result[key] = value

    # Backfill missing required fields with safe defaults.
    # Ollama frequently omits talking_points / resume_priority_skills.
    for key in required:
        if key not in result:
            spec = props.get(key, {})
            if spec.get("type") == "array":
                result[key] = []
            elif spec.get("type") == "object":
                result[key] = {}

    return result


def _sanitized_result(result: ModelResult, schema: dict | None, provider_name: str) -> ModelResult:
    """Return a ModelResult with ``_sanitize_output`` applied to its ``data``.

    Sanitization is skipped for non-dict data and when the sanitize pass
    returns the same object (identity-preserving fast path). Otherwise a new
    ModelResult is produced via ``dataclasses.replace`` so future fields are
    picked up automatically.
    """
    if not isinstance(result.data, dict):
        return result
    sanitized = _sanitize_output(result.data, schema)
    if sanitized is result.data:
        return result
    return replace(result, data=sanitized)


def _augment_with_errors(messages: list[dict], errors: list[str]) -> list[dict]:
    """Return a NEW messages list with schema errors appended to the last message.

    CRITICAL: Does NOT mutate the input list (Pitfall 2).

    Args:
        messages: Original messages list.
        errors: List of validation error strings from _validate_schema.

    Returns:
        New list where the last message content has schema errors appended.
    """
    error_text = "\n\nSchema validation errors from previous attempt:\n" + "\n".join(
        f"- {e}" for e in errors
    )
    return messages[:-1] + [{**messages[-1], "content": messages[-1]["content"] + error_text}]


def _make_adapter(
    provider_name: str,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
    job_id: str | None = None,
    purpose: str = "",
) -> BaseProvider:
    """Instantiate the correct provider adapter.

    Args:
        provider_name: Any name in _SUPPORTED_PROVIDERS.
        conn: Open SQLite connection. Required only for the Anthropic adapter
            (forwarded to call_claude for cost recording); pass None for non-
            Anthropic validation calls.
        config: Application config dict.
        job_id: Job dedup_key for cost attribution (nullable, Anthropic only).
        purpose: Feature attribution label for cost rows (Anthropic only).

    Returns:
        Concrete BaseProvider instance.

    Raises:
        ValueError: If provider_name is not in _SUPPORTED_PROVIDERS or
            if a provider's local prerequisites are missing (e.g. API key).
    """
    if provider_name not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider_name!r}")

    if config is None:
        config = {}

    # Lazy imports to avoid circular import: providers import from model_provider
    from job_finder.web.providers.anthropic_provider import AnthropicProvider
    from job_finder.web.providers.ollama_provider import OllamaProvider

    if provider_name == "anthropic":
        if not is_anthropic_available():
            raise ValueError(
                "Anthropic CLI not configured (ANTHROPIC_API_KEY / JF_ANTHROPIC_API_KEY missing)"
            )
        return AnthropicProvider()
    if provider_name == "gemini":
        from job_finder.web.providers.gemini_provider import GeminiProvider

        return GeminiProvider(config=config)
    if provider_name == "ollama":
        if config.get("_jf_ollama_unavailable"):
            raise ProviderUnavailable("ollama marked unavailable at startup")
        return OllamaProvider(config=config)
    if provider_name == "openrouter":
        from job_finder.web.providers.openrouter_provider import OpenRouterProvider

        return OpenRouterProvider(config=config)
    if provider_name == "groq":
        from job_finder.web.providers.groq_provider import GroqProvider

        return GroqProvider(config=config)
    if provider_name == "cerebras":
        from job_finder.web.providers.cerebras_provider import CerebrasProvider

        return CerebrasProvider(config=config)
    if provider_name == "claude_code_cli":
        from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider

        return ClaudeCodeCLIProvider(config=config)
    if provider_name == "gemini_cli":
        from job_finder.web.providers.gemini_cli import GeminiCLIProvider

        return GeminiCLIProvider(config=config)
    if provider_name == "local_bundled":
        from job_finder.web.providers.local_bundled import LocalBundledProvider

        lp_cfg = (config or {}).get("providers", {}).get("local_bundled", {})
        model_path = lp_cfg.get("model_path", "")
        if not model_path:
            raise ValueError("providers.local_bundled.model_path not configured")
        return LocalBundledProvider(
            model_path=model_path,
            n_ctx=lp_cfg.get("n_ctx", 4096),
        )

    raise ValueError(f"No adapter dispatch branch for provider: {provider_name!r}")


def _maybe_record_cost(
    result: ModelResult,
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
) -> None:
    """Record cost for the result of a cascade provider call.

    Polish-review F2 (2026-05-26) removed the historical
    ``if result.provider == "anthropic": return`` early-exit. Before F2,
    ``AnthropicProvider`` routed through ``call_claude``, which itself
    called ``record_cost`` with ``provider="claude_cli"``; this function
    skipped to avoid double-recording. Post-F2 the adapter goes directly
    to ``_run_oneshot`` and the cost row is written here with
    ``provider="anthropic"`` — single source of truth.

    Free providers (including ``"anthropic"`` after F2 — the CLI dispatch
    is subscription-funded) record cost as $0 instead of calling
    ``compute_cost``, which doesn't recognize their model names and would
    fall back to the most-expensive Claude pricing as a safety default.

    Args:
        result: ModelResult from a provider adapter call.
        conn: Open SQLite connection.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label.

    Raises:
        ValueError: If ``result.provider`` is empty. U6 guard — scoring_costs
            DEFAULT for the provider column ('anthropic', m018) is in
            FREE_PROVIDERS post-F2, so an INSERT that omits provider would
            silently land in a row that is filtered out of every cost rollup.
            Loud failure beats silent loss.
    """
    if not result.provider:
        raise ValueError(
            f"_maybe_record_cost: ModelResult.provider must be non-empty "
            f"(job_id={job_id}, purpose={purpose}, model={result.model})"
        )

    if result.provider in FREE_PROVIDERS:
        cost_usd = 0.0
    else:
        cost_usd = result.cost_usd
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        "INSERT INTO scoring_costs "
        "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider, schema_valid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            purpose,
            result.model,
            result.input_tokens,
            result.output_tokens,
            cost_usd,
            utc_now_iso(),
            result.provider,
            int(result.schema_valid),
        ),
    )
    conn.commit()


def call_model(
    tier: str,
    system: str,
    messages: list[dict],
    conn: sqlite3.Connection,
    config: dict,
    output_schema: dict | None = None,
    job_id: str | None = None,
    purpose: str = "",
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> ModelResult:
    """Dispatch a model call to the configured provider for the given tier.

    Routes by tier, validates output schema with jsonschema, retries once with
    augmented prompt on schema failure, falls back to Anthropic when retry
    fails, bypasses budget gate for free providers, and records cost only for
    non-Anthropic providers (avoiding double-recording).

    Args:
        tier: Workload class: "quick", "score", or "triage".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        conn: Open SQLite connection for budget gating and cost recording.
        config: Application config dict.
        output_schema: JSON schema dict for structured output (or None).
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label for cost rows.
        max_tokens: Maximum output tokens. Defaults to 1024.
        timeout: Request timeout in seconds. Defaults to provider default.

    Returns:
        ModelResult from the successful adapter call.

    Raises:
        BudgetExceededError: If cost_gate blocks an Anthropic call.
        ProviderCascadeExhaustedError: If all configured providers (primary +
            fallbacks) fail or are unavailable.
    """
    resolved = resolve_provider_config(tier, config)
    provider_name: str = resolved["provider"]
    model: str = resolved["model"]
    primary_variant: str | None = resolved["prompt_variant"]
    fallback_chain: list[dict] = resolved["fallback_chain"]
    daily_limits: dict[str, int] = resolved["daily_limits"]
    throttle_delays: dict[str, float] = resolved["throttle_delays"]

    # --- Cascade is the only path (Phase 40 hotfix 2026-05-17). Previously
    # an empty fallback_chain skipped the cascade entirely and direct-
    # dispatched to provider_name, which made cascade-bypass invisible. The
    # cascade loop already handles a one-entry chain correctly. ---
    _ensure_usage_current(conn)

    chain: list[dict] = [
        {"provider": provider_name, "model": model, "prompt_variant": primary_variant}
    ] + list(fallback_chain)

    # Audit-log the cascade the caller is about to try. Paired with the
    # "call_model ROUTED" entry below, this gives operators end-to-end
    # visibility into which provider actually handled which tier/purpose.
    logger.info(
        "call_model CASCADE: tier=%s chain=[%s] purpose=%s job_id=%s",
        tier,
        ", ".join(f"{e['provider']}:{e['model']}" for e in chain),
        purpose,
        job_id,
    )

    # Track last-call time per provider for inter-request throttling
    _last_call: dict[str, float] = {}

    for entry in chain:
        entry_provider = entry["provider"]
        entry_model = entry["model"]

        # Skip if daily limit exhausted
        if not _check_daily_limit(entry_provider, daily_limits):
            logger.info("Cascade: %s exhausted, skipping", entry_provider)
            continue

        # Skip if adapter creation fails (missing API key -> ValueError,
        # Ollama unreachable -> RuntimeError)
        try:
            adapter = _make_adapter(
                entry_provider,
                conn,
                config,
                job_id=job_id,
                purpose=purpose,
            )
        except (ValueError, RuntimeError, ImportError) as exc:
            logger.warning("Cascade: %s unavailable: %s", entry_provider, exc)
            continue

        # Budget gate for paid providers
        if entry_provider not in FREE_PROVIDERS:
            if not cost_gate(conn, config, tier):
                logger.info("Cascade: %s over budget, skipping", entry_provider)
                continue

        # CASC-05's per-provider variant prompt is gone alongside
        # PROMPT_VARIANTS (deleted with sonnet_evaluator in Plan 4).
        # All scoring callers now use the single v3 system prompt.
        effective_system = system

        # Inter-request throttle: respect per-provider delay from config
        delay = throttle_delays.get(entry_provider, 0)
        if delay > 0:
            last = _last_call.get(entry_provider, 0)
            elapsed = time.monotonic() - last
            if elapsed < delay:
                wait = delay - elapsed
                logger.debug("Cascade: throttling %s for %.1fs", entry_provider, wait)
                time.sleep(wait)

        # 429 retry with backoff (up to 2 retries)
        max_retries = 2
        for attempt in range(1 + max_retries):
            try:
                _last_call[entry_provider] = time.monotonic()
                result = adapter.call(
                    entry_model,
                    effective_system,
                    messages,
                    output_schema,
                    max_tokens,
                    timeout,
                )
                # Sanitize output for non-Anthropic providers (strip extra keys, coerce types)
                result = _sanitized_result(result, output_schema, entry_provider)
                # Schema validation + retry (per-provider, using original messages)
                errors = _validate_schema(result.data, output_schema)
                if errors:
                    augmented = _augment_with_errors(messages, errors)
                    _last_call[entry_provider] = time.monotonic()
                    result = adapter.call(
                        entry_model,
                        effective_system,
                        augmented,
                        output_schema,
                        max_tokens,
                        timeout,
                    )
                    result = _sanitized_result(result, output_schema, entry_provider)
                    errors = _validate_schema(result.data, output_schema)
                if not errors:
                    _increment_usage(entry_provider)
                    try:
                        _maybe_record_cost(result, conn, job_id, purpose)
                    except Exception as cost_exc:
                        # Cost recording is non-fatal — don't discard a good
                        # scoring result because of a transient DB lock.
                        logger.warning(
                            "Cascade: %s cost recording failed (non-fatal): %s",
                            entry_provider,
                            cost_exc,
                        )
                    logger.info(
                        "call_model ROUTED: tier=%s provider=%s model=%s purpose=%s job_id=%s",
                        tier,
                        result.provider,
                        result.model,
                        purpose,
                        job_id,
                    )
                    return result
                # Schema still invalid after retry — skip to next provider
                logger.warning(
                    "Cascade: %s schema invalid after retry, skipping",
                    entry_provider,
                )
                break  # Don't retry schema failures
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    if attempt < max_retries:
                        backoff = (2**attempt) * 2  # 2s, 4s
                        logger.warning(
                            "Cascade: %s rate limited (429), retry %d/%d after %ds",
                            entry_provider,
                            attempt + 1,
                            max_retries,
                            backoff,
                        )
                        time.sleep(backoff)
                        continue
                    # Exhausted retries — mark provider as done for today
                    logger.warning(
                        "Cascade: %s rate limited (429) after %d retries, marking exhausted",
                        entry_provider,
                        max_retries,
                    )
                    _daily_usage[entry_provider] = daily_limits.get(entry_provider, 999999)
                    break
                logger.warning("Cascade: %s HTTP error: %s", entry_provider, exc)
                break  # Non-429 HTTP errors — don't retry
            except Exception as exc:
                logger.warning("Cascade: %s error: %s", entry_provider, exc)
                break  # Unknown errors — don't retry

    raise ProviderCascadeExhaustedError(
        f"All providers in cascade exhausted or unavailable for tier: {tier!r}. "
        f"Providers tried: {[e['provider'] for e in chain]}"
    )
