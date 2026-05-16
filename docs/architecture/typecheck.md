# Type-Checking Strategy

## Active baseline tool: `mypy`

`mypy~=1.13` is the project's authoritative type-checker. The pre-commit
hook and CI signal use mypy. `pyright~=1.1` is also pinned and installed
as a complementary check (it's faster and sometimes catches different
issues), but it is opt-in — there is no CI gate on its output.

Configuration lives in `pyproject.toml` under `[tool.mypy]` and
`[tool.pyright]`. Both are pinned in `[project.optional-dependencies.dev]`
so the error set is reproducible across machines.

## Session 5 baseline (commit `ecdc302`, 2026-05-06)

Run from a clean `uv sync --extra dev --extra eval` against the
`job_finder/` package only (tests + scripts excluded by config):

| Tool   | Errors | Files | Source files checked |
|--------|--------|-------|----------------------|
| mypy   | 123    | 39    | 108                  |
| pyright| 46     | —     | (job_finder include) |

Raw outputs are captured at:
- `.planning/portfolio-cleanup/mypy-baseline.txt` (gitignored — local only)
- `.planning/portfolio-cleanup/pyright-baseline.txt` (gitignored — local only)

### Mypy error mix (rough proportions)

About a quarter of the mypy errors are `[import-untyped]` from missing
third-party stubs (`types-requests`, `types-PyYAML`). The rest are real
signal: `[arg-type]` and `[assignment]` mismatches concentrated in
`careers_scraper.py`, `careers_page_interactions.py`,
`pipeline_runner.py`, `ats_scanner.py`, and `web/blueprints/settings.py`.
None block runtime — these are static-analysis findings on a codebase
that previously had no type-checker enforcement.

## Why mypy over pyright as the active tool

Both tools were evaluated. mypy was chosen even though pyright reported
fewer errors at this snapshot:

1. **De-facto Python standard.** A Lead/Staff reviewer recognizing
   "type-clean under mypy" requires no extra context. Pyright is also
   well-known but is more frequently associated with editor tooling
   (Pylance) than with project-level discipline.
2. **Python-native distribution.** mypy is pure Python; pyright wraps
   a Node.js binary. Running CI in a Python-only environment is one
   less moving part.
3. **Stricter unannotated-call surface by default.** This project has
   no type annotations on most function bodies. mypy's diagnostics on
   that surface are louder and more actionable than pyright's, which
   matches the Session 8 plan to migrate callers to typed `Settings`.
4. **Lower follow-up cost on `import-untyped`.** Installing
   `types-requests` and `types-PyYAML` will silence ~25% of the mypy
   delta in one step. There is no equivalent quick win for pyright's
   smaller-but-deeper findings.

Pyright stays installed because it is faster on incremental checks
(useful during refactors) and because cross-checking two tools
periodically catches blind spots in either one.

## Configuration posture

Initial config is intentionally gentle:

- **mypy:** `ignore_missing_imports=true`, `warn_return_any=false`,
  `warn_unused_ignores=true`, `warn_redundant_casts=true`. Tests and
  scripts are excluded. Tightening (e.g., `disallow_untyped_defs`,
  `strict_optional`) lands in Session 8 alongside the Settings caller
  migration — not before, because tightening before callers are typed
  produces noise without signal.
- **pyright:** `typeCheckingMode="basic"`, `pythonVersion="3.13"`,
  same exclude set. Strict mode is reserved for after Session 8.

## CI / pre-commit integration

A local pre-commit hook is added in Session 5 at `--hook-stage manual`
(opt-in). It does not run on every commit. The intent is to keep
type-check available as a deliberate gate (`pre-commit run --hook-stage
manual --all-files`) until the error count is brought down below the
threshold a per-commit gate makes sense.

Promotion to the default pre-commit stage is a Session 9 / 10 item,
contingent on the baseline shrinking enough that contributors aren't
gated by pre-existing errors.

## Reproducing the baseline

```powershell
uv sync --extra dev --extra eval
uv run --active mypy job_finder
uv run --active pyright
```

The numbers in the table above should match (within drift from
upstream stub releases).

## Session 6 re-measurement (commit `3b2c796`, 2026-05-06)

After the migrations split (`db_migrate.py` → `db_migrate.py` + the
`migrations/` package with 53 files), the mypy file count
redistributed as expected.

| Tool   | Errors    | Files     | Source files checked | Δ errors |
|--------|-----------|-----------|----------------------|----------|
| mypy   | 121 (-2)  | 38 (-1)   | 162 (+54)            | -2       |
| pyright| 45 (-1)   | —         | (job_finder include) | -1       |

Both tools came in cleaner post-S6:

- **mypy** lost 2 errors when `db_migrate.py` shrank from a 1099-line
  monolith to a 76-line driver. The inline MIGRATIONS list contained
  `Migration` entries that mypy couldn't fully infer; replacing them
  with imports from per-file modules collapsed the type surface
  enough that two `[arg-type]` errors disappeared. The new
  `migrations/_runner.py`, `_gate.py`, `_post_hooks.py`, and the 48
  `m{NNN}_*.py` files all came in mypy-clean from the start (the S6
  refactor was opportunistic about adding annotations on the new
  surfaces).
- **pyright** lost 1 error from the same simplification.

Source-files checked grew by 54 (the per-version migration modules + the
new test files), which is the expected redistribution. With the
typed `Migration` dataclass in place, future migrations will produce
fewer untyped-call warnings than the legacy list-of-list-or-callable
shape.

### Cross-check note: `mypy .` vs `mypy job_finder`

The S5 baseline reproduces with `mypy job_finder` (scoped). Running
`mypy .` from the repo root picks up `backups/` (operator-managed
backup directory not on `.gitignore`'s exclude list nor in `[tool.mypy]
exclude`), which adds 4 unrelated `[var-annotated]` errors in a stale
investigative-script copy from April. These are not S6 regressions and
are out of scope for this session — adding `backups/` to the mypy
exclude is a Session 8 / Session 9 lint-cleanup item.

### Raw outputs

`.planning/portfolio-cleanup/mypy-baseline.txt` and
`pyright-baseline.txt` retain the S5 outputs as the immutable anchor.
The S6 measurement is recorded in this file only — the baseline
artifacts are regeneratable per the reproducing block above and don't
need a snapshot for every session.

## Session 7a re-measurement (commit `9cecbc4`, 2026-05-06)

After the scheduler-package split (`scheduler.py` → `scheduler/__init__.py`
+ 6 sibling modules: `_pidfile`, `_ollama`, `_factories`, `_jobs`,
`_runners`, `_sync`), the type-check baseline holds at the S6-close
numbers. The +6 source files are the new package modules; zero new errors
were introduced by the split.

| Tool   | Errors    | Files     | Source files checked | Δ errors |
|--------|-----------|-----------|----------------------|----------|
| mypy   | 121 (=)   | 38 (=)    | 168 (+6)             | 0        |
| pyright| 45 (=)    | —         | (job_finder include) | 0        |

One latent type issue surfaced during the split and was fixed in the
same session:

- `scheduler/_runners.py:run_enrichment_backfill_two_stage` returns a
  dict that mixes `int` and `list` values. When the body lived as a
  closure nested two levels deep inside `init_scheduler`, mypy's nested-
  function relaxation skipped the strict inference. Promoted to a top-
  level function, mypy infers `dict[str, object]` and trips on
  `result["errors"].append(...)` with `[attr-defined]`. Annotating
  `result: dict[str, Any]` (and the function return type) restores the
  baseline. Recorded as a refactor-surface for similar latent issues
  in S7b–7e: a closure→top-level extraction can surface mypy errors
  that runtime never saw.

The migrations-package decomposition pattern from S6 (private modules
under a package directory + re-exports from `__init__.py`) was
re-applied successfully here. None of the new scheduler modules
introduced type errors of their own.

Reproducing block unchanged. The S5 raw-output anchor remains immutable.

## Session 7b re-measurement (commit `fd21ed2`, 2026-05-06)

After the pipeline_detector-package split (`pipeline_detector.py` →
`pipeline_detector/__init__.py` + 5 sibling modules: `_constants`,
`_gmail`, `_signals`, `_db`, `_processing`), the type-check baseline
holds at the S7a-close numbers. The +5 source files are the new
package modules; zero new errors were introduced by the split.

| Tool   | Errors    | Files     | Source files checked | Δ errors |
|--------|-----------|-----------|----------------------|----------|
| mypy   | 121 (=)   | 38 (=)    | 173 (+5)             | 0        |
| pyright| 45 (=)    | —         | (job_finder include) | 0        |

No closure → top-level promotion happened in S7b (every extracted
function was already top-level in the legacy monolith), so the latent-
issue lesson from S7a (the `dict[str, Any]` annotation needed for the
runners' result dict) did not recur. The seven `_signals.py` functions,
the four `_db.py` helpers, and `_processing.py:_process_email` are all
mypy-clean from the start.

The S6 migrations pattern + S7a scheduler pattern is now the canonical
shape for 7-series module splits: lifecycle-only `__init__.py` +
focused private modules + re-exports for the test contract.

Reproducing block unchanged. The S5 raw-output anchor remains immutable.

## Session 7c re-measurement — 2026-05-06

After splitting `job_finder/web/ats_scanner.py` (863 LOC) into the
`ats_scanner/` package (6 files: `__init__.py`, `_upsert.py`, `_probe.py`,
`_promote.py`, `_run.py`, `_run_html.py`):

- **mypy `job_finder`:** **112 errors / 38 files / 167 source files**.
  Improvement of **-9 errors** vs. S6 close (121 / 38 / 162). Source
  files +5 (the new package modules). File count unchanged at 38.
- **pyright `job_finder`:** **45 errors / 0 warnings / 0 informations**.
  Unchanged from S6 close.

The mypy -9 improvement comes from the shape change rather than an
explicit type-annotation pass: the slim `__init__.py` drops 50+ lines
of imports (json, sqlite3, time, datetime, derive_classification,
standalone_connection, strip_html_to_text, plus the four lazy-import
try/except blocks). Several of those globals had `Any`-typed shapes
(e.g., `score_and_persist_job = None  # type: ignore[assignment]`)
that propagated through every reference inside the original 470-line
`run_ats_scan`. Splitting that function into typed helpers in `_run.py`
made the local type-narrowing tighter — the lazy globals only live in
`_run.py` / `_run_html.py` now, where the outer phase guards narrow
them at the call sites. New phase-helper signatures (`summary: dict`,
`all_new_job_keys: list`) also let mypy infer return shapes more
precisely than the inline-loop original.

The S7c code itself (the six new package modules + the test patches)
is mypy-clean from the start. No new mypy/pyright errors were
introduced; the -9 improvement is downstream of the refactor.

Note: at the time of original measurement Session 7a was in flight in
a parallel worktree, and S7c branched from `d1d20a9` (the scheduler
package-layout commit on main). Post-rebase onto the S7a+S7b tip, the
112/45 numbers hold — none of S7c's changes touch scheduler or
pipeline_detector types, and S7a/S7b each closed at 121/45 with zero
delta, so the -9 improvement composes cleanly.

### Reproducing block (S7c)

```bash
uv run --active mypy job_finder | tail -1
# Found 112 errors in 38 files (checked 167 source files)

uv run --active pyright job_finder | tail -1
# 45 errors, 0 warnings, 0 informations
```

Same scoping as S5/S6: `mypy job_finder` (NOT `mypy .`, per the S6
cross-check note above).

## Session 7e re-measurement (`portfolio/s7e-careers-crawler-split`, 2026-05-06)

After the careers_crawler split (`careers_crawler.py` → 8-module
package), the mypy delta is purely mechanical and the pyright total
is unchanged.

Numbers below are the post-reconciliation measurement after the S7a→S7b→S7c→S7e
linear-rebase landed on `main`. Anchor is the S7c-close baseline (112 / 38 / 167).

| Tool   | Errors      | Files       | Source files checked | Δ errors |
|--------|-------------|-------------|----------------------|----------|
| mypy   | 113 (+1)    | 41 (+3)     | 187 (+20)            | +1       |
| pyright| 45 (=)      | —           | (job_finder include) | =        |

The s7e-induced mypy additions are not new design issues — they are
duplicates of pre-existing errors that previously lived once in
`careers_crawler.py` and now appear once per sub-module that inherited
the offending import. Specifically:

- `[import-untyped] Library stubs not installed for "requests"` — the
  original file had this once; post-split it appears in `__init__.py`,
  `_static_tier.py`, `_playwright_tier.py`, and `_api_cache.py` (each
  of which still imports `requests` directly to do its own HTTP). +3.
- `[union-attr] Item "AttributeValueList" has no attribute "strip"`
  follows `_extract_jobs_from_soup` from `careers_crawler.py:172` to
  `_static_tier.py:87`. Same error, new home. 0 net change.
- `[arg-type] / [attr-defined]` in the `summary` dict (the
  "errors": list-vs-int union the type-checker can't reconcile) stay
  in `__init__.py` where `_crawl_companies` lives — same count as
  pre-split.

The new sub-modules themselves (8 of them) and the new
`_http_constants.py` come in mypy-clean except for the inherited
`requests` import noise. The new `tests/test_careers_crawler_invariants.py`
is excluded from mypy by config (tests are not type-checked at this
phase).

Source-files checked grew by 20 over the S7c-close anchor — 6 from
the S7a scheduler split, 5 from the S7b pipeline_detector split, and 9
from S7e itself (8 new careers_crawler sub-modules + 1 new
`_http_constants.py`). Files-with-errors grew by 3 (the three new tier
modules that import `requests`). pyright did not move at all — its
include set saw the same 45 errors before and after, since the new
modules pyright sees are clean of pyright-specific issues.

The headline mypy delta from the S7c-close anchor is +1 rather than the
+3–4 a naïve sum of the careers_crawler `requests` duplicates would
predict. The original pre-rebase s7e measurement (125 against a 121
S6-close anchor) reflected an s7e tip that had not yet seen S7c's -9
improvement; once both land on `main`, the per-error categories below
all hold but the totals compose to 113 rather than 116. The exact
3-error gap was not bisected as part of this reconciliation — every
listed error category remains present and accounted for in the
post-rebase output.

S9 lint cleanup will be the natural place to install
`types-requests` (silences the 4 duplicate import-untyped errors
across the careers_crawler package in a single step) and to adjust
the `summary` dict's typed shape.

### Reproducing for S7e

```powershell
uv run --active mypy job_finder    # 113 errors / 41 files / 187 source files
uv run --active pyright            # 45 errors
```

Both invocations match S5 / S6's invocation conventions: `mypy job_finder`
(scoped, NOT `mypy .`) so apples-to-apples comparison holds.

## Session 7d re-measurement (`portfolio/s7d-db-split`, 2026-05-06)

S7d split `job_finder/db.py` (845 LOC) into a package
(`job_finder/db/__init__.py` + `_classification.py` + `_persistence.py` +
`_jobs.py` + `_queries.py`). Public surface preserved via PEP 484 explicit
re-export form (`as X`). Anchor: S7e-close numbers above.

| Tool | Before (S7e-close) | After (S7d-close) | Delta |
|---|---|---|---|
| `mypy job_finder` | 113 errors / 41 files / 187 source files | **106 errors / 36 files / 191 source files** | **−7 errors, −5 files, +4 source files** |
| `pyright` | 45 errors | **45 errors** | **unchanged** |

Source-file count rose by 4 because the monolith became a package with
4 new private modules (`_classification.py`, `_persistence.py`, `_jobs.py`,
`_queries.py`). The original `db.py` is gone from the count; net `+4`
matches the four new files.

The mypy −7 / −5 files improvement is concentrated in two effects:

1. **Concentrated type-narrowing.** When a long module containing several
   distinct concerns is split, mypy's per-function inference no longer has
   to reconcile broader union types across unrelated call sites. The split
   makes it easier for mypy to follow narrower types within each
   sub-module — same effect S7c reported (-9 mypy errors when a slim
   `__init__.py` reduced cross-concern import surface).
2. **Drop of one explicit `dict | tuple` ambiguity** in the original
   `upsert_job` body's `jd_full_value = ()` line: the moved version in
   `_jobs.py` carries an explicit `jd_full_value: tuple = ()` annotation
   so mypy no longer re-infers an Any-flavored union when the conditional
   re-assigns it. (Annotation added at extraction time; not a behavioral
   change.)

Pyright unchanged: the `as X` re-export form silences `reportUnusedImport`
on every re-export site in `db/__init__.py`, neutralizing what would
otherwise have been ~25 new pyright complaints from the multi-file
re-export pattern. The pre-existing 45-error baseline (concentrated in
`gemini_provider`, `companies` blueprint, `pipeline_runner`, etc.) held
exactly.

### Raw output excerpts (S7d)

mypy tail:
```
job_finder\web\pipeline_runner.py:185: error: Unsupported left operand type for + ("object")  [operator]
job_finder\web\backfill_companies.py:419: error: Incompatible types in assignment (expression has type "int | None", variable has type "int")  [assignment]
Found 106 errors in 36 files (checked 191 source files)
```

pyright tail:
```
c:\Users\yourname\repos\job-cannon\job_finder\web\scoring_orchestrator.py:131:9 - error: Argument of type "Unknown | None" cannot be assigned to parameter "dedup_key" of type "str" in function "persist_job_assessment"
45 errors, 0 warnings, 0 informations
```

### Reproducing for S7d

```powershell
uv run --active mypy job_finder    # 106 errors / 36 files / 191 source files
uv run --active pyright            # 45 errors
```

The new private sub-modules in `job_finder/db/` are scanned the same way
as any other package member; no special configuration was added in S7d.

## Reconciliation R4 re-measurement (`portfolio/r4-typecheck-reconciled`, 2026-05-06)

R4 closed Findings F-D1 through F-D5 from `.planning/PORTFOLIO_RECONCILIATION_PLAN.md`.
Three small surface changes — none touched runtime behavior — net out
to the largest mypy reduction since S5:

| Tool | After S7d-close (originally documented) | After R4-close (clean-cache 1.20.2) | Delta |
|---|---|---|---|
| `mypy job_finder` | 106 errors / 36 files / 191 source files | **96 errors / 31 files / 191 source files** | **−10 errors, −5 files, ±0 source files** |
| `pyright` | 45 errors | **44 errors** | **−1 error** |

A complementary clean-cache anchor: at the s7d tag (and at R0 / pre-R4
HEAD), `mypy 1.20.2` reports **114 / 40 / 191** — F-D1.5's mypy-version
drift accounts for the gap against the originally-documented 106. From
that consistent-tooling anchor, R4 is **−18 errors / −9 files**.

### What R4 changed

R4.2 — `pyproject.toml [tool.mypy].exclude` extended to include `backups/`.
The exclude pattern now covers `tests/`, `scripts/`, `build/`, `dist/`,
and `backups/`. This makes `mypy job_finder` and `mypy .` produce
identical counts on machines that have user-data backups checked out
(F-D2 closed).

R4.3 — `types-requests~=2.32` added to `[project.optional-dependencies.dev]`
(and `uv.lock` regenerated). The careers_crawler split (S7e) had multiplied
the `[import-untyped] Library stubs not installed for "requests"` warning
across every sub-module that imports `requests` directly. Installing the
stubs silences all of them and additionally resolves the same warning in
non-careers-crawler modules (e.g. `enrichment_tiers.py`,
`backfill_companies.py`, parsers/, sources/) — accounting for the
−15 / −8 reduction (significantly larger than the F-D3-predicted −4).
F-D3 closed.

R4.4 — three `summary`-shaped dicts in `job_finder/web/careers_crawler/__init__.py`
annotated as `dict[str, Any]`, mirroring the S7a pattern in
`scheduler/_runners.py:42`. The dicts mix integer counters with an
`errors: list[str]` slot; without the explicit annotation, mypy infers
the value type as `object` and rejects `.extend(...)` / `.append(...)`
on the list slot. Sites annotated:

- `crawl_careers_batch.summary` (line 144) — outer literal.
- `_crawl_worker.local_summary` (line 312) — from `dict.fromkeys(_SUMMARY_KEYS, 0)`.
- `_crawl_companies.merged_summary` (line 475) — tightened from bare `: dict` for consistency.

F-D4 closed.

### What R4 deferred

R4.5 — the pre-commit hook stays at `--hook-stage manual`. F-D5 remains
open and is scoped to S9 (Lint Cleanup). At 96 mypy errors the per-commit
cost is still material (~10–20s for `mypy job_finder` from a cold cache),
and there is no CI mypy gate today, so promoting to `pre-push` would have
been bounded gain at material cost. S9 is the right place for this when
combined with `mypy --baseline` so contributors aren't gated by
pre-existing errors.

### Reproducing for R4

```powershell
uv run --active mypy job_finder    # 96 errors / 31 files / 191 source files
uv run --active pyright            # 44 errors
```

Clean-cache discipline (`Remove-Item .mypy_cache -Recurse -Force` before
the run) recommended for any cross-tag bisect; in normal use the cache
is fine.
