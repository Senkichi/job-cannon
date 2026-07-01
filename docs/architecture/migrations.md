# Migrations

Schema evolution for `jobs.db` is managed by a hand-rolled migration runner —
no Alembic, no SQLAlchemy. The system is intentionally simple: each migration
is a Python module that declares a `Migration` value object, and the runner
applies pending migrations in version order.

**"Applied" is set membership, not a scalar.** Applied migrations are recorded
as rows in a `schema_migrations` ledger table (see
[`_ledger.py`](../../job_finder/web/migrations/_ledger.py)); the pending set is
`[m for m in MIGRATIONS if m.version not in applied_versions(conn)]`. This is
the Rails/Django/Flyway/Liquibase convergence point, and it eliminates the
silent-skip bug the old single-`PRAGMA user_version` high-water mark had: under
the scalar scheme, two parallel branches would both pick "the next" number and
the loser (sorting at/below the merged `user_version`) was **silently skipped
forever**. With the ledger, a migration merged in below the current max is
simply absent from the set and **runs**. `PRAGMA user_version` is retained only
as a best-effort cache (kept equal to the ledger's `MAX(version)`) for external
inspectors. A legacy DB migrated under the old scheme is backfilled into the
ledger once, on first run (Flyway `baseline` / Alembic `stamp`): every migration
`<= user_version` is marked applied **without re-executing**.

## File layout

```
job_finder/web/
├── db_migrate.py                      # 76-line runner; public entry point
└── migrations/
    ├── __init__.py                    # Discovery: assembles MIGRATIONS + duplicate-version guard
    ├── types.py                       # Migration / MigrationContext dataclasses
    ├── _ledger.py                     # schema_migrations applied-set (ensure/applied_versions/has_run/backfill)
    ├── _gate.py                       # MigrationBlockedError + _check_backup_recent
    ├── _runner.py                     # _apply_migration (one migration → DB + ledger record)
    ├── _post_hooks.py                 # _run_rekey_if_stale (standing dedup re-key, post-loop)
    └── m001_initial_schema.py         # One file per migration, version in name
        m002_ai_scoring_columns.py
        ...
        m048_drop_rejection_interview.py
```

The naming convention is `m{version}_<snake_case_description>.py`. New migrations
are created by [`scripts/new_migration.py`](../../scripts/new_migration.py),
which **mints** the version automatically (see "Adding a new migration" below),
so authors never hand-pick a number. Legacy files use three-digit zero-padding
(`m001`..`m117`); minted files use a wider epoch-second stamp (e.g.
`m205087732_*`). Ordering is authoritative via `discovered.sort(key=lambda m:
m.version)`, not filename lexical order — so zero-padding is no longer
load-bearing for correctness. The discovery regex accepts any digit width
(`m\d+_`), and
[`tests/test_migration_invariants.py`](../../tests/test_migration_invariants.py)
asserts filename-integer == declared version regardless.

## The Migration value object

Each `m*.py` file declares a single `MIGRATION` constant:

```python
from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=42,
    description="extend classification enum vocabulary to include 'low_signal'",
    sql=["SELECT 1"],
)
```

The four fields:

| Field | Type | Purpose |
|---|---|---|
| `version` | `int` | 1-based migration number. PRAGMA user_version is set to this after the migration commits. **Never renumber a shipped migration.** |
| `description` | `str` | One-line summary suitable for log output. |
| `sql` | `list[str]` | SQL statements run in order, each in its own `conn.execute()` call. Per-statement idempotency is handled by the runner. |
| `py` | `Callable[[MigrationContext], None] \| None` | Optional Python helper for migrations that need filesystem or env state. Currently only Migration 41 uses this. |

`MigrationContext` carries the connection plus context that `py`-helpers might
need (`db_path`, `user_data_root`). It exists so helpers stay pure — they do
not reach into module globals.

## The non-negotiable rules

These are enforced by tests in `tests/test_migration_invariants.py`. CI fails
loudly if any of them slip:

1. **Versions are append-only and never renumber.** A shipped migration's
   version is recorded in every existing user's `schema_migrations` ledger (and
   backfilled from `user_version` for legacy DBs). Renumbering a shipped
   migration would make the old row an orphan and re-run the renamed file,
   breaking existing databases. (`MI-4` in the project's master invariants.)
   Existing files keep their small integers; **new** migrations get minted
   epoch-second stamps from `scripts/new_migration.py`.

2. **Migrations are append-only as files too.** Once a migration ships, you
   do not edit its file. A bug in a shipped migration is fixed by a NEW
   migration that compensates (e.g., Migration 12 fixes Migration 10's
   mistake of adding ATS-retry columns to `jobs` instead of `companies`,
   and Migration 13 drops Migration 10's columns).

3. **Every migration is idempotent on a partially-applied schema.** The
   runner swallows two SQLite `OperationalError` substrings:
   - `"duplicate column name"` — for `ALTER TABLE ADD COLUMN` re-runs.
   - `"no such column"` — for `ALTER TABLE DROP COLUMN` re-runs.
   Every other DDL must self-guard. Use `CREATE TABLE IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`, `DROP INDEX IF EXISTS`, `DROP TABLE IF
   EXISTS`. The intermediate-version resume test in
   `test_migration_invariants.py` exercises this contract end to end.

4. **No two migrations may share a version.** The discovery pass
   (`_verify_unique_versions` in
   [`migrations/__init__.py`](../../job_finder/web/migrations/__init__.py))
   hard-fails at import / CI on a duplicate version. This is the loud backstop
   behind the minted-stamp workflow: a hand-typed collision fails immediately
   instead of silently skipping a migration at runtime. (Ordering is by
   `m.version` after discovery, so filename zero-padding is cosmetic, not
   load-bearing.)

## The backup-recency gate (Migration 41)

Migration 41 is the only destructive migration that ships with a preflight
gate. It drops three columns from the `jobs` table (the legacy
`haiku_score`, `haiku_summary`, `sonnet_score`) which the v3.0 ordinal
rubric scoring made redundant.

Before running, the gate (`_check_backup_recent` in
[`_gate.py`](../../job_finder/web/migrations/_gate.py)) confirms one of:

- A `backup_userdata_*.tar.gz` file exists in the user-data root and is less
  than 24 hours old, OR
- `GSD_BACKUP_CONFIRMED=1` is set in the environment.

The env-var override exists for operators who use alternate backup schemes
(time-machine snapshots, ZFS datasets, manual `.backup` copies). Failing
both checks raises `MigrationBlockedError`, which propagates out of
`run_migrations` — the migration loop halts before any DDL has run.

`MigrationContext.user_data_root` is the path the gate globs against,
which lets the gate run cleanly under `monkeypatch.chdir(tmp_path)` in the
test suite.

## Adding a new migration

1. Run `python scripts/new_migration.py "<short description>"`. It **mints** a
   collision-free version (an epoch-second stamp, e.g. `205087732`) and writes
   `migrations/m{stamp}_<slug>.py` with a `MIGRATION` skeleton. You never pick a
   number — so two parallel branches never collide, and worker/agent prompts
   never carry a stale "next is mNNN".
2. Fill in `sql=[...]` with per-statement idempotency in mind: `CREATE TABLE IF
   NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, guarded `ALTER` (the runner
   swallows `duplicate column name` / `no such column`).
3. If the migration needs filesystem or env state, add a `py`-helper that takes
   a `MigrationContext`. See
   [`m041_drop_legacy_scores.py`](../../job_finder/web/migrations/m041_drop_legacy_scores.py)
   for the reference shape.
4. Add `tests/test_migration_<slug>.py` and verify:
   `uv run --active pytest tests/test_migration_invariants.py
   tests/test_migration.py tests/test_db_migrate.py tests/test_migration_ledger.py`.

## Migration history (one-line summaries)

| Range | Era |
|---|---|
| 1–6 | Foundation: jobs/runs/pipeline tables, AI scoring columns, Phase 5/6 intelligence + data-quality |
| 7–17 | Companies + ATS discovery, enrichment-tier tracking, retry metadata |
| 18–24 | Multi-provider scoring attribution, jd_full data-quality fixes, dashboard indexes |
| 25–34 | Career-ops scoring metadata, careers-page caching, ghost-job detection, liveness columns (later removed) |
| 35–40 | Careers crawler tier caching, AI navigation recipes, v3 ordinal rubric scoring |
| 41 | Drop legacy haiku/sonnet score columns (the only `py`-helper migration; backup-gated) |
| 42–46 | Classification vocabulary, gold-set labeling, eval-harness history, Workday URL bug heal |
| 47–48 | Public-repo cleanup: drop resume-generation, Drive feedback, interview prep, rejection report tables |

For the per-migration detail, read the `MIGRATION.description` in each
`m*.py` file — they are short and load at module import (no separate
metadata file to drift from the source).

## Known historical hazards

- **Migration 10/12/13** — Migration 10 added ATS retry columns to the wrong
  table (`jobs` instead of `companies`). Migration 12 added them to
  `companies` correctly. Migration 13 dropped the columns from `jobs`. The
  three migrations together preserve the version-monotonic invariant
  while fixing the bug.

- **Migration 33/39** — Migration 33 added liveness checker columns; the
  liveness checker module was later merged into the expiry checker.
  Migration 39 drops the dead columns.

- **Migration 7 + comp_data_json fixup in `run_migrations`** — Migration 7
  was historically missing `jobs.comp_data_json`; it was added later. To
  avoid creating a Migration 7.5 (which would break the version sequence),
  `run_migrations` runs a guarded `ALTER TABLE jobs ADD COLUMN
  comp_data_json …` after the loop completes when `final_version >= 7`.
  This is the only out-of-loop schema mutation in the runner.

- **Migrations 47/48** — Public-repo cleanup that removed Phase 4 (resume
  generation) and Phase 5 (interview prep / rejection analysis) features.
  These migrations drop the corresponding tables; the application code
  for those features was removed in the same commit batch.
