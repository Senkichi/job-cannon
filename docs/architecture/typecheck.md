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
