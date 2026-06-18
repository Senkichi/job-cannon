# Migrations

Schema evolution for `jobs.db` is managed by a hand-rolled migration runner —
no Alembic, no SQLAlchemy. The system is intentionally simple: each migration
is a Python module that declares a `Migration` value object, and the runner
applies pending migrations in order using `PRAGMA user_version` as the
sentinel.

## File layout

```
job_finder/web/
├── db_migrate.py                      # 76-line runner; public entry point
└── migrations/
    ├── __init__.py                    # Discovery: assembles MIGRATIONS at import
    ├── types.py                       # Migration / MigrationContext dataclasses
    ├── _gate.py                       # MigrationBlockedError + _check_backup_recent
    ├── _runner.py                     # _apply_migration (one migration → DB)
    ├── _post_hooks.py                 # _run_rekey_if_stale (standing dedup re-key, post-loop)
    └── m001_initial_schema.py         # One file per migration, version in name
        m002_ai_scoring_columns.py
        ...
        m048_drop_rejection_interview.py
```

The naming convention is **mandatory**: `m{NNN:03d}_<snake_case_description>.py`.
Three-digit zero-padding ensures `pkgutil.iter_modules` returns modules in
version order (without it, `m010` sorts before `m2`). The discovery pass also
sorts by `MIGRATION.version` defensively, so a renamed file would still
produce a correctly ordered list — but
[`tests/test_migration_invariants.py`](../../tests/test_migration_invariants.py)
asserts the convention regardless.

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

1. **Versions are append-only and never renumber.** The PRAGMA user_version
   IS the migration version. A user with `PRAGMA user_version = 30` running
   the latest code expects migrations 31, 32, … to apply. Renumbering a
   shipped migration breaks every existing user's database. (`MI-4` in the
   project's master invariants.)

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

4. **Three-digit zero-padding in filenames is mandatory.** Without it,
   `pkgutil.iter_modules` would visit `m010_*.py` before `m002_*.py` and
   the discovery pass would assemble the list in the wrong order. The
   defensive `sort by version` after discovery would correct the runtime
   list, but the source-tree ordering would still be misleading to anyone
   reading the package.

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

1. Pick the next available version number (currently 49+).
2. Create `migrations/m{version:03d}_<description>.py` with a `MIGRATION`
   constant.
3. Write the SQL with per-statement idempotency in mind: `CREATE TABLE IF
   NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, etc.
4. Bump `EXPECTED_MIGRATION_COUNT` in `tests/test_migration_invariants.py`
   so the count-sentinel test acknowledges the addition.
5. Run `uv run --active pytest tests/test_migration_invariants.py
   tests/test_migration.py tests/test_db_migrate.py` to verify.
6. If the migration needs filesystem or env state, define a `py`-helper
   that takes a `MigrationContext`. See
   [`m041_drop_legacy_scores.py`](../../job_finder/web/migrations/m041_drop_legacy_scores.py)
   for the reference shape.

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
