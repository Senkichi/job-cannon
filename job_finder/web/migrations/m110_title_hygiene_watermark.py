"""Migration 110 — raw_title forensic column + title_hygiene_version watermark.

This is the storage half of the title-hygiene robustness work. It establishes
the two things the standing re-sweep (``_run_title_resweep_if_stale`` in
``_post_hooks.py``) needs, mirroring exactly how m100 set up the dedup re-key:

1. ``raw_title TEXT DEFAULT NULL`` on ``jobs``. Until now ``title`` was immutable
   after INSERT — there was no sanctioned writer that rewrites a stored title.
   The re-sweep introduces the first one (it REPAIRS legacy card-text titles,
   e.g. "Data Scientist / IA Engineer Jun 15, 2026 View Job ->" ->
   "Data Scientist / IA Engineer"). ``raw_title`` preserves the pre-rewrite
   original the first time a row's title is machine-rewritten, so the repair is
   reversible per-row and a future (fixed) cleaner can re-run against the TRUE
   original rather than an already-cleaned value. It is sparse by design:
   non-NULL only on rows whose stored title was rewritten.

2. ``title_hygiene_version`` seeded to ``'0'`` in ``schema_meta`` (created by
   m100). Seeding to 0 — below the live ``TITLE_HYGIENE_VERSION`` (1) — arms the
   first re-sweep on next startup, which re-cleans + re-validates every existing
   row under the current contract and quarantines/declassifies the residue. On a
   fresh/empty DB the sweep finds no rows and simply stamps the watermark to 1.

   Seeding is ``INSERT OR IGNORE`` keyed on the watermark, so re-running this
   migration never clobbers a watermark the hook has already advanced (same
   idempotency contract as m100's dedup_normalizer_version seed).

Frozen-in-time note (MI-4): the watermark key ``'title_hygiene_version'`` and the
seed value ``'0'`` are inlined here, not imported, so a future rename of the
constant cannot alter what this migration does to historical DBs.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=110,
    description="raw_title forensic column + title_hygiene_version watermark (versioned title re-sweep)",
    sql=[
        # Idempotent: the runner swallows "duplicate column name" so this is a
        # no-op on DBs that already have the column.
        "ALTER TABLE jobs ADD COLUMN raw_title TEXT DEFAULT NULL",
        # schema_meta exists since m100. Seed 0 (below live version) to arm the
        # first re-sweep; OR IGNORE so an already-advanced watermark is preserved.
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('title_hygiene_version', '0')",
    ],
)
