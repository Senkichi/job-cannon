"""Migration 42 — extend classification enum vocabulary to include 'low_signal' (no-op DDL).

The jobs.classification column is plain TEXT with no CHECK constraint, so this
migration is a no-op DDL change at the schema level — the column already
accepts arbitrary strings. The purpose is to bump PRAGMA user_version (so
environments at version 41 advance to 42) and to document the new allowed
enum value via the application-layer rule in
`job_finder.db.derive_classification`.

Allowed classification values (post-Migration-42):
    apply       — all sub-scores ≥ 3 (and no overrides)
    consider    — all sub-scores ≥ 2 (and no overrides)
    skip        — fallback bucket
    reject      — legitimacy_note truthy OR any sub-score == 1
    low_signal  — NEW: enrichment_tier='exhausted' AND jd_full short
                  (per scoring.low_signal_jd_chars; default 1500). Surfaces
                  genuinely-no-signal jobs honestly instead of rolling them
                  into apply/consider/skip via unreliable rubric outputs.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=42,
    description="extend classification enum vocabulary to include 'low_signal' (no-op DDL — column has no CHECK)",
    sql=["SELECT 1"],
)
