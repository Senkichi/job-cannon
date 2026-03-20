# Wave 1: Quick UI Polish — Collapse Button & Highlight Animation

## Summary

Two small UX improvements to the job board accordion collapse flow:
1. Remove the redundant top collapse button from expanded rows (clicking the compact row already collapses)
2. Add a brief indigo highlight animation to the compact row after collapse, so the user can locate their position

## Changes

### A. Remove Top Collapse Button

**File:** `job_finder/web/templates/jobs/_row_expanded.html`

Remove the collapse button from the header `<div>` at the top of the expanded row (currently lines 27-38). The dates/stale badge span remains; only the `<button>` element is deleted.

**Rationale:** The compact row's `onclick` handler already triggers collapse. The bottom collapse button (lines 271-285) provides an explicit button after scrolling through long content. A third collapse affordance at the top adds clutter with no UX benefit.

**After this change**, the header row becomes just dates/stale info with `justify-between` simplified to plain layout (no need for space-between with only one child).

### B. Highlight Animation on Collapse

**Files:**
- `job_finder/web/templates/base.html` — CSS keyframes
- `job_finder/web/templates/jobs/_row.html` — compact row onclick collapse path
- `job_finder/web/templates/jobs/_row_expanded.html` — bottom collapse button after-request

**CSS (in base.html):**
```css
@keyframes highlight-flash {
  0% { background-color: rgba(99, 102, 241, 0.25); }
  100% { background-color: transparent; }
}
.highlight-flash { animation: highlight-flash 1.5s ease-out; }
```

Added as a regular `<style>` block (not `type="text/tailwindcss"`) since Tailwind v4 CDN may not compile custom utilities (per project memory).

**Trigger points:**
1. **Compact row onclick** (`_row.html`, collapse branch in the `else` block): After scrollIntoView, add `highlight-flash` class to `compactRow`.
2. **Bottom collapse button** (`_row_expanded.html`, `hx-on::after-request`): After scrollIntoView, add `highlight-flash` class to `cr` (the compact row).

**Animation restart technique:** `classList.remove('highlight-flash'); void el.offsetWidth; classList.add('highlight-flash')` — forces browser reflow to restart the animation if the user collapses the same row twice quickly.

## Testing

- Expand a job row, click the bottom Collapse button — compact row should flash indigo briefly
- Click a compact row to expand, click it again to collapse — same flash
- Verify no top collapse button exists in expanded rows
- Verify the header dates/stale badge still display correctly without the button
- Run `pytest tests/` to ensure no template rendering regressions

## Files Modified

| File | Change |
|------|--------|
| `base.html` | Add `<style>` block with keyframe animation |
| `_row_expanded.html` | Remove top collapse button; update bottom button after-request to add highlight class |
| `_row.html` | Add highlight class in collapse branch of onclick handler |
