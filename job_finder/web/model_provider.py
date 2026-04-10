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

import requests
from dataclasses import dataclass
from datetime import date as _date
from typing import Any

from jsonschema import ValidationError, validate

from job_finder.config import DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_OPUS, DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import BudgetExceededError, cost_gate, record_cost

logger = logging.getLogger(__name__)

_TIER_DEFAULTS: dict[str, str] = {
    "haiku": DEFAULT_MODEL_HAIKU,
    "sonnet": DEFAULT_MODEL_SONNET,
    "opus": DEFAULT_MODEL_OPUS,
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
    rows = conn.execute(
        "SELECT provider, COUNT(*) as cnt "
        "FROM scoring_costs "
        "WHERE date(timestamp) = date('now') "
        "GROUP BY provider"
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
        tier: Logical tier name: "sonnet", "haiku", or "opus".
        config: Full application config dict.

    Returns:
        Dict with keys:
            provider (str): "anthropic" | "gemini" | "ollama"
            model (str): Provider-specific model identifier
            prompt_variant (str | None): Prompt variant for primary provider
            fallback (str | None): Fallback provider name, or None
            fallback_chain (list[dict]): Ordered list of {provider, model} dicts for cascade, or []
            daily_limits (dict[str, int]): Per-provider daily request caps, or {}
            throttle_delays (dict[str, float]): Per-provider seconds between requests, or {}
    """
    providers_cfg = config.get("providers", {})
    tier_cfg = providers_cfg.get(tier, {})

    # Inherit provider routing from a configured peer tier when this tier has
    # no explicit config.  This lets a single providers.sonnet entry cover all
    # tiers without duplicating the cascade in config.yaml.  The model and
    # prompt_variant stay tier-specific (only provider + fallback_chain inherit).
    if not tier_cfg:
        _non_meta = {k: v for k, v in providers_cfg.items()
                     if isinstance(v, dict) and k not in ("daily_limits", "throttle_delays")}
        if _non_meta:
            _donor = next(iter(_non_meta.values()))
            tier_cfg = {
                "provider": _donor.get("provider", "anthropic"),
                "model": _donor.get("model"),
                "fallback_chain": _donor.get("fallback_chain", []),
                "fallback": _donor.get("fallback"),
            }

    scoring_model = config.get("scoring", {}).get("models", {}).get(tier)
    default_model = scoring_model or _TIER_DEFAULTS.get(tier, DEFAULT_MODEL_SONNET)

    provider = tier_cfg.get("provider", "anthropic")
    model = tier_cfg.get("model") or default_model
    prompt_variant = tier_cfg.get("prompt_variant", None)
    fallback = tier_cfg.get("fallback", None)
    fallback_chain = tier_cfg.get("fallback_chain", [])
    daily_limits = providers_cfg.get("daily_limits", {})
    throttle_delays = providers_cfg.get("throttle_delays", {})

    return {
        "provider": provider,
        "model": model,
        "prompt_variant": prompt_variant,
        "fallback": fallback,
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
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "anthropic", "gemini", "ollama", "ollm", "cohere", "mistral", "sambanova",
    "groq", "cerebras",
})

# Providers that are free (no cost_gate needed, record cost via record_cost).
# groq and cerebras are free in this app's budget model only.
# Vendor billing/rate limits still exist outside the app.
_FREE_PROVIDERS: frozenset[str] = frozenset({
    "gemini", "ollama", "ollm", "sambanova",
    "groq", "cerebras",
})


class ProviderCascadeExhaustedError(RuntimeError):
    """Raised when every configured provider in a cascade has been exhausted.

    Only raised by the cascade path in call_model() after all entries have been
    tried and skipped/failed. Not raised for schema-validation failures or
    non-cascade paths.
    """


def is_supported_provider_name(name: str) -> bool:
    """Return True if name is a registered provider in _SUPPORTED_PROVIDERS."""
    return name in _SUPPORTED_PROVIDERS


def tier_has_configured_provider(
    tier: str,
    config: dict,
    client: Any | None,
    conn: "sqlite3.Connection | None" = None,
) -> bool:
    """Return True if the tier has at least one operationally-routable provider entry.

    This validates provider names plus constructor-time readiness checks (e.g.,
    API-key presence and existing adapter health checks such as Ollama reachability),
    but does not probe model correctness or perform a live inference call.

    conn is accepted for API symmetry with call_model() callers but is not used
    during validation — _make_adapter() only uses conn for AnthropicProvider, which
    is short-circuited via the client-is-not-None check before _make_adapter() is called.
    """
    resolved = resolve_provider_config(tier, config)
    primary = resolved["provider"]
    chain = resolved["fallback_chain"]
    all_providers = [primary] + [entry["provider"] for entry in chain]

    for provider_name in all_providers:
        if provider_name == "anthropic":
            if client is not None:
                return True
            continue

        if not is_supported_provider_name(provider_name):
            continue

        try:
            _make_adapter(provider_name, client, conn, config)
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


def _augment_with_errors(messages: list[dict], errors: list[str]) -> list[dict]:
    """Return a NEW messages list with schema errors appended to the last message.

    CRITICAL: Does NOT mutate the input list (Pitfall 2).

    Args:
        messages: Original messages list.
        errors: List of validation error strings from _validate_schema.

    Returns:
        New list where the last message content has schema errors appended.
    """
    error_text = (
        "\n\nSchema validation errors from previous attempt:\n"
        + "\n".join(f"- {e}" for e in errors)
    )
    return messages[:-1] + [
        {**messages[-1], "content": messages[-1]["content"] + error_text}
    ]


def _make_adapter(
    provider_name: str,
    client: Any | None,
    conn: "sqlite3.Connection | None" = None,
    config: dict | None = None,
    job_id: str | None = None,
    purpose: str = "",
) -> "BaseProvider":
    """Instantiate the correct provider adapter.

    Args:
        provider_name: Any name in _SUPPORTED_PROVIDERS.
        client: Anthropic client (required for anthropic, unused for others).
        conn: Open SQLite connection. Required only for the Anthropic adapter;
            pass None for non-Anthropic validation calls.
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
    from job_finder.web.providers.cerebras_provider import CerebrasProvider
    from job_finder.web.providers.cohere_provider import CohereProvider
    from job_finder.web.providers.groq_provider import GroqProvider
    from job_finder.web.providers.mistral_provider import MistralProvider
    from job_finder.web.providers.ollama_provider import OllamaProvider
    from job_finder.web.providers.ollm_provider import OllmProvider
    from job_finder.web.providers.sambanova_provider import SambanovaProvider

    if provider_name == "anthropic":
        if client is None:
            raise ValueError("Anthropic client not provided")
        # Detect DOA clients: anthropic.Anthropic() succeeds without an API key
        # but every API call fails with "Could not resolve authentication method".
        # Fail fast here so the cascade skips immediately instead of wasting time.
        _key = getattr(client, "api_key", None)
        if not _key:
            raise ValueError("Anthropic client has no API key configured")
        return AnthropicProvider(
            client=client, conn=conn, config=config, job_id=job_id, purpose=purpose
        )
    if provider_name == "cerebras":
        return CerebrasProvider(config=config)
    if provider_name == "cohere":
        return CohereProvider(config=config)
    if provider_name == "gemini":
        from job_finder.web.providers.gemini_provider import GeminiProvider
        return GeminiProvider(config=config)
    if provider_name == "groq":
        return GroqProvider(config=config)
    if provider_name == "mistral":
        return MistralProvider(config=config)
    if provider_name == "ollama":
        return OllamaProvider(config=config)
    if provider_name == "ollm":
        return OllmProvider(config=config)
    if provider_name == "sambanova":
        return SambanovaProvider(config=config)


def _maybe_record_cost(
    result: "ModelResult",
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
) -> None:
    """Record cost for non-Anthropic providers.

    AnthropicProvider delegates to call_claude() which records cost internally.
    Calling record_cost() again for Anthropic would double-count (Pitfall 1).

    Args:
        result: ModelResult from a provider adapter call.
        conn: Open SQLite connection.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label.
    """
    if result.provider == "anthropic":
        return  # call_claude() already recorded it
    # Free providers: record cost as $0 instead of calling compute_cost()
    # which doesn't recognize their model names and falls back to Opus pricing.
    if result.provider in _FREE_PROVIDERS:
        cost_usd = 0.0
    else:
        cost_usd = result.cost_usd
    from job_finder.json_utils import utc_now_iso
    conn.execute(
        "INSERT INTO scoring_costs "
        "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, purpose, result.model, result.input_tokens, result.output_tokens,
         cost_usd, utc_now_iso(), result.provider),
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
    client: Any | None = None,
) -> "ModelResult":
    """Dispatch a model call to the configured provider for the given tier.

    Routes by tier, validates output schema with jsonschema, retries once with
    augmented prompt on schema failure, falls back to Anthropic when retry
    fails, bypasses budget gate for free providers, and records cost only for
    non-Anthropic providers (avoiding double-recording).

    Args:
        tier: Logical tier name: "sonnet", "haiku", or "opus".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        conn: Open SQLite connection for budget gating and cost recording.
        config: Application config dict.
        output_schema: JSON schema dict for structured output (or None).
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label for cost rows.
        max_tokens: Maximum output tokens. Defaults to 1024.
        timeout: Request timeout in seconds. Defaults to provider default.
        client: Anthropic client instance — required when provider is
            "anthropic" or fallback is "anthropic".

    Returns:
        ModelResult from the successful adapter call.

    Raises:
        BudgetExceededError: If cost_gate blocks an Anthropic call.
        RuntimeError: If schema validation fails after retry and no fallback
            is configured.
        RuntimeError: If all cascade providers are exhausted (when fallback_chain is configured).
    """
    resolved = resolve_provider_config(tier, config)
    provider_name: str = resolved["provider"]
    model: str = resolved["model"]
    primary_variant: str | None = resolved["prompt_variant"]
    fallback: str | None = resolved["fallback"]
    fallback_chain: list[dict] = resolved["fallback_chain"]
    daily_limits: dict[str, int] = resolved["daily_limits"]
    throttle_delays: dict[str, float] = resolved["throttle_delays"]

    # --- Cascade path (non-empty fallback_chain) ---
    if fallback_chain:
        _ensure_usage_current(conn)

        chain: list[dict] = [
            {"provider": provider_name, "model": model, "prompt_variant": primary_variant}
        ] + list(fallback_chain)

        # Track last-call time per provider for inter-request throttling
        _last_call: dict[str, float] = {}

        for entry in chain:
            entry_provider = entry["provider"]
            entry_model = entry["model"]
            entry_variant = entry.get("prompt_variant")

            # Skip if daily limit exhausted
            if not _check_daily_limit(entry_provider, daily_limits):
                logger.info("Cascade: %s exhausted, skipping", entry_provider)
                continue

            # Skip if adapter creation fails (missing API key -> ValueError,
            # Ollama unreachable -> RuntimeError)
            try:
                adapter = _make_adapter(
                    entry_provider, client, conn, config,
                    job_id=job_id, purpose=purpose,
                )
            except (ValueError, RuntimeError) as exc:
                logger.warning("Cascade: %s unavailable: %s", entry_provider, exc)
                continue

            # Budget gate for paid providers
            if entry_provider not in _FREE_PROVIDERS:
                if not cost_gate(conn, config, tier):
                    logger.info("Cascade: %s over budget, skipping", entry_provider)
                    continue

            # Resolve effective system prompt for this cascade entry (CASC-05)
            effective_system = system
            if entry_variant:
                # Lazy import to avoid circular dependency (sonnet_evaluator imports call_model)
                from job_finder.web.sonnet_evaluator import PROMPT_VARIANTS as _pv
                effective_system = _pv.get(entry_variant, system)

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
                        entry_model, effective_system, messages, output_schema, max_tokens, timeout,
                    )
                    # Sanitize output for non-Anthropic providers (strip extra keys, coerce types)
                    if entry_provider != "anthropic" and isinstance(result.data, dict):
                        sanitized = _sanitize_output(result.data, output_schema)
                        if sanitized is not result.data:
                            result = ModelResult(
                                data=sanitized, cost_usd=result.cost_usd,
                                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                model=result.model, provider=result.provider, schema_valid=result.schema_valid,
                            )
                    # Schema validation + retry (per-provider, using original messages)
                    errors = _validate_schema(result.data, output_schema)
                    if errors:
                        augmented = _augment_with_errors(messages, errors)
                        _last_call[entry_provider] = time.monotonic()
                        result = adapter.call(
                            entry_model, effective_system, augmented, output_schema, max_tokens, timeout,
                        )
                        if entry_provider != "anthropic" and isinstance(result.data, dict):
                            sanitized = _sanitize_output(result.data, output_schema)
                            if sanitized is not result.data:
                                result = ModelResult(
                                    data=sanitized, cost_usd=result.cost_usd,
                                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                    model=result.model, provider=result.provider, schema_valid=result.schema_valid,
                                )
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
                                entry_provider, cost_exc,
                            )
                        return result
                    # Schema still invalid after retry — skip to next provider
                    logger.warning(
                        "Cascade: %s schema invalid after retry, skipping", entry_provider,
                    )
                    break  # Don't retry schema failures
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 429:
                        if attempt < max_retries:
                            backoff = (2 ** attempt) * 2  # 2s, 4s
                            logger.warning(
                                "Cascade: %s rate limited (429), retry %d/%d after %ds",
                                entry_provider, attempt + 1, max_retries, backoff,
                            )
                            time.sleep(backoff)
                            continue
                        # Exhausted retries — mark provider as done for today
                        logger.warning(
                            "Cascade: %s rate limited (429) after %d retries, marking exhausted",
                            entry_provider, max_retries,
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

    # --- Backward-compat path (empty fallback_chain) — UNCHANGED from Phase 26 ---

    # Budget gate — skip entirely for free providers (INFRA-04)
    if provider_name not in _FREE_PROVIDERS:
        if not cost_gate(conn, config, tier):
            raise BudgetExceededError(f"Budget cap reached. Tier: {tier}")

    # Instantiate and make first attempt
    adapter = _make_adapter(provider_name, client, conn, config, job_id=job_id, purpose=purpose)
    result = adapter.call(model, system, messages, output_schema, max_tokens, timeout)

    # Schema validation — attempt 1
    errors = _validate_schema(result.data, output_schema)
    if not errors:
        _maybe_record_cost(result, conn, job_id, purpose)
        return result

    # Retry with augmented prompt (INFRA-02)
    augmented = _augment_with_errors(messages, errors)
    result = adapter.call(model, system, augmented, output_schema, max_tokens, timeout)
    errors = _validate_schema(result.data, output_schema)
    if not errors:
        _maybe_record_cost(result, conn, job_id, purpose)
        return result

    # Fallback to Anthropic when retry fails (INFRA-03)
    if fallback and client is not None:
        from job_finder.web.providers.anthropic_provider import AnthropicProvider

        fallback_adapter = AnthropicProvider(
            client=client, conn=conn, config=config, job_id=job_id, purpose=purpose
        )
        # Use empty config to get Anthropic default model (not the Gemini/Ollama model name)
        fallback_model = resolve_provider_config(tier, {})["model"]
        # AnthropicProvider handles cost_gate + record_cost internally via call_claude()
        result = fallback_adapter.call(
            fallback_model, system, messages, output_schema, max_tokens, timeout
        )
        return result

    raise RuntimeError(
        f"Schema validation failed after retry and no fallback available for tier: {tier}"
    )
