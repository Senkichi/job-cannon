"""Migration 82 — drop dead columns: opus_score, eval_blocks, job_archetype.

Audit (2026-06-05) confirmed zero active writers for all three columns:

  opus_score    — 58 stale rows, all pre-2026-03-26. No writer found in
                  job_finder/ or scripts/ (migration 19 added it; no
                  subsequent scorer writes to it).

  eval_blocks   — Schema column added by migration 27; 0 rows ever populated.
                  No writer in the codebase.

  job_archetype — 15/11,740 rows populated (4 distinct values). The only
                  writer, ``persist_job_archetype`` in ``_persistence.py``,
                  was dead code: defined and exported but never called from
                  any application or script path. Removed alongside this
                  migration.

grep audit (job_finder/ scripts/ tests/):
  - No assignment to these columns outside the migration definitions and
    the now-removed ``persist_job_archetype`` dead-code function.
  - Templates reference ``job.job_archetype`` / ``job.eval_blocks`` for
    display only (inside ``{% if %}`` guards). Those guards evaluate to
    False once the columns are absent from JOBS_ALL_COLUMNS.

The ``gold_*`` columns are intentionally PRESERVED — they are used by the
eval workflow.

SQLite 3.45+ supports ``ALTER TABLE … DROP COLUMN`` natively. The migration
runner catches ``no such column`` OperationalErrors, making each statement
idempotent on re-run.

No down helper — the drop is intentionally irreversible per §17 rollback.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=82,
    description="drop dead columns: opus_score, eval_blocks, job_archetype",
    sql=[
        "ALTER TABLE jobs DROP COLUMN opus_score",
        "ALTER TABLE jobs DROP COLUMN eval_blocks",
        "ALTER TABLE jobs DROP COLUMN job_archetype",
    ],
)
