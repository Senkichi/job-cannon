# Wave 1: UI Polish Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant top collapse button from expanded job rows and add an indigo highlight animation when rows collapse.

**Architecture:** Pure template/CSS changes. No backend modifications. Three template files touched: `base.html` (CSS keyframes), `_row_expanded.html` (remove button + update bottom button handler), `_row.html` (add highlight to compact row collapse path).

**Tech Stack:** Jinja2 templates, CSS animations, vanilla JS (HTMX event handlers)

**Spec:** `docs/superpowers/specs/2026-03-18-wave1-ui-polish-design.md`

---

## Chunk 1: Remove Top Collapse Button & Add Highlight Animation

### Task 1: Add CSS keyframe animation to base.html

**Files:**
- Modify: `job_finder/web/templates/base.html:17-19`

- [ ] **Step 1: Add the highlight-flash animation**

Add a new plain `<style>` block after the existing `<style type="text/tailwindcss">` block in `base.html`. Do NOT put this inside the tailwindcss block.

```html
  <style type="text/tailwindcss">
    @custom-variant dark (&:where(.dark, .dark *));
  </style>
  <style>
    @keyframes highlight-flash {
      0% { background-color: rgba(99, 102, 241, 0.25); }
      100% { background-color: transparent; }
    }
    .highlight-flash { animation: highlight-flash 1.5s ease-out; }
  </style>
```

- [ ] **Step 2: Verify base.html renders**

Run: `python -c "from job_finder.web import create_app; app = create_app(); client = app.test_client(); r = client.get('/'); print(r.status_code)"`

Expected: `200` (or `302` redirect to dashboard)

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/base.html
git commit -m "style: add highlight-flash CSS keyframe animation"
```

### Task 2: Remove top collapse button from expanded row

**Files:**
- Modify: `job_finder/web/templates/jobs/_row_expanded.html:15-39`

- [ ] **Step 1: Remove the collapse button from the header div**

Replace the header div (lines 15-39) with a simplified version that only contains the dates/stale span. Remove the `justify-between` class since there's only one child:

Before:
```html
      <div class="flex items-center justify-between text-xs text-slate-500">
        <span>
          Posted: ...
        </span>
        <button type="button"
                hx-get="/jobs/{{ job.dedup_key | urlencode }}/collapse"
                ...>
          Collapse
          ...
        </button>
      </div>
```

After:
```html
      <div class="flex items-center text-xs text-slate-500">
        <span>
          Posted: <span class="text-slate-400">{{ posted_date }}</span>
          &nbsp;&bull;&nbsp;
          First seen: <span class="text-slate-400">{{ first_seen_date }}</span>
          &nbsp;&bull;&nbsp;
          Last seen: <span class="text-slate-400">{{ last_seen_date }}</span>
          {% if job.is_stale %}
          &nbsp;&bull;&nbsp;
          <span class="text-amber-500 border border-amber-500/50 rounded px-1">Stale</span>
          {% endif %}
        </span>
      </div>
```

Key changes:
- Remove `justify-between` (only one child now)
- Delete the entire `<button>` element (lines 27-38)

- [ ] **Step 2: Run tests to verify template rendering**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass (no template rendering errors)

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/jobs/_row_expanded.html
git commit -m "ui: remove redundant top collapse button from expanded rows"
```

### Task 3: Add highlight animation to bottom collapse button

**Files:**
- Modify: `job_finder/web/templates/jobs/_row_expanded.html:271-285` (bottom collapse button)

- [ ] **Step 1: Update the bottom collapse button's after-request handler**

Find the bottom collapse button (around line 271-285 after the prior edit) and update its `hx-on::after-request` to add the highlight class:

Before:
```
hx-on::after-request="var cr = this.closest('tr').previousElementSibling; cr.dataset.expanded = 'false'; setTimeout(function(){ cr.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }, 100);"
```

After:
```
hx-on::after-request="var cr = this.closest('tr').previousElementSibling; cr.dataset.expanded = 'false'; setTimeout(function(){ cr.classList.remove('highlight-flash'); void cr.offsetWidth; cr.classList.add('highlight-flash'); cr.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }, 100);"
```

The `classList.remove → void offsetWidth → classList.add` sequence forces a browser reflow to restart the animation if triggered twice quickly.

- [ ] **Step 2: Commit**

```bash
git add job_finder/web/templates/jobs/_row_expanded.html
git commit -m "ui: add highlight animation to bottom collapse button"
```

### Task 4: Add highlight animation to compact row collapse path

**Files:**
- Modify: `job_finder/web/templates/jobs/_row.html:35-38`

- [ ] **Step 1: Update the compact row onclick collapse branch**

In `_row.html`, find the `else` branch in the onclick handler (the collapse path, around line 35-38):

Before:
```javascript
        } else {
          setTimeout(function() {
            compactRow.scrollIntoView({behavior: 'smooth', block: 'nearest'});
          }, 100);
        }
```

After:
```javascript
        } else {
          setTimeout(function() {
            compactRow.classList.remove('highlight-flash');
            void compactRow.offsetWidth;
            compactRow.classList.add('highlight-flash');
            compactRow.scrollIntoView({behavior: 'smooth', block: 'nearest'});
          }, 100);
        }
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/jobs/_row.html
git commit -m "ui: add highlight animation to compact row collapse path"
```

### Task 5: Manual verification checklist

- [ ] **Step 1: Start the app and verify**

Run: `python run.py`

Open `http://localhost:5000/jobs` in browser. Verify:
1. Expand a job row → no collapse button at the top of the expanded content
2. Dates/stale badge still display correctly in the header
3. Click the bottom "Collapse" button → compact row flashes indigo briefly (~1.5s)
4. Click a compact row to expand, click it again to collapse → same indigo flash
5. Expand and collapse the same row twice quickly → animation restarts cleanly

- [ ] **Step 2: Final commit if any adjustments needed**
