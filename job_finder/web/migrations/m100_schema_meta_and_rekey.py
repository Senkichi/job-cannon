"""Migration 100 — schema_meta table + dedup_normalizer_version seed (D-8).

P4.1 makes dedup_key derivation version-aware (D-8: derived values are
versioned; one-time sentinels are forbidden for derivations that can change).
The actual re-key happens in the standing post-migration hook
(``_run_rekey_if_stale`` in ``_post_hooks.py``), which compares the stored
``dedup_normalizer_version`` against the live ``NORMALIZER_VERSION`` and runs
``run_retroactive_dedup`` whenever they differ.

This migration only establishes the storage and seeds the version watermark
so the hook can decide whether a re-key is owed:

1. Create ``schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`` — a
   generic key/value side-table for derived-data version watermarks.
2. Seed ``dedup_normalizer_version`` IF NOT ALREADY PRESENT:
   - If the legacy once-ever sentinel exists (``merge_log`` row with
     ``merge_source='migration_complete'``) the DB already ran the v1
     retroactive dedup, so its keys are at **version 1** → seed ``'1'``.
   - Otherwise the DB never ran the legacy dedup (fresh install, or a DB that
     somehow predates it) → seed ``'0'`` so the hook performs the first re-key
     to the current version.

   Seeding is `INSERT OR IGNORE` keyed on the watermark, so re-running this
   migration never clobbers a watermark the hook has already advanced.

The seed runs as a ``py`` helper rather than inline SQL because it must read the
sentinel state conditionally; the DDL stays in ``sql`` so ``CREATE TABLE IF NOT
EXISTS`` idempotency is handled by the runner. On a fresh/empty DB this is a
no-op beyond creating the table and writing ``'0'`` — there are no rows to
re-key.

Frozen-in-time note (MI-4): the sentinel string ``'migration_complete'`` and
the watermark key ``'dedup_normalizer_version'`` are inlined here, not imported,
so future renames cannot alter what this migration does to historical DBs.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Inlined constants (MI-4: migrations are frozen in time).
_VERSION_KEY = "dedup_normalizer_version"
_LEGACY_SENTINEL_SOURCE = "migration_complete"


def _seed_version(ctx: MigrationContext) -> None:
    """Seed dedup_normalizer_version from the legacy once-ever sentinel state."""
    conn = ctx.conn

    # Already seeded? Leave the watermark untouched — the standing hook may have
    # advanced it past this seed value already.
    existing = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (_VERSION_KEY,)
    ).fetchone()
    if existing is not None:
        return

    # A DB that ran the legacy v1 retroactive dedup carries the sentinel row.
    # Its keys are at version 1; without the sentinel the DB never ran it.
    seed_value = "0"
    try:
        sentinel = conn.execute(
            "SELECT 1 FROM merge_log WHERE merge_source = ? LIMIT 1",
            (_LEGACY_SENTINEL_SOURCE,),
        ).fetchone()
        if sentinel is not None:
            seed_value = "1"
    except sqlite3.OperationalError:
        # merge_log absent (shouldn't happen post-m006) — treat as never-run.
        seed_value = "0"

    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
        (_VERSION_KEY, seed_value),
    )
    logger.info("m100: seeded %s = %s", _VERSION_KEY, seed_value)


MIGRATION = Migration(
    version=100,
    description="schema_meta table + dedup_normalizer_version seed (D-8 versioned dedup-key)",
    sql=[
        "CREATE TABLE IF NOT EXISTS schema_meta (  key TEXT PRIMARY KEY,  value TEXT NOT NULL)",
    ],
    py=_seed_version,
)
