---
phase: 34-greenfield-scorer-rewrite
plan: 01
subsystem: scoring
tags: [scorer, migration, dataclass, v3, ordinal]
requires:
  - Phase 33 Plan 2 frozen v3 prompt module (sha256 255c690e...d0c9da)
  - Phase 24 shared call_model dispatcher + ModelResult contract
provides:
  - Migration 40 (additive schema) — classification, sub_scores_json, scoring_model columns + idx_jobs_classification
  - db.JobAssessment dataclass (frozen, 6 sub-scores + classification + rationale + provider)
  - db.derive_classification(sub_scores, legitimacy_note) — Python 4-way rule (D-06)
  - db.persist_job_assessment(conn, dedup_key, assessment, provider, model) — row writer
  - web.job_scorer.score_job(job, conn, config, client) — pure-function scorer
  - web.job_scorer.ScoringResult envelope (@dataclass frozen)
  - web.job_scorer.JOB_ASSESSMENT_SCHEMA re-export
affects:
  - job_finder/web/db_migrate.py (append Migration 40)
  - job_finder/db.py (JOBS_ALL_COLUMNS extended; new dataclass + persist fn)
  - tests/test_migration.py (len(MIGRATIONS)==40 assertions; TestMigration40 class)
  - tests/test_db.py (TestJobAssessmentDataclass, TestDeriveClassification, TestPersistJobAssessment)
tech-stack:
  added: []
  patterns:
    - "v3 ordinal assessment schema ratified in production type system (D-05)"
    - "Python-derived classification rule at persist time (D-06 anti-pattern 3 defense)"
    - "legitimacy_note sourced from the jobs row at persist time, not from LLM output (D-07)"
    - "fit_analysis column REUSED for rationale payload (D-08) — zero-change downstream"
    - "scorer routes through shared call_model(tier='scoring', ...) dispatcher (D-09)"
key-files:
  created:
    - job_finder/web/job_scorer.py
    - tests/test_job_scorer.py
  modified:
    - job_finder/web/db_migrate.py
    - job_finder/db.py
    - tests/test_migration.py
    - tests/test_db.py
key-decisions:
  - "D-05 (ratified in code): JobAssessment is @dataclass(frozen=True) with sub_scores (dict[str,int]), classification (str), rationale (dict), provider (str|None)"
  - "D-06 (ratified in code): derive_classification runs rule order legitimacy→any1→all>=3→all>=2→skip"
  - "D-08 (ratified in code): rationale payload persists to reused fit_analysis column as JSON"
  - "D-09 (ratified in code): score_job calls call_model(tier='scoring', output_schema=JOB_ASSESSMENT_SCHEMA); no scorer-specific dispatcher code"
  - "D-12 (ratified in code): Migration 40 is additive only — legacy columns untouched; Plan 5 Migration 41 removes them"
  - "D-28 (ratified in test docstring): ordinal stability (not byte-identical) is the determinism success criterion for v3 scoring"
  - "Tier registration (Task 3 Step 5): resolve_provider_config's peer-inherit fallback handles tier='scoring' without config.yaml changes. Plan 2 adds an explicit providers.scoring block; Plan 1 stays out of config.yaml entirely per the task constraints."
requirements-completed:
  - SCORER-01
  - SCORER-02
  - SCORER-03
  - SCORER-04
  - SCORER-05
  - SCORER-06
  - SCORER-07
  - SCORER-08
  - SCORER-09
  - SCORER-10
  - SCORER-13
  - MIGRATE-01
  - MIGRATE-02
  - TESTS-16
  - TESTS-18
  - TESTS-20
duration: 38 min
completed: 2026-04-22
---

# Phase 34 Plan 1: v3 Scorer Skeleton + Migration 40 Summary

Greenfield v3.0 scorer skeleton — pure-function addition with zero
production callers. Migration 40 adds three columns (`classification`,
`sub_scores_json`, `scoring_model`) plus `idx_jobs_classification`;
`JobAssessment` + `derive_classification` + `persist_job_assessment`
land in `db.py`; `job_scorer.py` defines `score_job()` routing through
the shared `call_model(tier="scoring", ...)` dispatcher against the
Phase 33 frozen v3 prompt. Observable system behavior is byte-equivalent
to pre-Plan-1 state: legacy Haiku→Sonnet two-tier path remains fully
operational and the new scorer is not yet invoked anywhere.

- Duration: 38 min (2026-04-22T04:35:38Z → 2026-04-22T05:13:49Z)
- Tasks: 3 of 3 complete, each committed individually
- Files: 4 modified + 2 created

## Commits

| # | Hash | Task | What |
|---|------|------|------|
| 1 | f9ece88 | Task 1 | Migration 40 additive schema + JOBS_ALL_COLUMNS extension + 6 TestMigration40 tests |
| 2 | ef6002b | Task 2 | JobAssessment + derive_classification + persist_job_assessment + 25 tests |
| 3 | b2985c3 | Task 3 | job_scorer.py module + 21 tests |

## Verification

Per plan `<verification>`:

| Check | Result |
|-------|--------|
| `uv run --active pytest -q --tb=short` exits 0 | PASS — 2588 passed, 1 skipped |
| `PRAGMA table_info(jobs)` has classification/sub_scores_json/scoring_model | PASS (via TestMigration40) |
| `PRAGMA user_version` returns 40 | PASS (via test_migration_40_user_version_increments) |
| `python -c "from job_finder.web.job_scorer import score_job, ScoringResult, JOB_ASSESSMENT_SCHEMA; print('ok')"` | PASS — prints `ok` |
| `grep -rn "from job_finder.web.job_scorer" job_finder scripts` returns zero | PASS — no production callers |
| Git log shows atomic per-task commits | PASS — 3 distinct `feat(34-01):` commits |

Test delta: +46 net tests (2542 baseline → 2588 after Plan 1).
  - +6 Migration 40 shape/index/idempotency/defaults
  - +25 in test_db.py (JobAssessment + derive_classification + persist_job_assessment)
  - +21 in test_job_scorer.py (SCORER-05 skip, happy path routing, error paths, schema contract, module invariants)

## Acceptance Criteria — per task

### Task 1: Migration 40 + JOBS_ALL_COLUMNS

- `classification TEXT DEFAULT NULL` in db_migrate.py: line 725 — PASS
- `sub_scores_json TEXT DEFAULT NULL` in db_migrate.py: line 726 — PASS
- `scoring_model TEXT DEFAULT NULL` in db_migrate.py: line 727 — PASS
- `idx_jobs_classification` CREATE INDEX: line 728 — PASS
- `classification`/`sub_scores_json`/`scoring_model` in JOBS_ALL_COLUMNS: line 26 — PASS
- `>= 2` tests matching `def test_migration_40`: 6 present — PASS
- `pytest tests/test_migration.py` exits 0: 99 passed — PASS
- Full suite green: 2542 passed — PASS

### Task 2: JobAssessment + derive_classification + persist_job_assessment

- `class JobAssessment` with `@dataclass(frozen=True)`: line 33-34 — PASS
- `def derive_classification`: line 57 — PASS
- `def persist_job_assessment`: line 354 — PASS
- `persist_haiku_score` / `persist_sonnet_score` still present (300, 324): PASS
- `_SUB_SCORE_KEYS` tuple with 6 keys matching D-05: line 23 — PASS
- `if legitimacy_note` reject branch: line 80 — PASS
- `any(v == 1` reject branch: line 82 — PASS
- `all(v >= 3` apply branch: line 84 — PASS
- `all(v >= 2` consider branch: line 86 — PASS
- `COALESCE` in persist_job_assessment UPDATE: lines 409-410 — PASS
- `pytest tests/test_db.py` exits 0: 51 passed — PASS
- Full suite green: 2567 passed — PASS

### Task 3: job_scorer.py

- File exists and ≥ 150 lines: 220 lines — PASS
- `def score_job` exactly once: line 148 — PASS
- `class ScoringResult` with `@dataclass(frozen=True)` decorator: line 60 — PASS
- `from job_finder.web.scoring_prompts.v3_scoring_prompt import`: line 29 — PASS
- `tier="scoring"` inside score_job: line 191 — PASS
- `if not jd` SCORER-05 skip path: line 179 — PASS
- `from job_finder.web.job_scorer` returns zero hits in production code — PASS
- `ordinal stability` / `D-28` in test module docstring: lines 3, 6 — PASS
- `>= 9` test functions: 21 present — PASS
- `pytest tests/test_job_scorer.py` exits 0: 21 passed — PASS
- Full suite green: 2588 passed — PASS
- tier="scoring" accepted by call_model: verified via live `resolve_provider_config('scoring', ...)` — resolves through peer-inherit fallback (no config.yaml change needed in Plan 1; Plan 2 adds explicit providers.scoring block).

## Deviations from Plan

None — plan executed exactly as written. Three minor clarifications worth noting for Plan 2's executor (not deviations from Plan 1):

1. **Plan's `_coerce_assessment` skeleton used `data.get("sub_scores")` (nested-dict assumption).** The actual v3 schema (`JOB_ASSESSMENT_SCHEMA` in `v3_scoring_prompt.py`) emits the 6 sub-score fields at the TOP LEVEL of the response alongside `rationale` and `legitimacy_note` — not nested under a "sub_scores" key. The shipped `_coerce_assessment` extracts them by iterating `_SUB_SCORE_KEYS` against top-level keys. Task 3 test class `TestSchemaContract.test_coerce_extracts_sub_scores_from_top_level` pins this invariant.

2. **`call_model`'s `job_id` parameter is typed `str | None` in the actual signature** (not `int | None` as shown in the plan's `<interfaces>` block). `score_job` passes `job.get("dedup_key")` — a str — which matches the real signature.

3. **Task 3 Step 5 tier registration: the peer-inherit fallback path was sufficient.** No change to `_TIER_DEFAULTS` or `config.example.yaml` was required in Plan 1 — `resolve_provider_config('scoring', cfg)` cleanly resolves via `peer-inherit when tier_cfg empty` (model_provider.py:151-161) + `_TIER_DEFAULTS.get(tier, DEFAULT_MODEL_SONNET)` fallback (line 164). Live verification via `uv run --active python -c "from job_finder.web.model_provider import resolve_provider_config; print(resolve_provider_config('scoring', {}))"` confirms no KeyError. Plan 2 owns the explicit `providers.scoring` block addition.

Total deviations: 0 auto-fixed, 0 escalated. Impact: none.

## Issues Encountered

None.

## Authentication Gates

None — Plan 1 is pure-function addition with mocked tests; no external API touched.

## Next Phase Readiness

Plan 1 is **byte-equivalent to pre-Plan-1** in observable behavior — the new scorer is defined but has no production callers. Plan 2 will:

1. Add a `use_unified_scorer: bool` config flag (default False initially, flipped True in a follow-up commit)
2. Wire `score_and_persist_job()` to call `score_job` + `persist_job_assessment` + write a legacy-shim (haiku_score/sonnet_score derived from sub_scores) atomically
3. Add explicit `providers.scoring` to `config.example.yaml` and `config.yaml` (Edit tool — never Write per CLAUDE.md)
4. Route `pipeline_runner.run_ingestion` through the new path when the flag is True

Plan 2's executor should read the three clarifications in "Deviations from Plan" before wiring — especially the top-level sub-score extraction pattern and the `job_id: str | None` signature.

Ready for Wave 2 / Plan 34-02.
