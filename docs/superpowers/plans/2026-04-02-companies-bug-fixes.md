# Companies Blueprint Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 9 identified issues in the companies blueprint — dead Alpine.js code, a dead database JOIN, a stale-table bug after scans, missing input validation, import hygiene, a phantom DOM ID, and test coverage gaps.

**Architecture:** All changes are confined to `job_finder/web/blueprints/companies.py`, five templates under `job_finder/web/templates/companies/` and `components/`, and `tests/test_companies.py`. No new files are needed. Tasks are ordered so each produces a fully-passing test suite before the next begins.

**Tech Stack:** Flask 3.1, HTMX 2.x, Jinja2 (jinja2-fragments), SQLite, pytest via `uv run pytest`

---

## Issue Index

| # | File | Issue | Task |
|---|------|-------|------|
| 1 | `_sidebar.html`, `_scan_result.html` | `x-data`/`x-show` Alpine.js directives — Alpine not loaded; dead attributes | 1 |
| 2 | `companies.py` | `job_count_live` LEFT JOIN computed in 4 queries but never rendered in any template | 2 |
| 3 | `companies.py`, `index.html` | `POST /scan` does not refresh the companies table — stale data until manual reload | 6 |
| 4 | `companies.py` | `update_slug` writes `ats_platform` to DB without validating against allowed values | 5 |
| 5 | `companies.py` | `from datetime import datetime` imported inside two route bodies (lines 208, 245) | 4 |
| 6 | `companies.py` | `upsert_company` imported inside `add()` body; `ats_scanner` already imported at module level | 4 |
| 7 | `_row.html` | `id="company-row-{{ company.id }}"` inner div — never targeted by any HTMX call | 3 |
| 8 | `_row.html`, `_row_expanded.html` | Retry spinners have `class="htmx-indicator hidden"` — `hidden` (Tailwind `display:none`) prevents HTMX opacity changes from ever showing them; spinners are broken | 3 |
| 9 | `test_companies.py` | Missing: scan content assertion, add-route DB insertion, `ats_platform` filter | 7 |

---

## File Map

| File | What changes |
|------|-------------|
| `job_finder/web/blueprints/companies.py` | Remove 4 dead JOINs; move imports; add `ats_platform` validation; add `make_response` + `HX-Trigger-After-Settle` header to scan route |
| `job_finder/web/templates/components/_sidebar.html` | Remove `x-data="{ collapsed: false }"` from `<nav>` |
| `job_finder/web/templates/companies/_scan_result.html` | Remove `x-data` / `x-show` Alpine attributes |
| `job_finder/web/templates/companies/_row.html` | Remove `id="company-row-{{ company.id }}"` from inner div; remove `hidden` from retry spinner |
| `job_finder/web/templates/companies/_row_expanded.html` | Remove `hidden` from retry spinner |
| `job_finder/web/templates/companies/index.html` | Add HTMX refresh attributes to `#companies-table` |
| `tests/test_companies.py` | Add 3 missing test cases |

---

## Chunk 1: Dead Code Removal

Covers issues 1, 2, 7, 8. Pure cleanup — no behavior changes. Safe to land first since all existing tests cover these paths.

---

### Task 1: Remove Alpine.js dead code from templates

**Context:** Alpine.js is not loaded in `base.html` (only HTMX 2.0.8, Tailwind CDN, SortableJS). The `x-data` attribute on `_sidebar.html:4` is inert — the sidebar toggle runs via `toggleSidebar()` vanilla JS. The `x-data`/`x-show` pair on `_scan_result.html:6` is also inert — the dismiss is handled by a plain `setTimeout(() => el.remove(), 8000)` on lines 42-46 of that template.

**Files:**
- Modify: `job_finder/web/templates/components/_sidebar.html` — line 4
- Modify: `job_finder/web/templates/companies/_scan_result.html` — line 6

- [ ] **Step 1: Remove `x-data` from sidebar `<nav>` element**

`_sidebar.html:2-4` — change:
```html
<nav id="sidebar"
     class="bg-slate-800 flex flex-col transition-all duration-300 w-56 sticky top-0 h-screen overflow-y-auto flex-shrink-0"
     x-data="{ collapsed: false }">
```
To:
```html
<nav id="sidebar"
     class="bg-slate-800 flex flex-col transition-all duration-300 w-56 sticky top-0 h-screen overflow-y-auto flex-shrink-0">
```

- [ ] **Step 2: Remove both Alpine directives from `_scan_result.html`**

`_scan_result.html:4-7` — remove both `x-data="{ show: true }"` AND `x-show="show"` from the opening `<div>`. Change:
```html
<div id="ats-scan-result"
     class="{% if error %}bg-red-900/40 border-red-700{% else %}bg-emerald-900/40 border-emerald-700{% endif %} border rounded-lg px-4 py-3 flex items-center justify-between"
     x-data="{ show: true }" x-show="show">
```
To:
```html
<div id="ats-scan-result"
     class="{% if error %}bg-red-900/40 border-red-700{% else %}bg-emerald-900/40 border-emerald-700{% endif %} border rounded-lg px-4 py-3 flex items-center justify-between">
```

Both attributes must be removed. `x-show="show"` without Alpine would be an inert raw HTML attribute, causing no display behavior — but it's misleading. The dismiss is fully handled by the `setTimeout(() => el.remove(), 8000)` block on lines 42-46 of the same template.

- [ ] **Step 3: Verify existing tests still pass**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All 21 existing tests PASS. The scan result template is exercised by `TestScanRoute`.

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/templates/components/_sidebar.html job_finder/web/templates/companies/_scan_result.html
git commit -m "chore: remove dead Alpine.js directives from sidebar and scan result templates"
```

---

### Task 2: Remove dead `job_count_live` JOIN from four queries

**Context:** The index, collapse, toggle, and retry routes all compute `COUNT(j.dedup_key) as job_count_live` via a `LEFT JOIN jobs`. No template renders this value — `_row.html:69` shows `company.jobs_found_total`, not `job_count_live`. The JOIN fires on every user interaction with the table.

**Files:**
- Modify: `job_finder/web/blueprints/companies.py` — lines 68-77, 144-151, 217-224, 344-351

- [ ] **Step 1: Simplify `index()` query**

`companies.py:68-77` — change:
```python
    companies = conn.execute(
        f"""SELECT c.*,
               COUNT(j.dedup_key) as job_count_live
            FROM companies c
            LEFT JOIN jobs j ON j.company_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY c.{sort_by} ASC NULLS LAST""",
        params,
    ).fetchall()
```
To:
```python
    companies = conn.execute(
        f"""SELECT c.* FROM companies c
            {where_sql}
            ORDER BY c.{sort_by} ASC NULLS LAST""",
        params,
    ).fetchall()
```

The `where_sql` clauses already use the `c.` alias (`c.name LIKE ?`, `c.ats_platform = ?`), so keeping `FROM companies c` is necessary.

- [ ] **Step 2: Simplify `collapse()` query**

`companies.py:144-151` — change:
```python
    company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()
```
To:
```python
    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
```

- [ ] **Step 3: Simplify `toggle()` post-update fetch**

`companies.py:217-224` — change:
```python
    updated_company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()
```
To:
```python
    updated_company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
```

- [ ] **Step 4: Simplify `retry()` post-probe fetch**

`companies.py:344-351` — change:
```python
    updated_company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()
```
To:
```python
    updated_company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/blueprints/companies.py
git commit -m "perf: remove dead job_count_live JOIN from four company queries"
```

---

### Task 3: Remove unused inner row ID and fix broken retry spinners

**Context — phantom ID:** `_row.html:6` wraps row content in `<div id="company-row-{{ company.id }}">`. No HTMX call targets `#company-row-{id}` — all expand/collapse/toggle/retry calls use `#company-{{ company.id }}` (the `_table.html` wrapper div). The inner ID is dead.

**Context — broken spinners:** Both retry buttons use `hx-indicator="#retry-spinner-{{ company.id }}"` pointing to a `<span class="htmx-indicator hidden">`. When HTMX activates an `hx-indicator` element, it adds the `htmx-request` CSS class to it. The CSS rule `.htmx-request.htmx-indicator { opacity: 1 }` would make it visible — but Tailwind's `hidden` utility applies `display: none`, which HTMX's CSS-only approach does not override. The spinners are permanently invisible, even during active requests. The scan spinner in `index.html` has no `hidden` class and works correctly.

**Fix:** Remove `hidden` from the retry spinners. HTMX's opacity-based CSS can then show them during requests. The trade-off: the `<span>` (3×3 SVG) takes up layout space in the button at rest (opacity: 0, so visually invisible, but occupying horizontal space). The button already uses `flex items-center gap-1` so the spinner slot is a small fixed addition.

**Files:**
- Modify: `job_finder/web/templates/companies/_row.html` — lines 6, 83
- Modify: `job_finder/web/templates/companies/_row_expanded.html` — line 45

- [ ] **Step 1: Remove unused inner ID from `_row.html`**

`_row.html:6-9` — change:
```html
<div id="company-row-{{ company.id }}"
     class="grid grid-cols-7 gap-4 px-4 py-3 border-b border-slate-700/50 hover:bg-slate-750 transition-colors items-center
            {% if is_disabled %}opacity-50{% endif %}">
```
To:
```html
<div class="grid grid-cols-7 gap-4 px-4 py-3 border-b border-slate-700/50 hover:bg-slate-750 transition-colors items-center
            {% if is_disabled %}opacity-50{% endif %}">
```

- [ ] **Step 2: Remove `hidden` from retry spinner in `_row.html`**

`_row.html:83` — change:
```html
      <span class="htmx-indicator hidden" id="retry-spinner-{{ company.id }}">
```
To:
```html
      <span class="htmx-indicator" id="retry-spinner-{{ company.id }}">
```

- [ ] **Step 3: Remove `hidden` from retry spinner in `_row_expanded.html`**

`_row_expanded.html:45` — change:
```html
        <span class="htmx-indicator hidden" id="retry-spinner-exp-{{ company.id }}">
```
To:
```html
        <span class="htmx-indicator" id="retry-spinner-exp-{{ company.id }}">
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/templates/companies/_row.html job_finder/web/templates/companies/_row_expanded.html
git commit -m "fix: remove phantom row ID and fix broken retry spinners (htmx-indicator hidden prevented display)"
```

---

## Chunk 2: Correctness Fixes

Covers issues 4, 5, 6, 3. These change observable behavior and require test-first implementation.

---

### Task 4: Fix imports in companies.py

**Context:** `from datetime import datetime` is imported inside `toggle()` (line 208) and `update_slug()` (line 245). `upsert_company` is imported inside `add()` (line 173) even though `ats_scanner` is already imported at module level for `probe_ats_slugs` and `run_ats_scan`. These lazy imports work but are stylistically wrong and inconsistent.

**Files:**
- Modify: `job_finder/web/blueprints/companies.py` — lines 13-28, 173-174, 208, 245

No new tests needed — existing tests cover these paths.

- [ ] **Step 1: Add `datetime` and `upsert_company` to module-level imports**

`companies.py:13-28` — change the import block from:
```python
import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.web.ats_prober import probe_single_company
from job_finder.web.ats_scanner import probe_ats_slugs, run_ats_scan
from job_finder.web.db_helpers import get_db
```
To:
```python
import logging
from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.web.ats_prober import probe_single_company
from job_finder.web.ats_scanner import probe_ats_slugs, run_ats_scan, upsert_company
from job_finder.web.db_helpers import get_db
```

- [ ] **Step 2: Remove lazy `from datetime import datetime` from `toggle()` body**

`companies.py` inside `toggle()` — delete the line:
```python
    from datetime import datetime
```
Leave `now = datetime.now().isoformat()` in place (now resolves via module-level import).

- [ ] **Step 3: Remove lazy `from datetime import datetime` from `update_slug()` body**

Same deletion inside `update_slug()`.

- [ ] **Step 4: Remove lazy `upsert_company` import from `add()` body**

`companies.py:173-174` — change:
```python
    try:
        from job_finder.web.ats_scanner import upsert_company
        company_id = upsert_company(
```
To:
```python
    try:
        company_id = upsert_company(
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/blueprints/companies.py
git commit -m "refactor: move datetime and upsert_company to module-level imports in companies.py"
```

---

### Task 5: Validate `ats_platform` in `update_slug` route

**Context:** `update_slug()` writes `ats_platform` directly from form data to the DB without checking it against allowed values. The module-level `_ATS_PLATFORM_FILTER_VALUES` serves the search filter and includes `"none"` and `""` — those values are meaningless for writes. A separate write-path allowlist is needed.

The form dropdown emits `""` for None, which `or None` converts to `None`. So the valid stored values are: `"lever"`, `"greenhouse"`, `"ashby"`, `None`. The string `"none"` never reaches storage (it's already converted to `None` by `or None`).

**Files:**
- Modify: `job_finder/web/blueprints/companies.py` — module-level constants block (~line 33-35), `update_slug()` body (~line 242)
- Modify: `tests/test_companies.py` — `TestUpdateSlugRoute` class

- [ ] **Step 1: Write the failing test**

Add to `TestUpdateSlugRoute` in `tests/test_companies.py`:
```python
def test_update_slug_rejects_invalid_platform(self, companies_client):
    """POST update-slug with an unrecognized ats_platform returns 400."""
    client, db_path, conn = companies_client
    company_id = _insert_company(conn)
    response = client.post(
        f"/companies/{company_id}/update-slug",
        data={"ats_platform": "fakeats", "ats_slug": "test"},
    )
    assert response.status_code == 400
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_companies.py::TestUpdateSlugRoute::test_update_slug_rejects_invalid_platform -v`
Expected: FAIL — route currently returns 200 for any value.

- [ ] **Step 3: Add `_ATS_WRITE_PLATFORMS` constant at module level**

`companies.py:33-35` currently reads:
```python
# Validated allowlist for sort_by (no parameterized column names in SQLite)
_SORT_ALLOWLIST = {"name", "ats_platform", "last_scanned_at", "jobs_found_total"}
_ATS_PLATFORM_FILTER_VALUES = {"lever", "greenhouse", "ashby", "none", ""}
```
Change to:
```python
# Validated allowlist for sort_by (no parameterized column names in SQLite)
_SORT_ALLOWLIST = {"name", "ats_platform", "last_scanned_at", "jobs_found_total"}
_ATS_PLATFORM_FILTER_VALUES = {"lever", "greenhouse", "ashby", "none", ""}
# Valid stored values for ats_platform column (None = no ATS; "" is converted before this check)
_ATS_WRITE_PLATFORMS = {"lever", "greenhouse", "ashby", None}
```

- [ ] **Step 4: Add validation to `update_slug()`, after the 404 guard**

The `update_slug()` function flow is: 404 guard (line ~235-240) → form parsing (line ~242-243) → DB update. The validation MUST go after form parsing (so `or None` conversion has already run) and after the 404 guard (so a bad platform on a non-existent company correctly returns 404, not 400). Insert after line ~243:
```python
    ats_platform = request.form.get("ats_platform", "").strip() or None
    ats_slug = request.form.get("ats_slug", "").strip() or None

    if ats_platform not in _ATS_WRITE_PLATFORMS:
        return f"Invalid ats_platform: {ats_platform!r}", 400
```

`ats_platform` is already `None` when the form sends `""` (via `or None`), so the `"none"` dropdown option passes the check as `None`.

- [ ] **Step 5: Run full TestUpdateSlugRoute suite**

Run: `uv run pytest tests/test_companies.py::TestUpdateSlugRoute -v`
Expected: All 5 tests pass (4 existing + 1 new).

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/blueprints/companies.py tests/test_companies.py
git commit -m "fix: validate ats_platform against allowlist in update_slug route"
```

---

### Task 6: Refresh companies table after ATS scan completes

**Context:** `POST /companies/scan` returns only the `_scan_result.html` banner fragment into `#scan-result`. The companies table (`#companies-table`) is not updated — `last_scanned_at`, `jobs_found_total`, and any newly-discovered companies remain stale until the user reloads the page.

**Fix:** Return `HX-Trigger-After-Settle: refreshCompaniesTable` from the scan route. In `index.html`, attach HTMX attributes to `#companies-table` that listen for this event and re-fetch `/companies` as a fragment.

`HX-Trigger-After-Settle` fires the named event after HTMX has settled its DOM updates. The event fires on the element that triggered the request (the scan button) and bubbles to `body`. The `#companies-table` div listens with `hx-trigger="refreshCompaniesTable from:body"`. When triggered, it GETs `/companies` — which returns the `_table.html` fragment because HTMX sends `HX-Request: true` — and swaps its own `innerHTML`.

**Filter state:** Without `hx-include`, the automatic refresh would GET `/companies` with no query params, silently resetting any active search or platform filter. `hx-include=".filter-input"` includes the current values of both filter inputs (`.filter-input` class) so the refreshed table respects the user's active filters.

**Files:**
- Modify: `job_finder/web/blueprints/companies.py` — scan route (~line 280-303)
- Modify: `job_finder/web/templates/companies/index.html` — `#companies-table` div (~line 96)
- Modify: `tests/test_companies.py` — `TestScanRoute` class

- [ ] **Step 1: Write the failing test**

Add to `TestScanRoute` in `tests/test_companies.py`:
```python
def test_scan_returns_hx_trigger_after_settle_header(self, companies_client):
    """POST /companies/scan response includes HX-Trigger-After-Settle to refresh table."""
    client, db_path, conn = companies_client
    response = client.post("/companies/scan")
    assert response.status_code == 200
    assert "HX-Trigger-After-Settle" in response.headers
    assert "refreshCompaniesTable" in response.headers["HX-Trigger-After-Settle"]
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `uv run pytest tests/test_companies.py::TestScanRoute::test_scan_returns_hx_trigger_after_settle_header -v`
Expected: FAIL — header not currently returned.

- [ ] **Step 3: Add `make_response` to module-level Flask imports**

`companies.py` Flask import block — add `make_response`:
```python
from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
```

- [ ] **Step 4: Update scan route to return `HX-Trigger-After-Settle` header**

`companies.py:302-303` — change the final return from:
```python
    # render_template is OUTSIDE the try block — TemplateErrors propagate as 500
    return render_template("companies/_scan_result.html", result=result, error=scan_error)
```
To:
```python
    # render_template is OUTSIDE the try block — TemplateErrors propagate as 500
    resp = make_response(
        render_template("companies/_scan_result.html", result=result, error=scan_error)
    )
    resp.headers["HX-Trigger-After-Settle"] = "refreshCompaniesTable"
    return resp
```

- [ ] **Step 5: Add HTMX refresh attributes to `#companies-table` in `index.html`**

`index.html:95-98` — change:
```html
  <!-- Companies table -->
  <div id="companies-table">
    {% include "companies/_table.html" %}
  </div>
```
To:
```html
  <!-- Companies table -->
  <div id="companies-table"
       hx-get="/companies"
       hx-trigger="refreshCompaniesTable from:body"
       hx-target="this"
       hx-swap="innerHTML"
       hx-include=".filter-input">
    {% include "companies/_table.html" %}
  </div>
```

When `refreshCompaniesTable` bubbles to `body`, HTMX fires `GET /companies` with `HX-Request: true` and includes the current `.filter-input` values (search text and platform dropdown). The index route returns the `_table.html` fragment matching the active filters, which is swapped as `innerHTML` of `#companies-table`.

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All pass including new test.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/blueprints/companies.py job_finder/web/templates/companies/index.html tests/test_companies.py
git commit -m "feat: refresh companies table after ATS scan via HX-Trigger-After-Settle"
```

---

## Chunk 3: Test Coverage Gaps

Covers issue 9. Adds the three missing test cases.

---

### Task 7: Add missing test coverage

**Context:** Three gaps in `tests/test_companies.py`:
1. `TestScanRoute` only checks `status == 200` and `len > 0` — never asserts on rendered text
2. `TestAddRoute` mocks `upsert_company` entirely — never verifies a row was inserted
3. `TestIndexRoute` has no test for `ats_platform` filter behavior

**Files:**
- Modify: `tests/test_companies.py`

- [ ] **Step 1: Add scan result content assertion**

Add to `TestScanRoute`:
```python
def test_scan_result_contains_expected_text(self, companies_client):
    """POST /companies/scan renders a result message (TESTING guard returns zero-count dict)."""
    client, db_path, conn = companies_client
    response = client.post("/companies/scan")
    assert response.status_code == 200
    body = response.data.decode()
    # TESTING guard causes probe_ats_slugs + run_ats_scan to return immediately
    # so result dict is valid and renders the success branch
    assert "ATS scan complete" in body or "ATS scan failed" in body
```

- [ ] **Step 2: Add add-route DB insertion test**

Add to `TestAddRoute`:
```python
def test_add_inserts_company_in_db(self, companies_client):
    """POST /companies/add with a valid name creates a row in the companies table."""
    client, db_path, conn = companies_client
    # No mock — exercises the real upsert_company path.
    # TESTING flag prevents downstream probe_ats_slugs HTTP calls.
    response = client.post(
        "/companies/add",
        data={"company_name": "IntegCorp"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    row = conn.execute(
        "SELECT * FROM companies WHERE name_raw = ?", ("IntegCorp",)
    ).fetchone()
    assert row is not None
```

- [ ] **Step 3: Add ATS platform filter test**

Add to `TestIndexRoute`:
```python
def test_index_ats_platform_filter(self, companies_client):
    """ats_platform=lever returns only lever companies, excludes others."""
    client, db_path, conn = companies_client
    _insert_company(conn, name="LeverCo", ats_platform="lever")
    _insert_company(conn, name="GreenCo", ats_platform="greenhouse")
    response = client.get("/companies/?ats_platform=lever")
    assert response.status_code == 200
    assert b"LeverCo" in response.data
    assert b"GreenCo" not in response.data
```

- [ ] **Step 4: Run all new tests**

Run: `uv run pytest tests/test_companies.py -v`
Expected: All pass.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest tests/ -x`
Expected: All pass (no regressions introduced).

- [ ] **Step 6: Commit**

```bash
git add tests/test_companies.py
git commit -m "test: add scan content, add DB insertion, and ats_platform filter coverage"
```

---

## Completion Checklist

- [ ] Chunk 1 complete: Alpine dead code, dead JOIN, phantom ID, indicator class all cleaned up
- [ ] Chunk 2 complete: Imports fixed, `ats_platform` validated, scan triggers table refresh
- [ ] Chunk 3 complete: Three test gaps closed
- [ ] Full test suite green: `uv run pytest tests/ -x`
