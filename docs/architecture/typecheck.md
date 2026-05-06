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
