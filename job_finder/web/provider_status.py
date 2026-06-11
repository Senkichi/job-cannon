"""Cached AI-provider availability — shared by dashboard and job board.

Extracted from ``blueprints/dashboard.py`` when the job board grew the same
"no scoring provider" banner (WP3, release polish): two blueprints needing
the answer means the 5-minute cache must live in one place, not be
duplicated per blueprint.
"""

from __future__ import annotations

import time

from job_finder.web.claude_client import is_anthropic_available
from job_finder.web.model_provider import tier_has_configured_provider

# Cache provider availability for 5 minutes to avoid Ollama health check
# on every page load (5s timeout × 2 tiers = up to 10s per page load).
_provider_cache: dict = {}
_PROVIDER_CACHE_TTL = 300  # seconds


def cached_tier_available(tier: str, config: dict) -> bool:
    """Return tier availability from cache, refreshing every 5 minutes.

    Fast-path: if Anthropic is in the cascade chain and the CLI is configured
    (``ANTHROPIC_API_KEY`` or ``JF_ANTHROPIC_API_KEY`` set), short-circuit to
    True without probing other providers — avoids 2-5s Ollama health-check
    timeouts on cold start.
    """
    now = time.monotonic()
    entry = _provider_cache.get(tier)
    if entry and (now - entry[1]) < _PROVIDER_CACHE_TTL:
        return entry[0]

    # Fast path: Anthropic CLI configured and in the chain → available.
    # resolve_provider_config raises ValueError when providers.primary is unset
    # (2026-05-17 hotfix Fix 4a) — fall through to the boolean predicate which
    # translates that to False.
    if is_anthropic_available():
        from job_finder.web.model_provider import resolve_provider_config

        try:
            resolved = resolve_provider_config(tier, config)
        except ValueError:
            resolved = None
        if resolved is not None:
            providers = [resolved["provider"]] + [
                e["provider"] for e in resolved["fallback_chain"]
            ]
            if "anthropic" in providers or "anthropic_api" in providers:
                _provider_cache[tier] = (True, now)
                return True

    result = tier_has_configured_provider(tier, config)
    _provider_cache[tier] = (result, now)
    return result
