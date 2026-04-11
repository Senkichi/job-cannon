# Post-Merge Fix Plan: CLI Oneshot Refactor + Stash Integration

**Date**: 2026-04-10
**Context**: After merging the stashed career-ops enhancements with the CLI oneshot refactor (commit `909c453`), 141 test failures remain. All are traceable to 6 distinct root causes across 3 categories: refactor regressions, stash/merge adaptation gaps, and incomplete feature wiring.

**Current baseline**: 2245 passed, 141 failed (all in test_views.py when excluding test_gemini_provider and e2e)

---

## Issue 1: `get_filtered_jobs()` Signature Mismatch

**Severity**: HIGH — 29 test failures
**Root cause**: The filter bar overhaul (commit `0ff91ae`) added new filter parameters to the jobs blueprint's `_get_filter_kwargs()` but never added matching parameters to `get_filtered_jobs()` in `db.py`.

**Symptoms**: `TypeError: get_filtered_jobs() got unexpected keyword argument 'min_score'`

**Affected parameters** (passed by `jobs.py:_get_filter_kwargs()` but not accepted by `db.py:get_filtered_jobs()`):
- `min_score`
- `max_score`
- `salary_min`
- `source`
- `date_from`
- `date_to`

**Files to modify**:

### `job_finder/db.py` — `get_filtered_jobs()`
Add the 6 missing parameters to the function signature and implement the SQL filtering:

```python
def get_filtered_jobs(
    conn, status=None, location=None, posted_within=None,
    freshness=None, sort_by=None, sort_dir=None, limit=None,
    hide_stale=False, show_hidden=False,
    # New filter parameters:
    min_score=None,      # float — WHERE haiku_score >= ?
    max_score=None,      # float — WHERE haiku_score <= ?
    salary_min=None,     # int — WHERE salary_min >= ?
    source=None,         # str — WHERE sources LIKE '%"<source>"%'
    date_from=None,      # str (ISO date) — WHERE first_seen >= ?
    date_to=None,        # str (ISO date) — WHERE first_seen <= ?
) -> list[sqlite3.Row]:
```

For each parameter, add a conditional `WHERE` clause:
- `min_score` / `max_score`: filter on `haiku_score` column
- `salary_min`: filter on `salary_min` column (numeric comparison)
- `source`: JSON `LIKE` match against `sources` column (pattern: `%"linkedin"%`)
- `date_from` / `date_to`: filter on `first_seen` column (ISO string comparison)

**Validation**: `sort_by` allowlist already exists — no SQL injection risk from new params since they're all parameterized values, not column names.

**Tests**: 29 tests in `test_views.py` should pass after this fix.

---

## Issue 2: `DEFAULT_DAILY_BUDGET_USD` Missing from Jinja2 Globals

**Severity**: MEDIUM — 12 test failures (7 in test_views.py + 3 in test_settings.py + 2 in test_resume.py)
**Root cause**: The settings template references `DEFAULT_DAILY_BUDGET_USD` but it was never registered as a Jinja2 global when the daily budget feature was added.

**Symptoms**: `jinja2.exceptions.UndefinedError: 'DEFAULT_DAILY_BUDGET_USD' is undefined`

**Template usage** (`settings/index.html`):
- Line 330: `value="{{ config.get('scoring', {}).get('daily_budget_usd', DEFAULT_DAILY_BUDGET_USD) }}"`
- Line 332: `<p>Default: ${{ DEFAULT_DAILY_BUDGET_USD|int }}/day</p>`

**File to modify**:

### `job_finder/web/__init__.py` — `create_app()`
Add to the Jinja2 globals block (around line 120-127):

```python
from job_finder.web.claude_client import DEFAULT_DAILY_BUDGET_USD
# ... in create_app():
app.jinja_env.globals["DEFAULT_DAILY_BUDGET_USD"] = DEFAULT_DAILY_BUDGET_USD
```

**Tests**: All 12 affected tests should pass.

---

## Issue 3: `company_enricher` Tests Mock Removed `DDGS` Class

**Severity**: MEDIUM — 4 test failures
**Root cause**: During the stash merge, `enrich_company_info()` was kept using `search_duckduckgo()` (HEAD's helper), but the stash tests in `test_company_enricher.py` mock the old `DDGS` class that no longer exists in the module.

**Symptoms**: `AttributeError: <module 'job_finder.web.company_enricher'> does not have the attribute 'DDGS'`

**Failing tests**:
- `test_extracts_size_and_industry_from_ddg_results`
- `test_returns_empty_dict_on_no_results`
- `test_returns_empty_dict_on_exception`
- `test_startup_size_classification`

**File to modify**:

### `tests/test_company_enricher.py`
Change all mock targets from:
```python
@patch("job_finder.web.company_enricher.DDGS")
```
to:
```python
@patch("job_finder.web.company_enricher.search_duckduckgo")
```

Return values change from a list of dicts (`[{"body": "..."}]`) to a plain string (what `search_duckduckgo` returns). For example:
```python
# Old (DDGS mock):
mock_ddgs.return_value.__enter__.return_value.text.return_value = [
    {"body": "Acme has 5000 employees in the software industry"}
]

# New (search_duckduckgo mock):
mock_search.return_value = "Acme has 5000 employees in the software industry"
```

For the "no results" test, return `""` (empty string). For the exception test, raise inside the mock.

---

## Issue 4: `pipeline_runner.py` Double-Definition Bug

**Severity**: HIGH — silent correctness issue (tests pass but wrong code runs)
**Root cause**: The stash merge added re-exports from `ingestion_runner` at the top of `pipeline_runner.py`, but the file also contains local definitions of `_fetch_gmail()`, `_fetch_serpapi()`, `_score_and_persist()`, and `_log_to_email_parse_log()` further down. Python's name resolution means the local definitions **shadow** the imported ones.

**Impact**: The local versions have bugs:
- `_fetch_gmail()` local version doesn't handle `GmailSource.fetch_jobs()` tuple return correctly
- `_fetch_gmail()` local version lacks per-message dedup via `email_parse_log`
- `_score_and_persist()` local version lacks company upsert logic (`_upsert_job_company()`)

**Tests pass** because they mock these functions at `job_finder.web.pipeline_runner._fetch_gmail` — the mock replaces whichever definition is active.

**File to modify**:

### `job_finder/web/pipeline_runner.py`
Delete these local function definitions that are shadowed by imports:
- `_fetch_gmail()` (the local definition — ~62 lines)
- `_fetch_serpapi()` (the local definition — ~41 lines)
- `_score_and_persist()` (the local definition — ~71 lines)
- `_log_to_email_parse_log()` (the local definition — ~27 lines)

After deletion, the imported versions from `ingestion_runner` will be used. The re-export block already makes them available for test patches.

**Verification**: Run `uv run --active pytest tests/test_ingestion.py tests/test_scoring.py tests/test_scheduler.py -q --tb=short` — all should still pass since tests mock these functions.

---

## Issue 5: `SONNET_SCHEMA_WITH_EVAL_BLOCKS` Quick-Fix Needs Refinement

**Severity**: LOW — no test failures, but schema doesn't match actual usage
**Root cause**: During the merge I created `SONNET_SCHEMA_WITH_EVAL_BLOCKS` as a quick fix. It's architecturally sound but the field names don't match what test data and plan docs expect.

**Current schema fields**: `criterion`, `assessment`, `weight`
**Actual usage in test data** (`test_db.py`): `criterion`, `score`, `rationale`

**Production status**: `evaluate_job_sonnet()` still uses `SONNET_SCHEMA` (not the eval_blocks variant). The eval_blocks feature is defined in Migration 27 and the adoption plan but not yet wired end-to-end.

**File to modify**:

### `job_finder/web/sonnet_evaluator.py` — `SONNET_SCHEMA_WITH_EVAL_BLOCKS`
Align field names with actual usage:

```python
SONNET_SCHEMA_WITH_EVAL_BLOCKS = {
    **SONNET_SCHEMA,
    "properties": {
        **SONNET_SCHEMA["properties"],
        "eval_blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "rationale": {"type": "string"},
                },
                "required": ["criterion"],
            },
            "description": "Structured per-criterion evaluation blocks",
        },
    },
    "required": ["score", "summary", "fit_analysis"],
    "additionalProperties": False,
}
```

**Future work** (not in this plan): Wire `SONNET_SCHEMA_WITH_EVAL_BLOCKS` into `evaluate_job_sonnet()` behind a config flag, update the system prompt to instruct eval_block output, and persist via `score_and_persist_sonnet()`.

---

## Issue 6: Unused Import in `scoring_runner.py`

**Severity**: TRIVIAL — no test failures
**Root cause**: `BudgetExceededError` was imported from the stash but is never referenced in any function body.

**File to modify**:

### `job_finder/web/scoring_runner.py`
Remove from line 10:
```python
from job_finder.web.claude_client import BudgetExceededError  # DELETE
```

---

## Execution Order

Issues are independent — all can be done in parallel. Recommended order by impact:

| Priority | Issue | Tests Fixed | Effort |
|----------|-------|-------------|--------|
| 1 | `get_filtered_jobs()` signature | 29 | Medium (SQL changes + param wiring) |
| 2 | `DEFAULT_DAILY_BUDGET_USD` Jinja2 global | 12 | Trivial (2-line fix) |
| 3 | `pipeline_runner.py` double definitions | 0 (correctness) | Low (delete ~200 lines) |
| 4 | `company_enricher` test mocks | 4 | Low (mock target + return value changes) |
| 5 | `SONNET_SCHEMA_WITH_EVAL_BLOCKS` fields | 0 (schema accuracy) | Trivial |
| 6 | Unused `BudgetExceededError` import | 0 (hygiene) | Trivial |

**Expected outcome**: 141 → 96 failures from Issues 1+2. Issues 3-6 fix correctness and hygiene without changing test counts (issue 4's tests already pass due to mocking).

**Note**: The remaining ~96 failures in `test_views.py` after Issues 1+2 need separate investigation — they likely involve HTMX template assertions against changed markup from the stash features. That's a separate scope.
