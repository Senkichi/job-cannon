"""Migration 82 — drop three dead columns from ``jobs``.

Pre-drop audit (2026-06-05) — zero active writers confirmed:

    grep -rn "opus_score\\|eval_blocks\\|job_archetype" job_finder/ scripts/ tests/

Findings (writers vs. readers, excluding test helpers and migration history):

  opus_score:
    - ``job_finder/web/scoring_types.py:JobRow.opus_score`` — TypedDict hint,
      no runtime write.  Column last written before 2026-03-26; 58 stale rows.

  eval_blocks:
    - No production writer found.  Column has 0 rows populated.

  job_archetype:
    - ``job_finder/db/_persistence.py:persist_job_archetype()`` — function is
      exported in ``job_finder.db`` but has **zero non-test callers** in the
      codebase (only ``tests/test_db.py`` calls it).  Dead code, no active
      writer.
    - ``scripts/shootout_lib/baseline.py`` — SELECT reader only, not a writer.
    - Templates ``_row.html`` / ``_row_expanded.html`` — display-only; 15 rows
      populated out of 11,740.

The three columns are removed as Phase 49 dead-weight per D-11:
  §4.4 dead weight in .planning/specs/2026-05-29-ingestion-contract-enforcement.md.

Rollback: no down helper.  The drop is intentionally irreversible per §17
rollback.  Re-adding columns via a follow-up migration is the only rollback
path (data is lost at drop time).

Idempotency: the runner's ``_apply_migration`` catches ``no such column`` on
``ALTER TABLE DROP COLUMN`` and skips silently, so re-running is safe.

SQLite 3.35+ supports ``ALTER TABLE … DROP COLUMN`` directly.
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
