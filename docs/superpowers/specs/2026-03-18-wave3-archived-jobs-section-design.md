# Wave 3: Collapsible Archived Jobs Section

## Summary

Add a collapsible "Archived Jobs" section at the bottom of the job board page. Archived jobs are lazy-loaded on first expand. When a job is archived, it fades out of the main table with a smooth animation and the archived counter updates.

## New Route

**File:** `job_finder/web/blueprints/jobs.py`

```python
@jobs_bp.route("/archived-table", strict_slashes=False)
def archived_table():
    """HTMX partial — archived job rows for the collapsible section."""
```

- Queries `get_filtered_jobs(conn, status="archived", sort_by="first_seen", sort_dir="DESC", limit=200)`
- Returns `render_template("jobs/_table.html", jobs=jobs, pipeline_statuses=PIPELINE_STATUSES)`
- Must be registered **before** the `/<path:dedup_key>/expand` catch-all route to avoid route shadowing (per CLAUDE.md architecture decision)

## Index Page Changes

**File:** `job_finder/web/templates/jobs/index.html`

### Archived count in context

The `index()` route must pass `archived_count` to the template:

```python
archived_count = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
).fetchone()[0]
```

### Collapsible section markup

Below the main table's closing `</div>`, add a collapsible section (only rendered when `archived_count > 0`):

- **Toggle button:** Full-width clickable header with arrow indicator (▶ rotates to ▼), "Archived Jobs" label, and count badge
- **Body:** Hidden by default (`class="hidden"`), contains a full `<table>` with the same `<colgroup>` and `<thead>` as the main table (duplicate the markup; the tbody is the lazy-loaded part)
- **Lazy-load:** On first expand, `htmx.ajax('GET', '/jobs/archived-table', ...)` populates the tbody. A `data-loaded` flag prevents re-fetching on subsequent toggles.
- **Counter badge ID:** `id="archived-count"` — used by OOB swap when archiving a job

### Arrow animation

CSS transition on the arrow span: `transition: transform 0.2s`. Toggle sets `transform: rotate(90deg)` when expanded.

## Archive Action: Fade + Collapse Animation

### Current flow

The archive button in `_row_expanded.html` (line ~256-267) posts to `/jobs/<key>/status` with `hx-vals='{"pipeline_status": "archived"}'`. Currently targets `#status-cell-<key>` with `innerHTML` swap to update the dropdown.

### New flow

Change the archive action to trigger a row-removal animation instead of just updating the status cell.

**Critical: `jobs-updated` trigger conflict.** The current status route fires `HX-Trigger: jobs-updated` when archiving, which causes the main tbody to refetch all rows — killing any in-flight animation. Fix: when `new_status == "archived"`, do NOT set the `HX-Trigger: jobs-updated` header. The `archiveRow()` JS function handles removal client-side, and the tbody doesn't need to refetch (the row is being removed, not updated).

**Approach:** Add a client-side event handler that intercepts the successful archive response and animates the row out.

1. **Archive button changes:** Keep the existing `hx-post` but add `hx-on::after-request` to trigger the animation:
   ```
   hx-on::after-request="archiveRow(this)"
   ```

2. **`archiveRow(el)` function** (in `jobs/index.html` extra_scripts block):
   - Find the expanded `<tr>` (closest tr to the button)
   - Find the compact `<tr>` (previousElementSibling of the expanded row)
   - Add `archive-fadeout` class to both rows
   - After animation completes (800ms), remove both rows from DOM

3. **CSS animation** (in `base.html`, in a new plain `<style>` block — or shared with Wave 1's highlight-flash if that block already exists; NOT in the `type="text/tailwindcss"` block):
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

4. **OOB counter update:** The status change route (`POST /jobs/<key>/status`) should include an OOB fragment that updates the archived count badge. Add to the response:
   ```html
   <span id="archived-count" hx-swap-oob="innerHTML">{{ new_count }}</span>
   ```

### Status route modification

**File:** `job_finder/web/blueprints/jobs.py`, in the status change handler.

When the new status is `"archived"`, the response should include the OOB counter update. This requires querying the new archived count after the status update:

```python
if new_status == "archived":
    archived_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
    ).fetchone()[0]
    # Include OOB span in response
```

The current status route returns `render_template("jobs/_status_cell.html", ...)`. The OOB span is appended after this fragment in the response body (concatenated HTML — HTMX processes both the main swap target and the OOB element).

## Edge Cases

- **Archived section not yet in DOM:** If archived_count was 0 when the page loaded, the section doesn't exist. The OOB swap for the counter badge will silently fail (HTMX ignores OOB targets that don't exist). This is fine — on next page load, the section will appear with count=1.
- **Job expanded when archived:** The animation handles both the expanded and compact rows.
- **Archived section already expanded:** If the user archives a job while the archived section is open, the new job won't appear there until the section is collapsed and re-expanded (or page reload). Acceptable for v1 — no need to append in real-time.

## Testing

- Archive a job from the expanded view — row should fade out over ~0.8s
- Archived count badge should increment
- Expand the archived section — archived job should appear
- Collapse and re-expand — should not re-fetch (data-loaded flag)
- Page with 0 archived jobs — section should not render
- Direct browser access to `/jobs/archived-table` — returns fragment only (no HX-Request full-page fallback; this is a partial-only route like `/jobs/table`)
- Run `pytest tests/` for regression

## Files Modified

| File | Change |
|------|--------|
| `jobs.py` (blueprint) | Add `archived_table()` route; pass `archived_count` to index; add OOB counter in status route |
| `jobs/index.html` | Add collapsible archived section; add `archiveRow()` JS function |
| `base.html` | Add `archive-fadeout` CSS animation |
| `_row_expanded.html` | Add `hx-on::after-request="archiveRow(this)"` to archive button |
