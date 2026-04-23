---
phase: 34-greenfield-scorer-rewrite
plan: 05
status: complete
commit: feat(34-05) -- see git log
---

# Plan 5 Summary — Migration 41 destructive legacy-score column drop

**Objective:** Drop the transient legacy `haiku_score` / `haiku_summary` / `sonnet_score` columns and the `idx_jobs_haiku_score` index from the live SQLite DB via a backup-gated migration. Plans 1-4 landed the v3.0 ordinal scorer (classification + sub_scores_json) and converged the dataset; Plan 5 is the tight destructive commit that removes the now-unreferenced carry-over columns and collapses the schema.

## What landed

### Schema changes (live DB, `jobs.db`)
- `user_version`: 40 → 41
- Columns dropped: `haiku_score`, `haiku_summary`, `sonnet_score`
- Index dropped: `idx_jobs_haiku_score`
- Columns preserved (per D-13): `fit_analysis` (rationale payload), `scoring_provider`, `scoring_model`, `eval_blocks`, `opus_score`, `score`, `job_archetype`, `legitimacy_note`, `classification`, `sub_scores_json`
- Total column count: 40 → 37

### Backup-gate preflight (D-14)
- `_check_backup_recent()` in `job_finder/web/db_migrate.py`: globs `backup_userdata_*.tar.gz` in cwd; raises `MigrationBlockedError` if no match or the newest match is >24h old. `GSD_BACKUP_CONFIRMED=1` env var bypasses the check.
- Migration 41 calls the gate before any DDL; fail-closed default.
- `tests/conftest.py` sets `GSD_BACKUP_CONFIRMED=1` session-wide so the full migration chain runs unblocked in every fixture. Individual tests exercising the real gate logic monkeypatch it away.

### Runtime support for callable migrations
- `_apply_migration()` now accepts either a list of SQL statement strings (existing pattern) OR a callable(conn) that performs arbitrary preflight + SQL. Migration 41 is the first callable entry; the legacy list-based shape is preserved for all 40 prior migrations.

### Active code cleanup (beyond Plan 5's declared scope)
Plan 4E removed the legacy scoring *modules* but several readers of the columns survived. Plan 5 swept them:

| File | Change |
|---|---|
| `job_finder/db.py` | Removed `haiku_score`, `haiku_summary`, `sonnet_score` from `JOBS_ALL_COLUMNS`; removed `_LEGACY_SORT_ALIAS = {"haiku_score", "sonnet_score"}` bookmark-alias set and its two references in `get_filtered_jobs` |
| `job_finder/web/backfill_enrichment.py` | Two `ORDER BY COALESCE(haiku_score, 0) DESC` clauses: the enrichment pass now orders by classification-rank CASE (apply > consider > skip > reject > NULL, matching `agentic_enricher.py:497`); the scoring backfill orders by `first_seen DESC` |
| `job_finder/web/interview_prep.py` | Dropped `sonnet_score` from the `SELECT title, company, jd_full, sonnet_score, fit_analysis` (value was never read) |
| `job_finder/web/scoring_types.py` | `JobRow` TypedDict: removed `haiku_score` / `sonnet_score` keys; added `classification`, `sub_scores_json`, `scoring_provider`, `scoring_model` |
| `job_finder/web/db_migrate.py` | `_run_retroactive_dedup_once` legacy-column `UPDATE` swapped to null `classification`/`sub_scores_json`/`fit_analysis` (dead code post-Mig 6 sentinel, but would have spammed warnings after Mig 41 drops the columns) |

### Test updates
| File | Change |
|---|---|
| `tests/conftest.py` | `os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")` at module top |
| `tests/test_migration.py` | `TestMigration2`: asserts legacy cols *absent* post-full-chain (were present pre-Mig41); `idempotent` test migrated to assert `fit_analysis` survives + legacy cols gone; added 6 new `TestMigration41DestructiveShape` tests + 5 `TestMigration41BackupGate` tests; count-is-thirteen / count-is-19 asserts updated from 40 → 41 |
| `tests/test_db.py` | `_insert_job(classification=None, sub_scores_json=None)` helper signature replaces `sonnet_score=None, haiku_score=None` |
| `tests/test_scoring_orchestrator.py` | `test_does_not_write_legacy_shim_columns` replaced by `test_legacy_shim_columns_do_not_exist` (schema-level invariant — no column, no shim possible); `test_skipped_status_is_passthrough` reads `classification`/`sub_scores_json`/`fit_analysis` |
| `tests/test_agentic_enricher.py` | `_insert_job` fixture: stripped legacy columns from INSERT statement and defaults dict |
| `tests/test_backfill_enrichment.py` | `insert_job` fixture: legacy kwargs pre-stripped via `kwargs.pop()`, INSERT uses v3 column list; `test_ordering_by_score_desc` renamed `test_ordering_by_classification_rank` with `apply`/`reject`/None seeding |
| `tests/test_resume.py` | 9 direct INSERT sites: removed `sonnet_score`/`haiku_score` columns + values; `"sonnet_score": 85.0` dict literals migrated to `"classification": "apply"`; `test_quick_apply_returns_400_when_no_sonnet_score` renamed to `test_quick_apply_returns_400_when_not_classified_apply` |
| `tests/test_views.py` | 5 INSERT sites: legacy columns removed; seeded rows use `classification` + `sub_scores_json` where scored state was relevant; `test_save_jd_no_scoring_triggered` asserts classification/sub_scores_json NULL after save-jd (save-jd never populates them) |

## Preflight grep status (ARCHITECTURE.md 457-498)

All production code paths (`job_finder/`, `tests/`) are clean of reader references. Remaining matches are:

| Category | Locations | Disposition |
|---|---|---|
| Docstrings / history comments | `rejection_patterns.py:22,123`, `rejection_analyzer.py:133`, `scoring_orchestrator.py:87-88`, `resume_generator.py:416`, `careers_crawler.py:612`, `agentic_enricher.py:497`, `exclusion_filter.py:83`, `backfill_enrichment.py`, `claude_client.py:131` | Keep — narrative about Plan 3/4/5 migration history |
| `purpose='haiku_score'` cost labels | `tests/test_claude_client.py`, `tests/test_costs.py`, `tests/test_migration.py:1149`, `scripts/shootout_lib/baseline.py:58` | Keep — `scoring_costs.purpose` is a string column; `haiku_score` is just a historical purpose label, not the dropped `jobs` column |
| Historical migrations 1/7/13 data-fixup | `job_finder/web/db_migrate.py:114-116,438-536` | Keep — these run on a fresh DB *before* Mig 41 drops the columns, so in-sequence execution is safe |
| Legacy-site-name in Phase 33 shootout | `scripts/shootout_lib/{baseline,candidates,report}.py`, `scripts/v3_shootout.py`, `scripts/quality_cascade_latest.json`, `tests/test_shootout_lib.py`, `tests/test_v3_production_path_refit.py` | Keep — `"haiku_score"` is used as a shootout-matrix site-name label, not a DB column reference |
| One-off diagnostic / remediation scripts | `scripts/debug_db.py`, `scripts/linkedin_targeted_remediation.py`, `scripts/remediate_jd_contamination.py`, `scripts/v3_rescore.py`, `scripts/v3_rescore_validate.py`, `scripts/v3_shootout.py` | Will fail if re-invoked against the live DB — superseded and out of scope for Plan 5. Flagged in "Known follow-ups" below |

## Known follow-ups (out of Plan 5 scope)

- **Diagnostic scripts** still reference `haiku_score` / `sonnet_score` in raw SQL (`debug_db.py`, remediation scripts). They'll `OperationalError` on the live DB. None are on the production hot path or the scheduler; triggering requires an explicit `python scripts/<name>.py` invocation. Remove or migrate when next needed.
- **Phase 33 shootout baseline-generation code** (`scripts/shootout_lib/baseline.py` SELECT of `j.sonnet_score`, `j.haiku_score`) would fail today if you re-ran the shootout. Phase 33 is closed; these are reference-only.
- **`eval_blocks` column** was retained by D-13 but no v3.0 code writes it. Candidates for a future Plan X cleanup sweep.

## Backup / rollback

- **Backup used:** `jobs.db.backup-pre-mig41-20260423-113419` (49.4 MB, pre-migration copy of the live DB). `backup_userdata.sh` produces directories not tarballs, so the `GSD_BACKUP_CONFIRMED=1` override was used to pass the preflight gate — responsibility accepted per the checkpoint:human-verify protocol.
- **Rollback:** stop any running app + scheduler, `cp jobs.db.backup-pre-mig41-20260423-113419 jobs.db`, `git revert <plan-5-commit>`. Nothing else was mutated irreversibly; the commit is single-file-scope enough that revert is clean.

## Verification

- Migration applied:
  - `PRAGMA user_version` → 41
  - `PRAGMA table_info(jobs)` grep haiku/sonnet → 0 matches
  - `sqlite_master` index scan for `idx_jobs_haiku%` → 0 rows
  - Data: 2212 apply / 1059 consider / 651 reject / 546 NULL (unchanged from Plan 4 final state)
- Tests: `uv run --active pytest -q --tb=line` → 2408 passed, 5 skipped, 1 deselected, 0 failed
- Smoke: Flask app boots, `GET /jobs?classification=apply`, `GET /dashboard`, `GET /jobs?sort=classification` all return 200

## Phase 34 completion

**5 of 5 plans shipped. 62 requirement IDs closed. v3.0 ordinal scorer is the sole code path; no legacy columns, modules, shims, or dual-writes remain in production.**

- Plan 1 — Migration 40 additive schema + v3 scorer module (COMPLETE)
- Plan 2 — Orchestrator dual-write behind `use_unified_scorer` flag, flag flipped (COMPLETE)
- Plan 3 — Read-layer migration: query-layer + batch-scoring + dashboard + templates + resume gate (COMPLETE)
- Plan 4 — Rescore sweep B1+B2+B3+B4 (3922 rows) + legacy module deletion (COMPLETE)
- Plan 5 — Migration 41 destructive column drop + preflight cleanup (COMPLETE)

v3.0 milestone `Single-Tier Ordinal Scoring` delivered.
