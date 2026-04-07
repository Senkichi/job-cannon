# Job Cannon — Outstanding TODO Implementation Plan

> 16 chunks for the langgraph-agents plan-build-review workflow.
> Maximally unconsolidated — each chunk is a single conceptual unit.

```
Dependency graph:

  [1: Provider Cleanup (prod)]
          │
          ▼
  [2: Provider Cleanup (tests)]

  [3: Business Day Utility]  ──────────────────────┐
                                                    │
  [4: Dismissed Status + Auto-Dismiss] ────────────┐│
                                                   ││
  [5: Backend Filter Overhaul] ◄───────────────────┘│
          │                       ◄─────────────────┘
          ▼
  [6: Filter Bar Template Rebuild]
          │
          ▼
  [7: Filter Bar JavaScript]

  [8: Golden Asterisk Repurpose]  (needs 3 for freshness_cutoff)

  [9: Job Visibility + Filter Tests]  (needs 4, 5, 6)

  [10-15: God Object Splits]  (all independent, any order, any time)

  [16: E2E HTMX Validation]  (run last — needs 5, 6, 7, 8, 9)
```

---

## Chunk 1: Provider Cleanup (Production Code)

**Scope**: Delete dead provider adapters and clean all production code references.
**Files touched** (7): `job_finder/web/providers/cerebras_provider.py` (delete), `job_finder/web/providers/groq_provider.py` (delete), `job_finder/web/providers/openrouter_provider.py` (delete), `job_finder/web/model_provider.py`, `job_finder/web/score_calibration.py`, `job_finder/db.py`, `config.example.yaml`
**Verification**: `uv run pytest tests/ -x` (some provider tests will fail — expected, fixed in Chunk 2)

### Context

Cerebras/Groq/OpenRouter adapters were never validated in production. Ollama (r=0.852 vs Opus) handles local scoring reliably. Simplify cascade to Ollama → Anthropic.

### Implementation Plan

#### 1a. Delete dead provider files

Delete these 3 files entirely:
- `job_finder/web/providers/cerebras_provider.py`
- `job_finder/web/providers/groq_provider.py`
- `job_finder/web/providers/openrouter_provider.py`

#### 1b. Clean up `job_finder/web/model_provider.py`

1. **Line 174** — `_FREE_PROVIDERS` frozenset: Remove `"openrouter"`, `"groq"`, `"cerebras"`. Keep `"gemini"`, `"ollama"`, `"ollm"`, `"sambanova"`.

2. **Lines 245-273** — Remove the three import statements and their `if provider_name == "..."` dispatch branches:
   ```python
   # DELETE these imports:
   from job_finder.web.providers.cerebras_provider import CerebrasProvider  # line 245
   from job_finder.web.providers.groq_provider import GroqProvider          # line 247
   from job_finder.web.providers.openrouter_provider import OpenRouterProvider  # line 248
   
   # DELETE these dispatch branches:
   if provider_name == "cerebras": ...   # line 259
   if provider_name == "groq": ...       # line 265
   if provider_name == "openrouter": ... # line 273
   ```

3. **Line 44 area** — Update the docstring example: Change `"cerebras", "groq"` to `"ollama", "gemini"`.

#### 1c. Clean up `job_finder/web/score_calibration.py`

Line 67 — change docstring example from `"cerebras"` to `"ollama"`.

#### 1d. Clean up `job_finder/db.py`

Line 275 — change docstring example from `"cerebras"` to `"ollama"`.

#### 1e. Update `config.example.yaml`

Remove the commented-out `cerebras`, `groq`, `openrouter` entries from the cascade example block (lines 179-191). Replace with:
```yaml
#   fallback_chain:
#     - provider: ollama
#       model: qwen2.5:14b
#     - provider: anthropic
#       model: claude-sonnet-4-6
#   daily_limits: {}
```

#### 1f. Update `.planning/PROVIDER_REFERENCE.md`

Add a note: Cerebras/Groq/OpenRouter removed (never validated), Ollama sufficient as free primary. If file doesn't exist, skip.

**Done when**: 3 provider files deleted, no stale imports in `job_finder/`. Docstrings updated. Config example cleaned.

---

## Chunk 2: Provider Cleanup (Tests)

**Scope**: Rewrite tests referencing deleted providers.
**Files touched** (2): `tests/test_db.py`, `tests/test_model_provider.py`
**Depends on**: Chunk 1
**Verification**: `uv run pytest tests/test_db.py tests/test_model_provider.py -x -v`

### Context

`test_model_provider.py` has the heaviest coupling — the cascade test suite (lines 561-839) is built around a `cerebras → groq → anthropic` config fixture. `test_db.py` uses `provider="cerebras"` and `provider="groq"` as fixture data (lines 300, 306, 327, 331).

### Implementation Plan

#### 2a. Fix `tests/test_db.py`

Lines 300, 306, 327, 331 — replace `provider="cerebras"` and `provider="groq"` fixture data with `provider="ollama"` and `provider="gemini"`.

#### 2b. Fix `tests/test_model_provider.py`

1. **Line 325 area** — Remove `"openrouter"` from parametrize values for adapter tests.
2. **Lines 475-553** — `_check_daily_limit`/`_increment_usage` tests: replace `"cerebras"` and `"groq"` fixture config with `"ollama"` equivalents.
3. **Lines 561-839** — Cascade test suite: rewrite the config fixture from `cerebras → groq → anthropic` to `ollama → gemini → anthropic`. Update all assertions to match new provider names.

Run: `grep -rn "cerebras\|groq\|openrouter" tests/` after — should return zero matches.

**Done when**: All provider tests pass with no references to cerebras/groq/openrouter. Full suite passes.

---

## Chunk 3: Business Day Utility

**Scope**: Create business day calculation utility for freshness filters.
**Files touched** (3): `job_finder/utils/__init__.py` (new), `job_finder/utils/business_days.py` (new), `tests/test_business_days.py` (new)
**Depends on**: Nothing
**Verification**: `uv run pytest tests/test_business_days.py -v`

### Implementation Plan

Create `job_finder/utils/__init__.py` (empty) and `job_finder/utils/business_days.py`:

```python
"""Business day calculation for freshness filters."""

from datetime import date, timedelta


def business_days_ago(n: int, reference: date | None = None) -> date:
    """Return the date N business days before reference (default: today).
    
    Skips Saturdays (5) and Sundays (6).
    """
    ref = reference or date.today()
    days_back = 0
    while n > 0:
        days_back += 1
        check = ref - timedelta(days=days_back)
        if check.weekday() < 5:  # Mon-Fri
            n -= 1
    return ref - timedelta(days=days_back)
```

Write `tests/test_business_days.py`:
```python
from datetime import date
from job_finder.utils.business_days import business_days_ago

def test_business_days_ago_skips_weekends():
    """Monday - 1 biz day = Friday."""
    assert business_days_ago(1, reference=date(2026, 4, 6)) == date(2026, 4, 3)

def test_business_days_ago_3():
    """Monday - 3 biz days = Wednesday of prior week."""
    assert business_days_ago(3, reference=date(2026, 4, 6)) == date(2026, 4, 1)

def test_business_days_ago_zero_returns_reference():
    assert business_days_ago(0, reference=date(2026, 4, 6)) == date(2026, 4, 6)
```

**Done when**: `business_days_ago` works correctly, all 3 tests pass.

---

## Chunk 4: Dismissed Status + Auto-Dismiss Logic

**Scope**: Add "dismissed" pipeline status and auto-dismiss excluded jobs during scoring.
**Files touched** (3-4): `job_finder/constants.py`, `job_finder/web/haiku_scorer.py`, possibly `job_finder/web/pipeline_runner.py`
**Depends on**: Nothing
**Verification**: `uv run pytest tests/ -x`

### Context

Jobs the user has dismissed or that were auto-excluded by Haiku scoring should be separable from the active job list. Currently there is no "dismissed" status, and excluded jobs remain in "discovered" state.

### Implementation Plan

#### 4a. Add "dismissed" to `PIPELINE_STATUSES`

Edit `job_finder/constants.py` — append `"dismissed"` to `PIPELINE_STATUSES` (line 13, after "withdrawn"):
```python
PIPELINE_STATUSES = (
    "discovered", "reviewing", "applied", "phone_screen", "technical",
    "onsite", "offer", "accepted", "archived", "rejected", "withdrawn",
    "dismissed",  # NEW: user reviewed and not interested
)
```
No DB migration needed — `pipeline_status` is a TEXT column with no CHECK constraint.

#### 4b. Auto-dismiss excluded jobs in `haiku_scorer.py`

Read `job_finder/web/haiku_scorer.py` — find where `exclusion_reason` is written to DB. After that write, auto-set status to dismissed:

```python
if exclusion_reason:
    conn.execute(
        "UPDATE jobs SET pipeline_status = 'dismissed' WHERE dedup_key = ? AND pipeline_status = 'discovered'",
        (dedup_key,),
    )
```

Only auto-dismiss `discovered` jobs — don't override `reviewing`/`applied`/etc.

Also check `pipeline_runner.py` for pre-filter exclusion logic and apply same pattern.

**Done when**: "dismissed" is a valid pipeline status. Excluded jobs auto-transition to dismissed. Existing tests pass (no test relies on excluded jobs staying in "discovered").

---

## Chunk 5: Backend Filter Overhaul

**Scope**: Replace old filter params with new ones in `jobs.py` and `db.py`.
**Files touched** (2): `job_finder/web/blueprints/jobs.py`, `job_finder/db.py`
**Depends on**: Chunk 3 (business_days_ago), Chunk 4 (dismissed status for HIDDEN_STATUSES)
**Verification**: `uv run pytest tests/test_views.py tests/test_db_helpers.py -x`

### Context

Remove 6 unused/low-value filter parameters (min_score, max_score, salary_min, source, date_from, date_to) and add 3 new ones (posted_within, freshness, show_hidden).

### Implementation Plan

#### 5a. Update `_get_filter_kwargs()` in `jobs.py` (lines 60-80)

Replace with:
```python
def _get_filter_kwargs() -> dict:
    args = request.args
    statuses = [s for s in args.getlist("status") if s]
    return {
        "status": statuses if len(statuses) > 1 else (statuses[0] if statuses else None),
        "location": args.get("location") or None,
        "posted_within": args.get("posted_within") or None,
        "freshness": args.get("freshness") or None,
        "sort_by": args.get("sort_by", "score"),
        "sort_dir": args.get("sort_dir", "DESC"),
        "limit": 200,
        "hide_stale": args.get("hide_stale", "on") == "on",  # Default ON
        "show_hidden": args.get("show_hidden") == "on",
    }
```

Remove `_safe_float` and `_safe_int` helper calls if they become unused.

#### 5b. Add `_get_hidden_count()` helper in `jobs.py`

```python
def _get_hidden_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status IN ('archived', 'withdrawn', 'dismissed', 'rejected')"
    ).fetchone()
    return row[0] if row else 0
```

Pass `hidden_count` and `freshness_cutoff` to template context:
```python
from job_finder.utils.business_days import business_days_ago

# In the route handler:
freshness_cutoff = business_days_ago(3).isoformat()
hidden_count = _get_hidden_count(conn)
```

Remove `get_distinct_sources` from imports (no longer needed).

#### 5c. Update `get_filtered_jobs()` in `db.py` (lines 526-623)

1. **Remove parameters**: `min_score`, `max_score`, `salary_min`, `source`, `date_from`, `date_to`
2. **Add parameters**: `posted_within: str | None = None`, `freshness: str | None = None`, `show_hidden: bool = False`
3. **Remove SQL clauses** for deleted filters (lines 597-614)
4. **Replace deprioritization with exclusion** (lines 572-581):

```python
HIDDEN_STATUSES = ("archived", "withdrawn", "dismissed", "rejected")

if not status and not show_hidden:
    hidden_placeholders = ", ".join("?" * len(HIDDEN_STATUSES))
    conditions.append(f"pipeline_status NOT IN ({hidden_placeholders})")
    params.extend(HIDDEN_STATUSES)
```

5. **Add new filter clauses**:

```python
if posted_within:
    within_map = {
        "today": "date('now')",
        "3d": "date('now', '-3 days')",
        "1w": "date('now', '-7 days')",
        "1m": "date('now', '-1 month')",
    }
    if posted_within in within_map:
        conditions.append(f"first_seen >= {within_map[posted_within]}")

if freshness:
    from job_finder.utils.business_days import business_days_ago
    cutoff = None
    if freshness == "biz1":
        cutoff = business_days_ago(1).isoformat()
    elif freshness == "biz3":
        cutoff = business_days_ago(3).isoformat()
    if cutoff:
        conditions.append("first_seen >= ?")
        params.append(cutoff)
```

6. Keep `salary_max` in `allowed_sort_cols` (salary sort still useful).

#### 5d. Remove `get_distinct_sources()` from `db.py`

Delete the function at lines 644-656.

**Done when**: `_get_filter_kwargs` returns new params. `get_filtered_jobs` accepts new params. Old filter params removed. `get_distinct_sources` deleted.

---

## Chunk 6: Filter Bar Template Rebuild

**Scope**: Remove old filter HTML and build new two-row layout.
**Files touched** (1): `job_finder/web/templates/jobs/index.html`
**Depends on**: Chunk 5 (backend params must match template form fields)
**Verification**: Manual — start app and verify filter bar renders. `uv run pytest tests/ -x` for regression.

### Implementation Plan

#### 6a. Remove old filter HTML

Delete these HTML blocks from the filter form (lines 57-98):
- Source filter `<select name="source">` (lines 57-65)
- Min score `<input name="min_score">` (lines 67-73)
- Max score `<input name="max_score">` (lines 74-79)
- Salary min `<input name="salary_min">` (lines 81-86)
- Date from `<input name="date_from">` (lines 88-93)
- Date to `<input name="date_to">` (lines 94-98)

#### 6b. Restructure to two-row layout

Replace the filter form innards with:

```html
<!-- Row 1: Status pills (full width) -->
<div class="flex flex-wrap items-center gap-1 w-full">
  <button type="button" id="status-all-btn" onclick="toggleAllStatus(this)" ...>All</button>
  {% for s in pipeline_statuses %}
    <label class="flex items-center gap-1 cursor-pointer">
      <input type="checkbox" name="status" value="{{ s }}" ... class="hidden status-pill-cb">
      <span class="... status-pill-label">{{ s | replace('_', ' ') | title }}</span>
    </label>
  {% endfor %}
</div>

<!-- Row 2: Remaining filters -->
<div class="flex flex-wrap items-center gap-2 w-full">
  <!-- Location -->
  <select name="location" id="filter-location" class="appearance-auto bg-slate-700 text-slate-200 text-xs rounded px-2 py-1.5 border border-slate-600 focus:outline-none focus:border-indigo-500">
    <option value="">All Locations</option>
    {% for loc in locations %}<option value="{{ loc }}" {% if filters.get('location') == loc %}selected{% endif %}>{{ loc }}</option>{% endfor %}
  </select>

  <!-- Posted within dropdown (replaces date_from/date_to) -->
  <select name="posted_within" id="filter-posted-within" class="appearance-auto bg-slate-700 text-slate-200 text-xs rounded px-2 py-1.5 border border-slate-600 focus:outline-none focus:border-indigo-500">
    <option value="">Any time</option>
    <option value="today" {% if filters.get('posted_within') == 'today' %}selected{% endif %}>Today</option>
    <option value="3d" {% if filters.get('posted_within') == '3d' %}selected{% endif %}>Last 3 days</option>
    <option value="1w" {% if filters.get('posted_within') == '1w' %}selected{% endif %}>Last week</option>
    <option value="1m" {% if filters.get('posted_within') == '1m' %}selected{% endif %}>Last month</option>
  </select>

  <!-- Freshness toggle buttons -->
  <button type="button" id="filter-biz-1" class="px-2 py-1 text-xs rounded border border-slate-600 bg-slate-700 text-slate-200 hover:bg-slate-600 transition-colors cursor-pointer freshness-toggle" onclick="toggleFreshness(this, 'biz1')">Last Biz Day</button>
  <button type="button" id="filter-biz-3" class="px-2 py-1 text-xs rounded border border-slate-600 bg-slate-700 text-slate-200 hover:bg-slate-600 transition-colors cursor-pointer freshness-toggle" onclick="toggleFreshness(this, 'biz3')">Last 3 Biz Days</button>
  <input type="hidden" name="freshness" id="filter-freshness" value="">

  <!-- Sort -->
  <select name="sort_by" id="filter-sort-by" ...>
    <option value="score" ...>Score</option>
    <option value="first_seen" ...>Date</option>
    <option value="salary_max" ...>Salary</option>
    <option value="company" ...>Company</option>
  </select>
  <select name="sort_dir" id="filter-sort-dir" ...>
    <option value="DESC" ...>Desc</option>
    <option value="ASC" ...>Asc</option>
  </select>

  <!-- Hide stale (default ON) -->
  <label class="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
    <input type="checkbox" name="hide_stale" value="on"
           {% if filters.get('hide_stale', 'on') == 'on' %}checked{% endif %}
           hx-get="/jobs/table" hx-trigger="change" hx-target="#job-table-body"
           hx-include="[name='status'],[name='location'],[name='posted_within'],[name='freshness'],[name='sort_by'],[name='sort_dir'],[name='hide_stale'],[name='show_hidden']"
           class="w-3.5 h-3.5 accent-violet-500">
    Hide stale
    {% if stale_count > 0 %}<span class="px-1 py-0.5 rounded text-xs bg-amber-900/50 text-amber-400 border border-amber-700/50 font-mono">{{ stale_count }}</span>{% endif %}
  </label>

  <!-- Show hidden (dismissed/rejected/archived/withdrawn) -->
  <label class="flex items-center gap-2 text-xs text-slate-400 cursor-pointer">
    <input type="checkbox" name="show_hidden" value="on"
           {% if filters.get('show_hidden') == 'on' %}checked{% endif %}
           class="w-3.5 h-3.5 accent-violet-500">
    Show hidden
    {% if hidden_count > 0 %}<span class="px-1 py-0.5 rounded text-xs bg-slate-700 text-slate-400 border border-slate-600 font-mono">{{ hidden_count }}</span>{% endif %}
  </label>
</div>
```

Update the `hx-trigger` on the form to include new elements:
```html
<form id="filter-form"
      hx-get="/jobs/table" hx-target="#job-table-body"
      hx-trigger="change from:select, change from:input"
      hx-swap="innerHTML"
      class="flex flex-wrap gap-2 mb-4 p-3 bg-slate-800/50 rounded-lg border border-slate-700">
```

**Done when**: Filter bar renders two clean rows. Old filters gone. New controls visible.

---

## Chunk 7: Filter Bar JavaScript

**Scope**: Add freshness toggle logic and localStorage filter persistence.
**Files touched** (1): `job_finder/web/templates/jobs/index.html` (script block)
**Depends on**: Chunk 6 (HTML elements must exist for JS to bind)
**Verification**: Manual — toggle freshness buttons, change filters, reload page, verify persistence.

### Implementation Plan

Add to the `{% block scripts %}` section of `index.html`:

```javascript
/* --- Freshness toggle buttons --- */
function toggleFreshness(btn, value) {
  const hidden = document.getElementById('filter-freshness');
  const allToggles = document.querySelectorAll('.freshness-toggle');
  
  if (hidden.value === value) {
    hidden.value = '';
    allToggles.forEach(b => {
      b.classList.remove('bg-indigo-600', 'border-indigo-500', 'text-white');
      b.classList.add('bg-slate-700', 'border-slate-600', 'text-slate-200');
    });
  } else {
    hidden.value = value;
    allToggles.forEach(b => {
      b.classList.remove('bg-indigo-600', 'border-indigo-500', 'text-white');
      b.classList.add('bg-slate-700', 'border-slate-600', 'text-slate-200');
    });
    btn.classList.remove('bg-slate-700', 'border-slate-600', 'text-slate-200');
    btn.classList.add('bg-indigo-600', 'border-indigo-500', 'text-white');
  }
  document.getElementById('filter-posted-within').value = '';
  htmx.trigger(document.getElementById('filter-form'), 'change');
}

// Clear freshness toggles when "posted within" dropdown changes
document.getElementById('filter-posted-within')?.addEventListener('change', function() {
  document.getElementById('filter-freshness').value = '';
  document.querySelectorAll('.freshness-toggle').forEach(b => {
    b.classList.remove('bg-indigo-600', 'border-indigo-500', 'text-white');
    b.classList.add('bg-slate-700', 'border-slate-600', 'text-slate-200');
  });
});

/* --- Filter persistence in localStorage --- */
const FILTER_STORAGE_KEY = 'jc-job-filters';
const PERSISTED_FILTERS = ['hide_stale', 'show_hidden', 'sort_by', 'sort_dir', 'posted_within'];

function saveFilters() {
  const form = document.getElementById('filter-form');
  const state = {};
  PERSISTED_FILTERS.forEach(name => {
    const el = form.querySelector('[name="' + name + '"]');
    if (!el) return;
    state[name] = el.type === 'checkbox' ? el.checked : el.value;
  });
  localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(state));
}

function restoreFilters() {
  const raw = localStorage.getItem(FILTER_STORAGE_KEY);
  if (!raw) return;
  try {
    const state = JSON.parse(raw);
    const form = document.getElementById('filter-form');
    PERSISTED_FILTERS.forEach(name => {
      const el = form.querySelector('[name="' + name + '"]');
      if (!el || !(name in state)) return;
      if (el.type === 'checkbox') { el.checked = state[name]; }
      else { el.value = state[name]; }
    });
    htmx.trigger(form, 'change');
  } catch (e) { /* ignore corrupt storage */ }
}

document.getElementById('filter-form').addEventListener('change', saveFilters);
document.addEventListener('DOMContentLoaded', restoreFilters);
```

**Done when**: Freshness toggles activate/deactivate with visual feedback. Freshness and posted_within are mutually exclusive. Filters persist across page reloads via localStorage.

---

## Chunk 8: Repurpose Golden Asterisk

**Scope**: Change score cell asterisk from "Sonnet score" indicator to "fresh job" indicator.
**Files touched** (1): `job_finder/web/templates/jobs/_score_cell.html`
**Depends on**: Chunk 3 (freshness_cutoff from business_days_ago)
**Verification**: Manual — verify asterisk appears on recently surfaced jobs, not on Sonnet-scored jobs.

### Implementation Plan

Edit `job_finder/web/templates/jobs/_score_cell.html`. Currently (line 9 and 27):
```html
{% set is_sonnet = job.sonnet_score is not none %}
...
{% if is_sonnet %}<span class="text-amber-400 text-xs ml-0.5">*</span>{% endif %}
```

Replace with:
```html
{% set is_fresh = job.first_seen and job.first_seen >= freshness_cutoff %}
...
{% if is_fresh %}<span class="text-amber-400 text-xs ml-0.5" title="Surfaced in last 3 days">*</span>{% endif %}
```

Remove the `{% set is_sonnet = ... %}` line (no longer used).

Ensure `freshness_cutoff` is passed in template context (done in Chunk 5).

**Done when**: Asterisk indicates freshness (last 3 days), not Sonnet score source.

---

## Chunk 9: Job Visibility + Filter Tests

**Scope**: Create tests for new filter behavior and update existing tests referencing removed filters.
**Files touched** (3+): `tests/test_job_visibility.py` (new), plus updates to any tests referencing `min_score`, `max_score`, `salary_min`, `source`, `date_from`, `date_to`, `get_distinct_sources`
**Depends on**: Chunks 4, 5, 6
**Verification**: `uv run pytest tests/test_job_visibility.py -v && uv run pytest tests/ -x`

### Implementation Plan

#### 9a. Search and update existing tests

```bash
grep -rn "min_score\|max_score\|salary_min\|filter.*source\|date_from\|date_to\|get_distinct_sources" tests/
```

For each match: delete tests for removed filters, remove incidental param usage.

#### 9b. Create `tests/test_job_visibility.py`

```python
def test_dismissed_status_hidden_by_default(app, client):
    """Dismissed jobs don't appear in default job list."""

def test_dismissed_visible_with_show_hidden(app, client):
    """Dismissed jobs appear when show_hidden=on."""

def test_rejected_hidden_by_default(app, client):
    """Rejected jobs don't appear in default job list."""

def test_auto_dismiss_excluded_job(app, client):
    """Jobs with exclusion_reason are auto-set to dismissed status."""

def test_auto_dismiss_does_not_override_reviewing(app, client):
    """Auto-dismiss only affects discovered jobs, not reviewing/applied."""
```

**Done when**: New visibility tests pass. No tests reference removed filter params. Full suite passes.

---

## Chunk 10: Split `ats_scanner.py`

**Scope**: Split 1073 LOC into 3 focused modules.
**Files touched**: `job_finder/web/ats_scanner.py` + 2 new files + all importers
**Depends on**: Nothing (independent)
**Verification**: `uv run pytest tests/ -x` after split. `wc -l` on all new files under 500.

### Implementation Plan

1. Read entire file, identify all public functions and their callers
2. Group functions by responsibility cluster. Expected split:
   - `ats_scanner.py` — Core ATS detection logic (pattern matching, URL analysis)
   - `ats_classifier.py` — ATS system classification and scoring
   - `ats_page_analyzer.py` — Page content analysis (HTML parsing, status extraction)
3. Create new modules, move functions
4. Update all importers: `grep -rn "from job_finder.web.ats_scanner import\|from job_finder.web import ats_scanner" job_finder/ tests/`
5. Verify: `uv run pytest tests/ -x`

**Done when**: All 3 modules under 500 LOC. All tests pass. No stale imports.

---

## Chunk 11: Split `pipeline_runner.py`

**Scope**: Split 860 LOC into 2-3 modules. Note: `scoring_runner.py` (278 LOC) already partially extracted.
**Files touched**: `job_finder/web/pipeline_runner.py` + 1-2 new files + all importers
**Depends on**: Nothing (independent)
**Verification**: `uv run pytest tests/ -x`

### Implementation Plan

1. Read `pipeline_runner.py` and existing `scoring_runner.py` to understand what's already extracted
2. Expected split:
   - `pipeline_runner.py` — Orchestration (run_ingestion, run_scoring)
   - `ingestion_runner.py` — Source fetching and dedup logic
   - `scoring_runner.py` — Already exists, may need additional functions moved into it
3. Create/update modules, move functions
4. Update all importers
5. Verify: `uv run pytest tests/ -x`

**Done when**: `pipeline_runner.py` under 500 LOC. All tests pass.

---

## Chunk 12: Split `db.py`

**Scope**: Split 773 LOC into 3 modules with re-exports for backward compatibility.
**Files touched**: `job_finder/db.py` + 2 new files + importers
**Depends on**: Nothing (independent), but schedule after Chunk 5 if possible (Chunk 5 modifies `get_filtered_jobs`)
**Verification**: `uv run pytest tests/ -x`

### Implementation Plan

Expected split:
1. `db.py` — Core CRUD: `upsert_job`, `get_job`, `get_filtered_jobs`, `update_pipeline_status`
2. `db_queries.py` — Read-only aggregates: `get_distinct_locations`, `get_dashboard_stats`, `get_pending_detections`
3. `db_pipeline.py` — Pipeline events: `get_pipeline_events`, `record_pipeline_event`, detection queries

**Caution**: `db.py` is imported everywhere. Use re-export for backwards compat:
```python
# db.py — keep all public names importable
from job_finder.db_queries import get_distinct_locations, get_dashboard_stats, ...
from job_finder.db_pipeline import get_pipeline_events, record_pipeline_event, ...
```

**Done when**: All 3 modules under 500 LOC. Re-exports work. All tests pass.

---

## Chunk 13: Split `data_enricher.py`

**Scope**: Split 762 LOC into 2 modules.
**Files touched**: `job_finder/web/data_enricher.py` + 1 new file + importers
**Depends on**: Nothing (independent)
**Verification**: `uv run pytest tests/ -x`

### Implementation Plan

1. `data_enricher.py` — Orchestration and tier selection
2. `enrichment_sources.py` — Individual source implementations (SerpAPI, DDG, DataForSEO, etc.)

**Done when**: Both modules under 500 LOC. All tests pass.

---

## Chunk 14: Split `backfill_companies.py`

**Scope**: Split 720 LOC into 2 modules.
**Files touched**: `job_finder/web/backfill_companies.py` + 1 new file + importers
**Depends on**: Nothing (independent)
**Verification**: `uv run pytest tests/ -x`

### Implementation Plan

1. `backfill_companies.py` — Batch orchestration
2. `company_resolver.py` — Individual company resolution (homepage discovery, domain guessing)

**Done when**: Both modules under 500 LOC. All tests pass.

---

## Chunk 15: Split `resume_generator.py`

**Scope**: Split 619 LOC into 2 modules.
**Files touched**: `job_finder/web/resume_generator.py` + 1 new file + importers
**Depends on**: Nothing (independent)
**Verification**: `uv run pytest tests/ -x`

### Implementation Plan

1. `resume_generator.py` — Orchestration (generate_resume entry point)
2. `resume_content.py` — Content generation helpers (section builders, formatting)

**Done when**: Both modules under 500 LOC. All tests pass.

---

## Chunk 16: E2E HTMX Validation

**Scope**: Playwright browser tests validating interactive actions work via HTMX without page refresh.
**Files touched** (1-2): `tests/e2e/test_jobs_page.py` (new), possibly `tests/e2e/conftest.py` (update seeds)
**Depends on**: Chunks 5, 6, 7, 8, 9 (validates final UI state)
**Verification**: `uv run pytest tests/e2e/ -v`

### Context

Infrastructure already exists: `tests/e2e/conftest.py` has live Flask server fixture + Playwright page fixture. `tests/e2e/test_smoke.py` has 8 basic smoke tests. This chunk adds detailed interaction tests for the jobs page.

**Run LAST** — after all filter/visibility chunks are complete.

### Implementation Plan

#### 16a. Update `tests/e2e/conftest.py` seeds if needed

Ensure sample data includes jobs with varying statuses (discovered, reviewing, applied, dismissed, rejected, archived), varying scores, varying dates, and varying locations to exercise all filter paths.

#### 16b. Create `tests/e2e/test_jobs_page.py`

```python
"""E2E: Jobs page HTMX interactions update DOM without page refresh."""

class TestStatusDropdown:
    def test_status_change_reflects_without_reload(self, page):
        """Changing status dropdown updates row in-place via HTMX."""

class TestAccordionExpandCollapse:
    def test_expand_shows_detail_inline(self, page):
        """Clicking expand shows job detail in expand slot."""
    def test_collapse_hides_detail(self, page):
        """Clicking collapse restores hidden placeholder."""

class TestFilterBar:
    def test_status_pills_filter_table(self, page):
        """Status pills filter table via HTMX."""
    def test_freshness_toggle_filters(self, page):
        """Freshness toggle buttons filter to recent jobs."""
    def test_posted_within_filters(self, page):
        """Posted within dropdown filters table."""
    def test_show_hidden_reveals_dismissed(self, page):
        """Show hidden toggle reveals dismissed/rejected jobs."""

class TestJobActions:
    def test_dismiss_removes_from_list(self, page):
        """Setting status to dismissed removes from default view."""
    def test_score_button_updates_cell(self, page):
        """Triggering scoring updates score cell in-place."""

class TestDOMIntegrity:
    def test_no_stale_data_after_status_change(self, page):
        """After status change, old status text gone from DOM."""
    def test_filter_state_survives_navigation(self, page):
        """Navigate away and back — localStorage restores filters."""
```

#### 16c. Fix broken HTMX wiring found during testing

For each E2E failure, investigate and fix at the blueprint/template level:
- Wrong `hx-target` or `hx-swap` mode
- `204` responses instead of `200` (HTMX requires 200 for outerHTML)
- Missing `HX-Request` header guard on fragment routes
- Accordion expand/collapse not replacing correct row

**Done when**: All E2E tests pass. Every interactive action updates DOM without reload.

---

## Execution Summary

| Chunk | Name | Dependencies | Parallelizable With |
|-------|------|-------------|-------------------|
| 1 | Provider Cleanup (prod) | None | 3, 4, 10-15 |
| 2 | Provider Cleanup (tests) | 1 | 3, 4, 10-15 |
| 3 | Business Day Utility | None | 1, 2, 4, 10-15 |
| 4 | Dismissed Status + Auto-Dismiss | None | 1, 2, 3, 10-15 |
| 5 | Backend Filter Overhaul | 3, 4 | 10-15 |
| 6 | Filter Bar Template Rebuild | 5 | 10-15 |
| 7 | Filter Bar JavaScript | 6 | 10-15 |
| 8 | Golden Asterisk Repurpose | 3 | 10-15 |
| 9 | Job Visibility + Filter Tests | 4, 5, 6 | 10-15 |
| 10 | Split ats_scanner.py | None | 1-9, 11-15 |
| 11 | Split pipeline_runner.py | None | 1-10, 12-15 |
| 12 | Split db.py | None (prefer after 5) | 1-11, 13-15 |
| 13 | Split data_enricher.py | None | 1-12, 14-15 |
| 14 | Split backfill_companies.py | None | 1-13, 15 |
| 15 | Split resume_generator.py | None | 1-14 |
| 16 | E2E HTMX Validation | 5, 6, 7, 8, 9 | None (last) |

## Items Removed (Already Completed)

- ~~**test_sonnet_queue fix**~~ — Fixed in commit `5b980cb` (Mar 27 2026). `backfill_enrichment.py` now calls `unwrap_scoring_result()` correctly.
- ~~**Flask config thread safety**~~ — Implemented in commit `4d323a4`. `get_config_snapshot()` lives in `db_helpers.py:84` and `scheduler.py` already uses it consistently in all scheduled jobs.

## Runner Script Template

```python
"""Chunk N: <title>"""
from langgraph_agents.graphs.plan_build_review import plan_build_review_app

WORKSPACE = r"<repo-root>"
TASK = """\
<high-level description + project context>

Context:
- job-cannon is a personal job search Flask app (Python 3.13, Flask 3.1, SQLite, HTMX)
- Use `uv run pytest tests/ -v` to verify all changes
- config.yaml must ONLY be modified with surgical Edit tool, NEVER full Write
- Tests use pytest; `uv run pytest` always (never bare pytest)
"""
PLAN = """\
<copy the corresponding chunk from this document>
"""

def main() -> None:
    result = plan_build_review_app.invoke({
        "task": TASK, "current_plan": PLAN, "current_code": "",
        "workspace_path": WORKSPACE,
        "e2e_verdict": "", "e2e_report": "", "e2e_cycle": 0,
    })
    print(f"E2E verdict: {result.get('e2e_verdict', 'N/A')}")
    print(f"E2E cycles: {result.get('e2e_cycle', 0)}")
    if r := result.get("e2e_report"):
        print(r[-3000:] if len(r) > 3000 else r)

if __name__ == "__main__":
    main()
```
