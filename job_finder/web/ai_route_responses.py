"""Shared degraded-response helpers for AI-backed route fragments."""

from __future__ import annotations

import html


def tier_unavailable_message(tier: str, action: str) -> str:
    """Return a consistent user-facing message for unroutable AI tiers."""
    return (
        f"{tier.capitalize()} tier unavailable. "
        f"{action} requires a configured or reachable provider."
    )


def render_htmx_error_fragment(container_id: str, message: str) -> tuple[str, int]:
    """Return a standard HTMX error fragment with status 200."""
    escaped_message = html.escape(message)
    return (
        f'<div id="{container_id}" class="text-xs text-red-400">'
        f"{escaped_message}"
        f"</div>",
        200,
    )