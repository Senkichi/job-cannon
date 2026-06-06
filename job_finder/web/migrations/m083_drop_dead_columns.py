"""Migration 83 — drop dead columns opus_score, eval_blocks, job_archetype.

Phase 49.06 (spec §13 commit 49.06; D-11; D-13; §4.4 dead weight).

Pre-drop audit (2026-06-05, run across job_finder/ scripts/ tests/):
  - ``opus_score``    — 58 stale rows (all pre-2026-03-26); only appears in the
                        ``JobRow`` TypedDict hint (doc-only, .get() access) — no
                        production writer. Added m019, superseded by v3 scoring.
  - ``eval_blocks``   — 0 rows ever populated; only a guarded template display
                        branch read it. No writer anywhere.
  - ``job_archetype`` — 15/11,740 rows populated; the only writer
                        ``persist_job_archetype`` had NO production caller (tests
                        + public-surface listing only). Dropped together with the
                        dead writer in this commit.
The ``gold_*`` columns are PRESERVED (used by the eval workflow).

SQLite 3.45+ supports ``ALTER TABLE DROP COLUMN`` directly. The migration runner
swallows ``no such column`` so re-runs after a partial application are idempotent.
No down helper — the drop is intentionally irreversible per spec §17 rollback
(re-adding is a forward migration; the data is gone).
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=83,
    description="drop dead columns opus_score, eval_blocks, job_archetype",
    sql=[
        "ALTER TABLE jobs DROP COLUMN opus_score",
        "ALTER TABLE jobs DROP COLUMN eval_blocks",
        "ALTER TABLE jobs DROP COLUMN job_archetype",
    ],
)
