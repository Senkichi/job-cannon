"""Migration 41 — drop legacy haiku_score/haiku_summary/sonnet_score columns.

Preflight: backup-recency gate (see `db_migrate._check_backup_recent`).

Drops:
    - haiku_score, haiku_summary, sonnet_score columns
    - idx_jobs_haiku_score index

Preserves:
    - fit_analysis (now holds v3.0 rationale payload)
    - scoring_provider, scoring_model
    - eval_blocks, opus_score, score, job_archetype, legitimacy_note
    - classification, sub_scores_json (v3 scoring surface from Mig 40)

No inline rollback — recovery path is a DB restore from the gated backup.
Idempotent via "no such column" handling in `_apply_migration`.
"""

from job_finder.web.migrations._gate import _check_backup_recent
from job_finder.web.migrations.types import Migration, MigrationContext


def _drop_legacy_scores(ctx: MigrationContext) -> None:
    # The gate lives in `migrations._gate` so the import is direct (no
    # cycle through `db_migrate.py`).
    _check_backup_recent(ctx.user_data_root, initial_version=ctx.initial_version)
    ctx.conn.execute("DROP INDEX IF EXISTS idx_jobs_haiku_score")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN haiku_score")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN haiku_summary")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN sonnet_score")


MIGRATION = Migration(
    version=41,
    description="drop legacy haiku_score/haiku_summary/sonnet_score after backup-recency gate",
    py=_drop_legacy_scores,
)
