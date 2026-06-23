"""Migration 113 — drop the vestigial ``jobs.score`` column (v3.0 "Plan 4" tail).

Preflight: backup-recency gate (see ``db_migrate._check_backup_recent``),
mirroring m041 — this migration is destructive (a column drop) and the rollback
path is a DB restore from the gated backup.

Background
----------
Under v3.0 single-tier scoring the 0-100 ``jobs.score`` column is vestigial: the
LLM path persists only ``classification`` + ``sub_scores_json`` (the
Python-derived 6-30 composite is the live fit signal), and the ingestion-time
heuristic that used to populate ``score`` is no longer consulted by any read
path. PR #509 removed the last reads (the ``min_score``/``max_score`` filter
shim); this migration removes the dead column itself, finishing the Plan-4
migration tail that m041 deliberately deferred (m041 dropped
haiku_score/sonnet_score but kept ``score``).

Drop order
----------
SQLite's ``ALTER TABLE DROP COLUMN`` refuses if the column is referenced by any
index, trigger, view, or generated column, so the dependents are removed first:

  1. The I-03 contract triggers ``tg_jobs_scoring_provider_when_scored_{ins,upd}``
     (m078). Their ``WHEN`` clause references ``NEW.score`` — leaving them in
     place would make SQLite refuse the column drop. I-03 ("scoring_provider
     required when score is set") is retired together with the column: it has no
     v3.0 analog (the surviving I-04/I-05 already gate the LLM scoring surface on
     ``scoring_model``).
  2. ``idx_jobs_score`` (m001) — indexes ``score(DESC)``.
  3. ``ALTER TABLE jobs DROP COLUMN score`` (SQLite 3.35+; project ships 3.49).

``score_breakdown`` is intentionally left in place — out of scope for this drop.
``computed_status`` (the m082 VIRTUAL generated column) references only
pipeline/staleness/expiry columns, never ``score``, so it does not block the drop.

Idempotency: the trigger/index drops use ``IF EXISTS``; a re-applied
``DROP COLUMN`` raises "no such column: score", which ``_apply_migration``
swallows for destructive migrations.

No inline rollback — recovery is a DB restore from the gated backup (as m041).
"""

from __future__ import annotations

from job_finder.web.migrations._gate import _check_backup_recent
from job_finder.web.migrations.types import Migration, MigrationContext

# The I-03 trigger names created by m078 (``_TRIGGER_BASE["I-03"]`` + _ins/_upd).
# Hardcoded — not imported from m078 — so this migration stays a frozen,
# self-contained artifact decoupled from m078's private constants.
_I03_TRIGGERS = (
    "tg_jobs_scoring_provider_when_scored_ins",
    "tg_jobs_scoring_provider_when_scored_upd",
)


def _drop_score_column(ctx: MigrationContext) -> None:
    # Gate FIRST (mirrors m041): a blocked backup leaves the schema untouched.
    _check_backup_recent(
        ctx.user_data_root,
        initial_version=ctx.initial_version,
        migration_label="Migration 113",
    )
    # Retire the I-03 triggers that reference NEW.score, else DROP COLUMN refuses.
    for trigger in _I03_TRIGGERS:
        ctx.conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    ctx.conn.execute("DROP INDEX IF EXISTS idx_jobs_score")
    ctx.conn.execute("ALTER TABLE jobs DROP COLUMN score")


MIGRATION = Migration(
    version=113,
    description=(
        "drop vestigial jobs.score column + idx_jobs_score + I-03 triggers "
        "after backup-recency gate (v3.0 Plan 4 tail)"
    ),
    py=_drop_score_column,
)
