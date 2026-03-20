# Wave 3: Archived Jobs Section Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a collapsible archived jobs section to the job board with lazy-loading, and animate rows out when archiving with a fade+collapse effect.

**Architecture:** New `/jobs/archived-table` HTMX partial route, collapsible section in index.html with lazy-load, CSS animation for archive action, OOB counter update, suppress `jobs-updated` trigger for archive to prevent animation conflict.

**Tech Stack:** Flask, Jinja2, HTMX, CSS animations, vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-18-wave3-archived-jobs-section-design.md`

---

## Chunk 1: Route & Collapsible Section

### Task 1: Add archived-table route

**Files:**
- Modify: `job_finder/web/blueprints/jobs.py`

- [ ] **Step 1: Write test for the new route**

```python
def test_archived_table_route(client, app):
    """GET /jobs/archived-table should return table fragment."""
    resp = client.get("/jobs/archived-table")
    assert resp.status_code == 200
```

Add to `tests/test_jobs_blueprint.py` (or the existing jobs test file).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ -k "archived_table_route" -v`

Expected: FAIL (404 — route doesn't exist)

- [ ] **Step 3: Add the route and archived_count to index**

In `job_finder/web/blueprints/jobs.py`:

**Add the archived-table route BEFORE the `/<path:dedup_key>/expand` route** (route ordering matters — per CLAUDE.md):

```python
@jobs_bp.route("/archived-table", strict_slashes=False)
def archived_table():
    """HTMX partial — archived job rows for the collapsible section."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    jobs = get_filtered_jobs(conn, status="archived", sort_by="first_seen", sort_dir="DESC", limit=200)

    return render_template(
        "jobs/_table.html",
        jobs=jobs,
        pipeline_statuses=PIPELINE_STATUSES,
    )
```

**In the `index()` route**, add `archived_count` query and pass to template:

```python
    stale_count = _get_stale_count(conn)
    archived_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
    ).fetchone()[0]

    return render_template(
        "jobs/index.html",
        jobs=jobs,
        filters=request.args,
        pipeline_statuses=PIPELINE_STATUSES,
        locations=locations,
        sources=sources,
        stale_count=stale_count,
        archived_count=archived_count,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ -k "archived_table_route" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/blueprints/jobs.py tests/test_jobs_blueprint.py
git commit -m "feat: add /jobs/archived-table route and archived_count context"
```

### Task 2: Add collapsible archived section to index.html

**Files:**
- Modify: `job_finder/web/templates/jobs/index.html`

- [ ] **Step 1: Add the collapsible section markup**

In `jobs/index.html`, after the main table's closing `</div>` (the `overflow-x-auto` div) and before the closing `</div>{% endblock %}`, add:

```html
  <!-- Archived jobs collapsible section -->
  {% if archived_count > 0 %}
  <div class="mt-6 border border-slate-700 rounded-lg">
    <button type="button"
            id="archived-toggle"
            class="w-full flex items-center justify-between px-4 py-3 text-sm text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 rounded-lg transition-colors"
            onclick="
              var body = document.getElementById('archived-body');
              var arrow = document.getElementById('archived-arrow');
              if (body.classList.contains('hidden')) {
                body.classList.remove('hidden');
                arrow.style.transform = 'rotate(90deg)';
                if (!body.dataset.loaded) {
                  htmx.ajax('GET', '/jobs/archived-table', {target: '#archived-table-body', swap: 'innerHTML'});
                  body.dataset.loaded = 'true';
                }
              } else {
                body.classList.add('hidden');
                arrow.style.transform = '';
              }
            ">
      <span class="flex items-center gap-2">
        <span id="archived-arrow" class="transition-transform duration-200" style="display:inline-block">&#9654;</span>
        Archived Jobs
        <span id="archived-count" class="px-1.5 py-0.5 rounded text-xs bg-slate-700 text-slate-400 font-mono">{{ archived_count }}</span>
      </span>
    </button>
    <div id="archived-body" class="hidden">
      <div class="overflow-x-auto border-t border-slate-700">
        <table class="text-xs text-left border-collapse w-full" style="table-layout: fixed;">
          <colgroup>
            <col style="width: 5%;">
            <col style="width: 28%;">
            <col style="width: 16%;">
            <col style="width: 14%;">
            <col style="width: 10%;">
            <col style="width: 12%;">
            <col style="width: 15%;">
          </colgroup>
          <thead>
            <tr class="border-b border-slate-700 text-slate-400 uppercase tracking-wide">
              <th class="px-1 py-2">Score</th>
              <th class="px-2 py-2">Title</th>
              <th class="px-2 py-2">Company</th>
              <th class="px-2 py-2">Location</th>
              <th class="px-1 py-2">Salary</th>
              <th class="px-2 py-2">Posted</th>
              <th class="px-1 py-2">Status</th>
            </tr>
          </thead>
          <tbody id="archived-table-body">
            <tr><td colspan="7" class="px-4 py-6 text-center text-slate-500 text-xs">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  {% endif %}
```

- [ ] **Step 2: Verify the page renders**

Run: `python -c "from job_finder.web import create_app; app = create_app(); client = app.test_client(); r = client.get('/jobs/'); print(r.status_code, 'archived-toggle' in r.data.decode())"`

Expected: `200 True` (if there are archived jobs in the DB) or `200 False` (if no archived jobs)

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/jobs/index.html
git commit -m "feat: add collapsible archived jobs section with lazy-loading"
```

## Chunk 2: Archive Animation & OOB Counter

### Task 3: Add archive-fadeout CSS animation

**Files:**
- Modify: `job_finder/web/templates/base.html`

- [ ] **Step 1: Add the animation to the existing `<style>` block**

In `base.html`, add to the plain `<style>` block (created in Wave 1, or create it now if Wave 1 hasn't been implemented yet):

```css
    @keyframes archive-fadeout {
      0% { opacity: 1; }
      60% { opacity: 0; max-height: 200px; }
      100% { opacity: 0; max-height: 0; padding: 0; overflow: hidden; }
    }
    .archive-fadeout {
      animation: archive-fadeout 0.8s ease-out forwards;
    }
```

- [ ] **Step 2: Commit**

```bash
git add job_finder/web/templates/base.html
git commit -m "style: add archive-fadeout CSS animation"
```

### Task 4: Add archiveRow() JS function and wire archive button

**Files:**
- Modify: `job_finder/web/templates/jobs/index.html` (extra_scripts block)
- Modify: `job_finder/web/templates/jobs/_row_expanded.html` (archive button)

- [ ] **Step 1: Add archiveRow() function to index.html**

In the `{% block extra_scripts %}` section of `jobs/index.html`, add inside the existing `<script>` tag (after the resortByScore IIFE):

```javascript
function archiveRow(el) {
  var expandedTr = el.closest('tr');
  if (!expandedTr) return;
  var compactTr = expandedTr.previousElementSibling;

  // Add fadeout animation to both rows
  expandedTr.classList.add('archive-fadeout');
  if (compactTr) compactTr.classList.add('archive-fadeout');

  // Remove from DOM after animation completes
  setTimeout(function() {
    if (expandedTr.parentNode) expandedTr.remove();
    if (compactTr && compactTr.parentNode) compactTr.remove();
  }, 850); // slightly longer than 0.8s animation
}
```

- [ ] **Step 2: Update archive button in _row_expanded.html**

Find the archive button (around line 255-267 in `_row_expanded.html`). Add `hx-on::after-request="archiveRow(this)"`:

Before:
```html
        <button type="button"
                hx-post="/jobs/{{ job.dedup_key | urlencode }}/status"
                hx-vals='{"pipeline_status": "archived"}'
                hx-target="#status-cell-{{ jd_key_safe }}"
                hx-swap="innerHTML"
                hx-confirm="Archive this job?"
                hx-disable-elt="this"
```

After:
```html
        <button type="button"
                hx-post="/jobs/{{ job.dedup_key | urlencode }}/status"
                hx-vals='{"pipeline_status": "archived"}'
                hx-target="#status-cell-{{ jd_key_safe }}"
                hx-swap="innerHTML"
                hx-confirm="Archive this job?"
                hx-disable-elt="this"
                hx-on::after-request="archiveRow(this)"
```

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/jobs/index.html job_finder/web/templates/jobs/_row_expanded.html
git commit -m "feat: add archive row fade-out animation"
```

### Task 5: Suppress jobs-updated trigger & add OOB counter

**Files:**
- Modify: `job_finder/web/blueprints/jobs.py:248-255` (status route)

- [ ] **Step 1: Modify the status route response for archives**

In `jobs.py`, replace the current archive-specific response logic (lines 248-255):

Before:
```python
    resp = make_response(render_template(
        "jobs/_status_cell.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
    ))
    if new_status == "archived":
        resp.headers["HX-Trigger"] = "jobs-updated"
    return resp
```

After:
```python
    status_html = render_template(
        "jobs/_status_cell.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
    )

    if new_status == "archived":
        # Don't fire jobs-updated — archiveRow() handles DOM removal client-side.
        # Firing jobs-updated would cause tbody refetch, killing the fade animation.
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
        ).fetchone()[0]
        oob_html = f'<span id="archived-count" hx-swap-oob="innerHTML">{archived_count}</span>'
        return make_response(status_html + oob_html)

    return make_response(status_html)
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/blueprints/jobs.py
git commit -m "feat: suppress jobs-updated on archive, add OOB archived count update"
```

### Task 6: Manual verification

- [ ] **Step 1: Test the full archive flow**

Run: `python run.py`, open `http://localhost:5000/jobs`

1. Expand a job row
2. Click "Archive" → confirm
3. Verify: row fades out over ~0.8s
4. Verify: archived count badge increments (or section appears if first archive)
5. Click the "Archived Jobs" section header → should expand and lazy-load
6. Verify: the just-archived job appears in the archived table
7. Collapse and re-expand → no re-fetch (instant toggle)
