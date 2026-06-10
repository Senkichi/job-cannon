"""Migration 89 — retag scoring_costs rows written under API-key Anthropic transport.

Issue 303 (2026-06-10): prior to this fix every cascade AnthropicProvider call
wrote provider='anthropic' regardless of whether the claude CLI was running under
subscription OAuth ($0) or a per-token-billed ANTHROPIC_API_KEY.  The F2
correction (m057 / 2026-05-26) moved 'anthropic' into FREE_PROVIDERS on the
assumption of subscription transport, inadvertently making API-key spend invisible.

Post-fix, API-key transport writes provider='anthropic_api' (NOT in FREE_PROVIDERS)
so cost_gate and budget accounting apply.  Historical rows from the broken window
(post-F2 through this migration) cannot be retroactively classified by transport
mode — we have no way to tell which rows came from an API-key session vs. an
OAuth session.  Those rows all have cost_usd=0 (forced by _maybe_record_cost's
FREE_PROVIDERS check) so re-tagging them as 'anthropic_api' with cost_usd still 0
would be misleading without the real token counts.

This migration is therefore intentionally a no-op SQL pass.  Its purpose is to
document the schema event boundary so future audits can correlate the gap period
(between m057 and this migration) with potentially under-reported spend.

If you know you were using ANTHROPIC_API_KEY during that window and want to
investigate real spend, query:
    SELECT * FROM scoring_costs
    WHERE provider = 'anthropic'
      AND timestamp >= '2026-05-26T00:00:00Z'
      AND timestamp < '<this migration timestamp>';
Those rows have cost_usd=0 but real token counts — multiply by MODEL_PRICING to
estimate the missing spend.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=89,
    description="document anthropic API-key transport retag boundary (Issue 303, 2026-06-10)",
    sql=[
        # No-op: existing 'anthropic' rows with cost_usd=0 cannot be reliably
        # distinguished as subscription vs. API-key; leave them as-is.
        # Future rows from API-key transport are written as 'anthropic_api' by
        # AnthropicProvider (see job_finder/web/providers/anthropic_provider.py).
        "SELECT 1",
    ],
)
