"""Migration 57 — preserve historical paid Anthropic SDK rows post-F2.

Polish-review F2 (commit c8e698d, 2026-05-26) added "anthropic" to
FREE_PROVIDERS (claude_client.py:70-81) because the post-F2 cascade-routed
AnthropicProvider dispatches via `claude -p` CLI ($0 subscription).

Side effect: every historical scoring_costs row with provider='anthropic'
AND cost_usd > 0 (real paid SDK calls made before the CLI cutover,
M-2 2026-05-20) is now silently excluded from cost rollups (cost_gate,
get_cost_stats, get_monthly_provider_breakdown, etc.) because the
NOT IN (free_providers) filter catches them.

Heal pass:
  Retag pre-F2 paid rows from 'anthropic' -> 'anthropic_sdk' so they
  continue to count as paid spend. The new tag is NOT in FREE_PROVIDERS,
  so the rollup queries see them again.

Discriminator: cost_usd > 0 isolates real paid calls from default-leaked
rows (the m018 column DEFAULT is 'anthropic' and any INSERT that omits
provider has cost_usd = 0 today). Post-F2 free rows have cost_usd = 0
(see AnthropicProvider.call: ModelResult(cost_usd=0.0, ...)).

This migration does NOT fix the m018 column DEFAULT itself — SQLite
cannot ALTER COLUMN DEFAULT without a full table rebuild, which is
deferred. The defense-in-depth runtime guard (record_cost +
_maybe_record_cost assertions, this commit) prevents new default-leak
rows from being written.

See .planning/specs/2026-05-26-polish-review-audit.md (MAJOR —
scoring_costs.provider migration default) and
.planning/specs/2026-05-27-polish-review-followups-plan.md (U6).
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=57,
    description="retag historical paid Anthropic SDK rows from 'anthropic' to 'anthropic_sdk' post-F2 FREE_PROVIDERS flip",
    sql=[
        """UPDATE scoring_costs
              SET provider = 'anthropic_sdk'
            WHERE provider = 'anthropic'
              AND cost_usd > 0""",
    ],
)
