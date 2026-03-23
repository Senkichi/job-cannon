# Port job-finder Improvements to job-cannon — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port ~30 improvements from job-finder into job-cannon via surgical diff-and-apply in 5 dependency-ordered waves, plus implement a new multi-select status filter.

**Architecture:** Most changes are wholesale file replacements (job-finder's version is strictly superior for files where cannon has no unique changes). Surgical edits used only where cannon has unique content to preserve. The multi-select filter is new work on the template + backend query.

**Tech Stack:** Python 3.13, Flask 3.1, HTMX 2.x, SQLite, Jinja2

**Source repo:** `<other-repo>` (referred to as FINDER below)
**Target repo:** `<repo-root>` (referred to as CANNON below, this is our working directory)

**Important:** After each wave, run `uv run pytest tests/ -x` to catch breakage early. Fix any failures before proceeding.

---

## Chunk 1: Wave 1 — Foundation Types & Constants

### Task 1: Create json_utils.py and update imports

**Files:**
- Create: `job_finder/json_utils.py` (copy from FINDER)
- Modify: `job_finder/web/db_helpers.py:16` (update import path)
- Delete: `job_finder/utils.py` (superseded)
- Delete: `job_finder/output/__init__.py` (empty legacy)

- [ ] **Step 1: Copy json_utils.py from FINDER**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/json_utils.py /c/Users/senki/repos/job-cannon/job_finder/json_utils.py
```

- [ ] **Step 2: Update db_helpers.py import**

In `job_finder/web/db_helpers.py`, line 16, change:
```python
# OLD
from job_finder.utils import safe_json_load  # noqa: F401 -- re-exported for backward compatibility
# NEW
from job_finder.json_utils import safe_json_load  # noqa: F401 -- re-exported for backward compatibility
```

- [ ] **Step 3: Delete superseded files**

```bash
rm /c/Users/senki/repos/job-cannon/job_finder/utils.py
rm -rf /c/Users/senki/repos/job-cannon/job_finder/output/
```

- [ ] **Step 4: Verify no remaining imports of job_finder.utils**

```bash
grep -r "from job_finder.utils" /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
grep -r "import job_finder.utils" /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
```

Expected: Hits in `main.py` and `db.py` only (both replaced in later waves). If any other file shows a hit, fix it now.

- [ ] **Step 5: Commit**

```bash
git add job_finder/json_utils.py job_finder/web/db_helpers.py
git rm job_finder/utils.py job_finder/output/__init__.py
git commit -m "refactor: move safe_json_load to json_utils.py, delete empty output package"
```

---

### Task 2: Create scoring_types.py

**Files:**
- Create: `job_finder/web/scoring_types.py` (copy from FINDER)

- [ ] **Step 1: Copy scoring_types.py from FINDER**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/scoring_types.py /c/Users/senki/repos/job-cannon/job_finder/web/scoring_types.py
```

- [ ] **Step 2: Verify module imports cleanly**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "from job_finder.web.scoring_types import JobRow, ScoringResult, format_salary_range; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/scoring_types.py
git commit -m "feat: add scoring_types module (JobRow, ScoringResult, format_salary_range)"
```

---

### Task 3: Update blueprints __init__.py and config.py

**Files:**
- Modify: `job_finder/web/blueprints/__init__.py:5-17` (list → tuple, add frozenset)
- Modify: `job_finder/config.py:19` (add DEFAULT_BORDERLINE_HIGH)

- [ ] **Step 1: Update PIPELINE_STATUSES to tuple and add frozenset**

In `job_finder/web/blueprints/__init__.py`, change:
```python
# OLD
PIPELINE_STATUSES = [
    "discovered",
    "reviewing",
    "applied",
    "phone_screen",
    "technical",
    "onsite",
    "offer",
    "accepted",
    "archived",
    "rejected",
    "withdrawn",
]
# NEW
PIPELINE_STATUSES = (
    "discovered",
    "reviewing",
    "applied",
    "phone_screen",
    "technical",
    "onsite",
    "offer",
    "accepted",
    "archived",
    "rejected",
    "withdrawn",
)

VALID_PIPELINE_STATUSES = frozenset(PIPELINE_STATUSES)
```

- [ ] **Step 2: Add DEFAULT_BORDERLINE_HIGH to config.py**

In `job_finder/config.py`, after line 19 (`DEFAULT_HAIKU_THRESHOLD = 42`), add:
```python
DEFAULT_BORDERLINE_HIGH = 54
```

- [ ] **Step 3: Verify imports**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "from job_finder.web.blueprints import PIPELINE_STATUSES, VALID_PIPELINE_STATUSES; print(type(PIPELINE_STATUSES).__name__, len(VALID_PIPELINE_STATUSES))"
```

Expected: `tuple 11`

```bash
uv run python -c "from job_finder.config import DEFAULT_BORDERLINE_HIGH; print(DEFAULT_BORDERLINE_HIGH)"
```

Expected: `54`

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/blueprints/__init__.py job_finder/config.py
git commit -m "refactor: PIPELINE_STATUSES to tuple, add VALID_PIPELINE_STATUSES frozenset and DEFAULT_BORDERLINE_HIGH"
```

---

### Task 4: Run tests after Wave 1

- [ ] **Step 1: Run full test suite**

```bash
cd /c/Users/senki/repos/job-cannon && uv run pytest tests/ -x -q
```

Expected: All tests pass. The changes so far are additive (new files, one import path change). No test should break because `db_helpers.py` re-exports `safe_json_load` at the same name.

- [ ] **Step 2: Fix any failures before proceeding to Wave 2**

---

## Chunk 2: Wave 2 — Core Module Refactors

### Task 5: Replace db.py with FINDER version

This is the largest and riskiest change. FINDER's db.py replaces the `JobDB` class with module-level functions and adds many new helpers.

**Files:**
- Replace: `job_finder/db.py` (copy from FINDER)

- [ ] **Step 1: Back up current db.py for reference**

```bash
cp /c/Users/senki/repos/job-cannon/job_finder/db.py /c/Users/senki/repos/job-cannon/job_finder/db.py.bak
```

- [ ] **Step 2: Copy FINDER's db.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/db.py /c/Users/senki/repos/job-cannon/job_finder/db.py
```

**Note:** The copied `db.py` does NOT include multi-select status support in `get_filtered_jobs()`. That is added in Task 16 Step 3.


- [ ] **Step 3: Verify key functions exist**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
from job_finder.db import (
    upsert_job, get_job, get_filtered_jobs, update_pipeline_status,
    persist_haiku_score, persist_sonnet_score, load_job_context,
    get_dashboard_stats, get_recent_runs, get_pipeline_summary,
    log_run, get_distinct_locations, get_distinct_sources,
    get_pipeline_events, get_jobs_by_status
)
print('All db functions importable')
"
```

Expected: `All db functions importable`

- [ ] **Step 4: Verify JobDB class is gone**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
try:
    from job_finder.db import JobDB
    print('FAIL: JobDB still exists')
except ImportError:
    print('OK: JobDB removed')
"
```

Expected: `OK: JobDB removed`

- [ ] **Step 5: Remove backup**

```bash
rm /c/Users/senki/repos/job-cannon/job_finder/db.py.bak
```

- [ ] **Step 6: Commit**

```bash
git add job_finder/db.py
git commit -m "refactor: replace JobDB class with module-level functions, add persist helpers and explicit columns"
```

---

### Task 6: Create scoring_orchestrator.py and description_formatter.py

**Files:**
- Create: `job_finder/web/scoring_orchestrator.py` (copy from FINDER)
- Create: `job_finder/web/description_formatter.py` (copy from FINDER)

- [ ] **Step 1: Copy both new modules**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/scoring_orchestrator.py /c/Users/senki/repos/job-cannon/job_finder/web/scoring_orchestrator.py
cp /c/Users/senki/repos/job-finder/job_finder/web/description_formatter.py /c/Users/senki/repos/job-cannon/job_finder/web/description_formatter.py
```

- [ ] **Step 2: Verify imports**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
from job_finder.web.scoring_orchestrator import score_and_persist_haiku, score_and_persist_sonnet, load_scoring_profile
from job_finder.web.description_formatter import format_description_filter
print('OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/scoring_orchestrator.py job_finder/web/description_formatter.py
git commit -m "feat: add scoring_orchestrator (centralized scoring) and description_formatter (extracted filter)"
```

---

### Task 7: Update claude_client.py

**Files:**
- Replace: `job_finder/web/claude_client.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's claude_client.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/claude_client.py /c/Users/senki/repos/job-cannon/job_finder/web/claude_client.py
```

- [ ] **Step 2: Verify key changes are present**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
from job_finder.web.claude_client import DEFAULT_API_TIMEOUT_SECONDS
print(f'Timeout: {DEFAULT_API_TIMEOUT_SECONDS}s')
"
```

Expected: `Timeout: 120s`

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/claude_client.py
git commit -m "fix: add API timeout, graceful unknown model pricing, configurable budget_cap in get_cost_stats"
```

---

### Task 8: Update ats_scanner.py (remove JobDB usage)

**Files:**
- Replace: `job_finder/web/ats_scanner.py` (copy from FINDER)

- [ ] **Step 1: Diff the two versions to check for cannon-only content**

```bash
diff /c/Users/senki/repos/job-cannon/job_finder/web/ats_scanner.py /c/Users/senki/repos/job-finder/job_finder/web/ats_scanner.py | head -60
```

Review the diff. If cannon has unique changes beyond what FINDER has, apply those changes surgically instead of a wholesale copy. If FINDER is strictly superior (all cannon changes are a subset), proceed with the copy.

- [ ] **Step 2: Copy FINDER's ats_scanner.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/ats_scanner.py /c/Users/senki/repos/job-cannon/job_finder/web/ats_scanner.py
```

- [ ] **Step 3: Verify no JobDB imports remain**

```bash
grep -n "JobDB" /c/Users/senki/repos/job-cannon/job_finder/web/ats_scanner.py
```

Expected: No output (zero matches).

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/ats_scanner.py
git commit -m "refactor: replace JobDB with module-level db functions in ats_scanner"
```

---

### Task 9: Update web/__init__.py (app factory)

**Files:**
- Replace: `job_finder/web/__init__.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's __init__.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/__init__.py /c/Users/senki/repos/job-cannon/job_finder/web/__init__.py
```

- [ ] **Step 2: Verify app creates successfully**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
from job_finder.web import create_app
app = create_app(config={'TESTING': True, 'db': {'path': ':memory:'}})
print(f'App created, secret key length: {len(app.secret_key)}')
"
```

Expected: App creates, secret key is a long hex string (64 chars from `token_hex(32)`).

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/__init__.py
git commit -m "refactor: extract format_description to dedicated module, use secrets.token_hex for secret key"
```

---

### Task 10: Run tests after Wave 2

- [ ] **Step 1: Run full test suite**

```bash
cd /c/Users/senki/repos/job-cannon && uv run pytest tests/ -x -q 2>&1 | tail -20
```

Expected: Some tests may fail due to `JobDB` references or changed function signatures. Note which tests fail — they will be fixed in Wave 5 (Task 19). As long as the failures are only test-level (import errors, wrong signatures), not runtime crashes in the main code, proceed to Wave 3.

- [ ] **Step 2: Record failing tests for Wave 5 fix**

List any test files that fail and what the error is (e.g., `ImportError: cannot import name 'JobDB'`).

---

## Chunk 3: Wave 3 — Consumers (Scorers, Runner, Scheduler)

### Task 11: Replace haiku_scorer.py

**Files:**
- Replace: `job_finder/web/haiku_scorer.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's haiku_scorer.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/haiku_scorer.py /c/Users/senki/repos/job-cannon/job_finder/web/haiku_scorer.py
```

- [ ] **Step 2: Verify ScoringResult return type**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
import inspect
from job_finder.web.haiku_scorer import score_job_haiku
sig = inspect.signature(score_job_haiku)
print(f'Params: {list(sig.parameters.keys())}')
print(f'Return: {sig.return_annotation}')
"
```

Expected: Parameters include `experience_profile` (not `profile`). Return annotation is `ScoringResult`.

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/haiku_scorer.py
git commit -m "refactor: haiku_scorer returns ScoringResult, uses experience_profile param"
```

---

### Task 12: Replace sonnet_evaluator.py

**Files:**
- Replace: `job_finder/web/sonnet_evaluator.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's sonnet_evaluator.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/sonnet_evaluator.py /c/Users/senki/repos/job-cannon/job_finder/web/sonnet_evaluator.py
```

- [ ] **Step 2: Commit**

```bash
git add job_finder/web/sonnet_evaluator.py
git commit -m "refactor: sonnet_evaluator returns ScoringResult, uses scoring_types"
```

---

### Task 13: Replace pipeline_runner.py

**Files:**
- Replace: `job_finder/web/pipeline_runner.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's pipeline_runner.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/pipeline_runner.py /c/Users/senki/repos/job-cannon/job_finder/web/pipeline_runner.py
```

- [ ] **Step 2: Verify no JobScorer import**

```bash
grep -n "JobScorer\|from job_finder.scoring" /c/Users/senki/repos/job-cannon/job_finder/web/pipeline_runner.py
```

Expected: No output (zero matches).

- [ ] **Step 3: Verify scoring_orchestrator is used**

```bash
grep -n "scoring_orchestrator" /c/Users/senki/repos/job-cannon/job_finder/web/pipeline_runner.py
```

Expected: Import line and usage of `score_and_persist_haiku`, `score_and_persist_sonnet`, `load_scoring_profile`.

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/pipeline_runner.py
git commit -m "refactor: pipeline_runner uses scoring_orchestrator, persists company enrichment"
```

---

### Task 14: Replace scheduler.py

**Files:**
- Replace: `job_finder/web/scheduler.py` (copy from FINDER)

- [ ] **Step 1: Copy FINDER's scheduler.py**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/scheduler.py /c/Users/senki/repos/job-cannon/job_finder/web/scheduler.py
```

- [ ] **Step 2: Verify factory functions exist**

```bash
grep -n "_make_simple_job\|_make_tracked_job" /c/Users/senki/repos/job-cannon/job_finder/web/scheduler.py
```

Expected: Both factory functions defined.

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/scheduler.py
git commit -m "refactor: DRY scheduler with job closure factories"
```

---

## Chunk 4: Wave 4 — Blueprints + Multi-Select Filter

### Task 15: Replace blueprint files (jobs, dashboard, profile, settings)

**Files:**
- Replace: `job_finder/web/blueprints/jobs.py` (copy from FINDER)
- Replace: `job_finder/web/blueprints/dashboard.py` (copy from FINDER)
- Replace: `job_finder/web/blueprints/profile.py` (copy from FINDER)
- Replace: `job_finder/web/blueprints/settings.py` (copy from FINDER)

- [ ] **Step 1: Copy all four blueprint files**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/web/blueprints/jobs.py /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/jobs.py
cp /c/Users/senki/repos/job-finder/job_finder/web/blueprints/dashboard.py /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/dashboard.py
cp /c/Users/senki/repos/job-finder/job_finder/web/blueprints/profile.py /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/profile.py
cp /c/Users/senki/repos/job-finder/job_finder/web/blueprints/settings.py /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/settings.py
```

- [ ] **Step 2: Verify HX-Request guards in jobs.py**

```bash
grep -n "HX-Request\|hx-request" /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/jobs.py
```

Expected: Multiple lines showing HX-Request header checks on fragment routes.

- [ ] **Step 3: Verify _safe_float/_safe_int in jobs.py**

```bash
grep -n "_safe_float\|_safe_int" /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/jobs.py
```

Expected: Function definitions and usages in `_get_filter_kwargs()`.

- [ ] **Step 4: Verify scoring_orchestrator usage in dashboard.py**

```bash
grep -n "scoring_orchestrator" /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/dashboard.py
```

Expected: Import and usages of `score_and_persist_haiku`, `score_and_persist_sonnet`.

- [ ] **Step 5: Verify settings.py guidelines_path fix**

```bash
grep -n "parent.parent.parent.parent" /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/settings.py
```

Expected: Two occurrences (in `index()` and `apply_guidelines_merge()`).

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/blueprints/jobs.py job_finder/web/blueprints/dashboard.py job_finder/web/blueprints/profile.py job_finder/web/blueprints/settings.py
git commit -m "refactor: blueprints use scoring_orchestrator, add HX-Request guards, safe param coercion, batch timeout"
```

---

### Task 16: Update jobs.py for multi-select status filter

The FINDER version of `_get_filter_kwargs()` uses single `args.get("status")`. We need to change it to support multiple statuses via `args.getlist("status")`.

**Files:**
- Modify: `job_finder/web/blueprints/jobs.py` (~line 64-65)

- [ ] **Step 1: Update _get_filter_kwargs() for multi-select**

In `job_finder/web/blueprints/jobs.py`, find the `_get_filter_kwargs()` function and change the status line:

```python
# OLD
        "status": args.get("status") or None,
# NEW
        "status": [s for s in args.getlist("status") if s] or None,
```

This returns a list of selected statuses, or `None` if none selected (which means "all").

- [ ] **Step 2: Verify the change**

```bash
grep -A2 '"status"' /c/Users/senki/repos/job-cannon/job_finder/web/blueprints/jobs.py | head -5
```

Expected: Shows `args.getlist("status")`.

- [ ] **Step 3: Update db.py get_filtered_jobs() to accept list of statuses**

In `job_finder/db.py`, find the `get_filtered_jobs()` function signature and update the `status` parameter type:

```python
# OLD signature
def get_filtered_jobs(conn, *, status=None, ...):
# NEW signature (update type hint)
def get_filtered_jobs(conn, *, status: str | list[str] | None = None, ...):
```

Then locate where it handles the `status` filter parameter and change it to support both a single string and a list:

```python
# OLD (single status exact match)
    if status:
        conditions.append("pipeline_status = ?")
        params.append(status)
# NEW (support list of statuses)
    if status:
        if isinstance(status, list):
            placeholders = ",".join("?" * len(status))
            conditions.append(f"pipeline_status IN ({placeholders})")
            params.extend(status)
        else:
            conditions.append("pipeline_status = ?")
            params.append(status)
```

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/blueprints/jobs.py job_finder/db.py
git commit -m "feat: support multi-select status filtering in jobs blueprint and db query"
```

---

### Task 17: Implement multi-select status filter UI

**Files:**
- Modify: `job_finder/web/templates/jobs/index.html` (lines 14-31)

- [ ] **Step 1: Replace the status select dropdown with checkbox pills**

In `job_finder/web/templates/jobs/index.html`, replace lines 14-31 (the form opening through the status select closing):

```html
<!-- OLD (lines 14-31) -->
  <form id="filter-form"
        hx-get="/jobs/table"
        hx-target="#job-table-body"
        hx-trigger="change from:select, change from:input"
        hx-swap="innerHTML"
        class="flex flex-wrap gap-2 mb-4 p-3 bg-slate-800/50 rounded-lg border border-slate-700">

    <!-- Status filter -->
    <select name="status"
            class="appearance-auto bg-slate-700 text-slate-200 text-xs rounded px-2 py-1.5 border border-slate-600 focus:outline-none focus:border-indigo-500">
      <option value="">All Statuses</option>
      {% for s in pipeline_statuses %}
        <option value="{{ s }}" {% if filters.get('status') == s %}selected{% endif %}>
          {{ s | replace('_', ' ') | title }}
        </option>
      {% endfor %}
    </select>
```

Replace with:

```html
<!-- NEW -->
  <form id="filter-form"
        hx-get="/jobs/table"
        hx-target="#job-table-body"
        hx-trigger="change from:input"
        hx-swap="innerHTML"
        class="flex flex-wrap gap-2 mb-4 p-3 bg-slate-800/50 rounded-lg border border-slate-700">

    <!-- Status filter (multi-select pills) -->
    <div class="flex flex-wrap items-center gap-1.5">
      <button type="button" onclick="toggleAllStatuses(this)"
              class="text-xs px-2 py-1 rounded border border-slate-600 bg-slate-700 text-slate-300 hover:bg-slate-600 cursor-pointer">
        All
      </button>
      {% set active_statuses = filters.get('status') or [] %}
      {% for s in pipeline_statuses %}
        <label class="cursor-pointer">
          <input type="checkbox" name="status" value="{{ s }}"
                 {% if not active_statuses or s in active_statuses %}checked{% endif %}
                 class="hidden peer">
          <span class="text-xs px-2 py-1 rounded border transition-colors
                       peer-checked:bg-indigo-600 peer-checked:border-indigo-500 peer-checked:text-white
                       bg-slate-800 border-slate-600 text-slate-400 hover:border-slate-500">
            {{ s | replace('_', ' ') | title }}
          </span>
        </label>
      {% endfor %}
    </div>
```

- [ ] **Step 2: Update the hide_stale checkbox hx-include to work with new status inputs**

In the same file, find the `hx-include` attribute on the hide_stale checkbox (around line 99) and ensure it references the checkbox `name='status'` inputs:

```html
<!-- OLD -->
             hx-include="[name='status'],[name='location'],[name='min_score'],[name='max_score'],[name='salary_min'],[name='source'],[name='date_from'],[name='date_to'],[name='sort_by'],[name='sort_dir'],[name='hide_stale']"
<!-- NEW (same — already correct, [name='status'] matches all checkboxes with that name) -->
             hx-include="[name='status'],[name='location'],[name='min_score'],[name='max_score'],[name='salary_min'],[name='source'],[name='date_from'],[name='date_to'],[name='sort_by'],[name='sort_dir'],[name='hide_stale']"
```

No change needed — `[name='status']` already selects all elements with `name="status"`, including multiple checkboxes.

- [ ] **Step 3: Add the toggleAllStatuses() JS function**

In the `{% block extra_scripts %}` section, add before the existing `archiveRow` function:

```javascript
/**
 * Toggle all status filter checkboxes on/off and trigger a table refresh.
 */
function toggleAllStatuses(btn) {
  var checkboxes = document.querySelectorAll('#filter-form input[name="status"]');
  var allChecked = Array.from(checkboxes).every(function(cb) { return cb.checked; });
  checkboxes.forEach(function(cb) { cb.checked = !allChecked; });
  // Trigger change event to fire HTMX reload
  if (checkboxes.length > 0) {
    checkboxes[0].dispatchEvent(new Event('change', { bubbles: true }));
  }
}
```

- [ ] **Step 4: Verify template syntax**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('job_finder/web/templates'))
t = env.get_template('jobs/index.html')
print('Template parsed OK')
"
```

Expected: `Template parsed OK`

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/templates/jobs/index.html
git commit -m "feat: replace status dropdown with multi-select checkbox pills"
```

---

### Task 18: Write test for multi-select status filter

**Files:**
- Modify: `tests/test_scoring.py` (add test class near existing `TestFilteredJobsSorting`)

The existing `TestFilteredJobsSorting` and `TestHideStaleFilter` classes in `test_scoring.py` already test `get_filtered_jobs()` using the `migrated_db` fixture. Add the new test class alongside them.

- [ ] **Step 1: Add multi-select filter tests to test_scoring.py**

In `tests/test_scoring.py`, after the `TestHideStaleFilter` class, add:

```python
class TestMultiSelectStatusFilter:
    """Verify get_filtered_jobs supports list of statuses (IN clause)."""

    def test_multi_status_returns_matching_jobs(self, migrated_db):
        """Passing a list of statuses uses IN clause."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j1", "Job A", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", "applied"),
        )
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j2", "Job B", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 4.0, "{}", "unreviewed", "rejected"),
        )
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j3", "Job C", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 3.0, "{}", "unreviewed", "archived"),
        )
        conn.commit()

        # Filter for two statuses
        jobs = get_filtered_jobs(conn, status=["applied", "rejected"])
        keys = {j["dedup_key"] for j in jobs}
        assert keys == {"j1", "j2"}, f"Expected j1+j2, got {keys}"

    def test_single_status_string_still_works(self, migrated_db):
        """Passing a single string status still works (backward compat)."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j1", "Job A", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", "applied"),
        )
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j2", "Job B", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 4.0, "{}", "unreviewed", "rejected"),
        )
        conn.commit()

        # Filter with single string (not a list)
        jobs = get_filtered_jobs(conn, status="applied")
        keys = {j["dedup_key"] for j in jobs}
        assert keys == {"j1"}

    def test_none_status_returns_all(self, migrated_db):
        """Passing status=None returns all jobs (no filter)."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j1", "Job A", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", "applied"),
        )
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j2", "Job B", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 4.0, "{}", "unreviewed", "rejected"),
        )
        conn.commit()

        jobs = get_filtered_jobs(conn, status=None)
        assert len(jobs) >= 2
```

- [ ] **Step 2: Run the new tests**

```bash
cd /c/Users/senki/repos/job-cannon && uv run pytest tests/test_scoring.py::TestMultiSelectStatusFilter -v
```

Expected: All 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scoring.py
git commit -m "test: add multi-select status filter tests for get_filtered_jobs"
```

---

## Chunk 5: Wave 5 — Safety, Tests, Cleanup

### Task 19: Port remaining safety changes

**Files:**
- Replace: `job_finder/sources/gmail_source.py` (copy from FINDER)
- Replace: `job_finder/web/db_migrate.py` (copy from FINDER)
- Replace: `job_finder/models.py` (copy from FINDER)

- [ ] **Step 1: Copy safety-related files**

```bash
cp /c/Users/senki/repos/job-finder/job_finder/sources/gmail_source.py /c/Users/senki/repos/job-cannon/job_finder/sources/gmail_source.py
cp /c/Users/senki/repos/job-finder/job_finder/web/db_migrate.py /c/Users/senki/repos/job-cannon/job_finder/web/db_migrate.py
cp /c/Users/senki/repos/job-finder/job_finder/models.py /c/Users/senki/repos/job-cannon/job_finder/models.py
```

- [ ] **Step 2: Verify Gmail pagination cap**

```bash
grep -n "max_messages\|500" /c/Users/senki/repos/job-cannon/job_finder/sources/gmail_source.py | head -5
```

Expected: Shows `max_messages=500` parameter.

- [ ] **Step 3: Verify to_dict() removed from models.py**

```bash
grep -n "to_dict" /c/Users/senki/repos/job-cannon/job_finder/models.py
```

Expected: No output (zero matches).

- [ ] **Step 4: Verify new migration exists**

```bash
grep -n "company_size\|industry" /c/Users/senki/repos/job-cannon/job_finder/web/db_migrate.py
```

Expected: Shows ALTER TABLE adding `company_size` and `industry` columns.

- [ ] **Step 5: Commit**

```bash
git add job_finder/sources/gmail_source.py job_finder/web/db_migrate.py job_finder/models.py
git commit -m "fix: Gmail 500-msg pagination cap, new migration for company enrichment, remove dead to_dict"
```

---

### Task 20: Fix failing tests

This task addresses test failures caused by the Wave 2-4 changes. The exact fixes depend on which tests failed in Task 10.

**Common patterns to fix:**

- [ ] **Step 1: Find all test files (including conftest.py) that import JobDB**

```bash
grep -rn "JobDB\|from job_finder.db import JobDB" /c/Users/senki/repos/job-cannon/tests/ --include="*.py"
```

Check `conftest.py` explicitly — it's the backbone of all test fixtures:
```bash
grep -n "JobDB" /c/Users/senki/repos/job-cannon/tests/conftest.py
```

For each match: replace `JobDB` usage with module-level function calls.

- [ ] **Step 2: Find all test files referencing old scorer return types**

```bash
grep -rn "\.get('score')\|\.get('fit_analysis')\|result\[" /c/Users/senki/repos/job-cannon/tests/ --include="*.py" | grep -i "haiku\|sonnet\|scorer\|eval"
```

If tests expect `dict` returns from scorers, update to handle `ScoringResult` (which has `.data` and `.status` attributes).

- [ ] **Step 3: Find tests using old profile parameter name**

```bash
grep -rn "profile=" /c/Users/senki/repos/job-cannon/tests/ --include="*.py" | grep -i "haiku\|scorer"
```

Change `profile=` to `experience_profile=` where calling haiku_scorer.

- [ ] **Step 4: Check cannon-only test files for db.py compatibility**

```bash
grep -n "from job_finder" /c/Users/senki/repos/job-cannon/tests/test_docx_formatter.py /c/Users/senki/repos/job-cannon/tests/test_drive_status.py /c/Users/senki/repos/job-cannon/tests/test_drive_uploader.py
```

If any import `JobDB` or old function signatures, update them. These files must be preserved (not replaced).

- [ ] **Step 5: Run full test suite and iterate**

```bash
cd /c/Users/senki/repos/job-cannon && uv run pytest tests/ -x -q
```

Repeat: fix the first failure, re-run. Continue until all tests pass.

- [ ] **Step 6: Copy any additional test files from FINDER that don't exist in CANNON**

Check if FINDER has test files that CANNON lacks (beyond the 3 cannon-only files):

```bash
diff <(ls /c/Users/senki/repos/job-finder/tests/test_*.py | xargs -I{} basename {}) <(ls /c/Users/senki/repos/job-cannon/tests/test_*.py | xargs -I{} basename {})
```

For test files only in FINDER: copy them to CANNON. For test files only in CANNON (the 3 known ones): preserve them.

- [ ] **Step 7: Commit test fixes**

```bash
git add tests/
git commit -m "fix: update tests for new db.py API, ScoringResult return types, and experience_profile param"
```

---

### Task 21: Cleanup — delete dead code, move todos

**Files:**
- Delete: `job_finder/main.py`
- Delete: `job_finder/scoring/` (entire package)
- Move: `.planning/todos/pending/*.md` → `.planning/todos/done/`

- [ ] **Step 1: Verify no remaining imports of scoring package**

```bash
grep -rn "from job_finder.scoring\|import job_finder.scoring" /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
```

Expected: No output (zero matches — pipeline_runner was already updated in Task 13).

- [ ] **Step 2: Delete dead code**

```bash
rm /c/Users/senki/repos/job-cannon/job_finder/main.py
rm -rf /c/Users/senki/repos/job-cannon/job_finder/scoring/
```

- [ ] **Step 3: Move completed todos**

```bash
mkdir -p /c/Users/senki/repos/job-cannon/.planning/todos/done
mv /c/Users/senki/repos/job-cannon/.planning/todos/pending/2026-03-21-fix-job-board-not-refreshing-when-date-filter-is-cleared.md /c/Users/senki/repos/job-cannon/.planning/todos/done/
mv /c/Users/senki/repos/job-cannon/.planning/todos/pending/2026-03-21-replace-status-filter-dropdown-with-multi-select-checkboxes.md /c/Users/senki/repos/job-cannon/.planning/todos/done/
```

- [ ] **Step 4: Commit**

```bash
git rm job_finder/main.py
git rm -r job_finder/scoring/
git add .planning/todos/
git commit -m "chore: delete dead CLI entry point and scoring package, close completed todos"
```

---

### Task 22: Final verification

- [ ] **Step 1: Run full test suite**

```bash
cd /c/Users/senki/repos/job-cannon && uv run pytest tests/ -q
```

Expected: All tests pass.

- [ ] **Step 2: Verify no stale imports**

```bash
grep -rn "from job_finder.utils " /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
grep -rn "from job_finder.scoring" /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
grep -rn "JobDB" /c/Users/senki/repos/job-cannon/job_finder/ --include="*.py"
```

Expected: Zero matches for all three.

- [ ] **Step 3: Verify app factory works**

```bash
cd /c/Users/senki/repos/job-cannon && uv run python -c "from job_finder.web import create_app; app = create_app(config={'TESTING': True, 'db': {'path': ':memory:'}}); print('App factory OK')"
```

Expected: `App factory OK` (no import errors).

- [ ] **Step 4: Summary commit if any loose changes remain**

```bash
git status
# If any uncommitted changes, stage and commit:
# git add -A && git commit -m "chore: final port cleanup"
```

---

## Post-Implementation Checklist

After all tasks complete:

1. **Manual smoke test** (human-needed):
   - Start app with `uv run python run.py`
   - Navigate to job board — verify it loads
   - Verify status pills render (not a dropdown)
   - Click individual status pills — verify filtering works
   - Click "All" toggle — verify select/deselect all
   - Expand a job row — verify accordion works
   - Check dashboard — verify batch scoring UI present

2. **Update CLAUDE.md** if needed:
   - Test count may have changed
   - Any new architectural decisions from the port
   - Module count may have changed

3. **job-finder repo retired** — no further development there
