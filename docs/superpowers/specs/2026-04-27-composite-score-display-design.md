# Composite Score Display — Design

**Date:** 2026-04-27
**Status:** Approved by user, ready for implementation plan
**Scope:** Job board score-cell visual change. Pure presentation layer.

## Problem

Phase 34 replaced the legacy 0–100 numeric score with a four-way classification badge (Apply / Consider / Skip / Reject) backed by 6 ordinal sub-scores. The classification system is better suited to LLM scoring strengths and is staying.

**Pain point:** every Apply job now looks identical (uniform green badge). The user can no longer scan downward and stop when relevant jobs stop surfacing — the relative-ranking signal that made the old system useful for triage is invisible. The system *does* sort Apply jobs by `sub_score_sum` underneath as a tiebreaker, but that ordering is hidden from the eye.

## Goal

Surface the existing within-classification ranking so the user can scan a sorted list top-down and judge attractiveness at a glance, without losing the classification model that's already in production.

## Solution Summary

Replace the classification badge in the Score cell with a **classification-colored composite number** (raw sum of 6 sub-scores, range 6–30). Add a Tailwind hover-tooltip showing the 6 sub-scores plus the top strength and top gap from the existing rationale. Sort logic, schema, and DB queries remain unchanged.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D-1 | Drop the Apply/Consider/Skip/Reject text badge entirely | User explicitly does not need it; the colored number conveys both magnitude and classification |
| D-2 | Composite scale = raw sum of 6 sub-scores (6–30) | Faithful representation; mean × 20 fabricates precision the source data doesn't have (only 25 distinct values, irrational spacing) |
| D-3 | Color by classification, flat per class (no intensity gradient) | apply=green, consider=amber, skip=slate, reject=red, NULL=muted-slate. The number conveys magnitude; varying color too creates visual noise |
| D-4 | Tooltip = 3 lines: sub-scores compact line + top strength + top gap | Sub-scores answer "how strong"; strength+gap answer "on what dimension". One-line per logical chunk, fast to read |
| D-5 | Tooltip via Tailwind CSS-only (no JS, no native `title`) | Native `title` has ~500ms hover delay user explicitly rejected. Tailwind `group` + `group-hover:visible` matches dark theme, instant appear |
| D-6 | Sort key unchanged: classification rank DESC, then sub_score_sum DESC | A 27 Skip beating an 18 Apply by raw sum would violate the "willing to apply" floor that the classification floor encodes. Floor is load-bearing |
| D-7 | `data-sort-score` carries packed key `classification_rank * 100 + sub_score_sum` | Preserves single-key client-side JS sort in `index.html:330-364` without refactor; stays in parity with server two-key sort |
| D-8 | No new DB column for composite | `sub_scores_json` round-trips through SQLite `json_extract` already (used in `db.py:550-555`); a stored column adds a sync hazard with zero perf gain |
| D-9 | No helper function; compose composite inline in Jinja `{% set %}` | Template already parses `sub_scores_json` via `from_json` filter; six adds is trivial inline |
| D-10 | Score column header stays "Score" | Still describes what's there; renaming to "Fit" doesn't add information |

## Visual

```
Score column (8% width, right-aligned):

  30 *      ← Apply, sum 30, freshness star (≤ 3 days)
  28
  18        ← weakest Apply still ≥ 3 in every axis
  22        ← Consider; classification floor pushes below weakest Apply
  20
  12
  27        ← Skip with one axis at 2; ranks high within Skip but below Consider floor
  06        ← Reject; legitimacy_note or any axis = 1
  —         ← Unscored / pre-v3
```

Color mapping (Tailwind):

| Classification | Text color | NULL handling |
|---|---|---|
| apply | `text-green-400 font-semibold` | — |
| consider | `text-amber-400 font-semibold` | — |
| skip | `text-slate-400 font-semibold` | — |
| reject | `text-red-400 font-semibold` | — |
| NULL | `text-slate-600` glyph `—` | placeholder |

Freshness star (`*` for jobs first_seen ≤ 3 days, existing logic) preserved, appended after the number.

## Tooltip

Trigger: CSS-only via Tailwind `group` / `group-hover:visible` pattern. Markup lives inside the `<td>`, hidden div positioned absolutely, revealed on hover.

Content (three logical lines):

```
Title 5 · Location 4 · Comp 5 · Domain 4 · Seniority 5 · Skills 4
Strength: Deep platform experience aligns with infra-heavy stack
Gap: No published Kubernetes operator work
```

Source: `sub_scores_json` (already parsed in template scope) and `fit_analysis` JSON column (parsed via `from_json`).

Edge cases:
- `fit_analysis` missing or unparseable → tooltip shows only the sub-score line.
- `rationale.strengths == []` → omit Strength line.
- `rationale.gaps == []` → omit Gap line.
- All missing → tooltip is just the sub-score line (still useful).
- Long strength/gap text → truncate at ~120 chars with `…` suffix.
- NULL classification → no tooltip, or a minimal "Not yet scored" tooltip.

## Sort behavior

Unchanged. The composite (`sub_score_sum`) is already the secondary sort key behind classification rank in `db.py:_classification_score_order`. No DB or query changes.

Operational consequence the user has accepted: numbers do **not** decrease monotonically going down the list — color shifts mark classification boundaries (`30, 28, 18, 22, 20, 12, 27, 06, —`).

## Files Touched

### Modified

- **`job_finder/web/templates/jobs/_score_cell.html`** — Full rewrite. Drop badge markup; render colored composite number; build CSS tooltip group with sub-scores + strength + gap from `fit_analysis`. Preserve `id="score-{dedup_key}"` (OOB swap target), freshness star, and `data-sort-score` attribute (with new packed-key value).
- **`job_finder/web/templates/jobs/index.html:303-364`** — No code change. The `resortByScore()` JS reads `data-sort-score` and is single-key — works unchanged once `_score_cell.html` writes the packed key.
- **`job_finder/web/blueprints/jobs.py`** — Confirm `fit_analysis` is in row context (it already is via `JOBS_ALL_COLUMNS`); no change expected. Document if a defensive edit is needed.

### Not touched

- `_row_expanded.html` Fit Breakdown section (sub-scores in expanded view stay as-is).
- `_row_detail.html` (detail page sub-scores stay as-is).
- `db.py` sort logic (`_classification_score_order`, `_SUB_SCORE_SUM_SQL`).
- DB schema, migrations, fixtures.
- `_status_cell.html`, `_table.html`, `_row.html` (no score-display changes).

## Tests

Add to `tests/test_views.py` (or split out `tests/test_score_cell.py` if test file grows):

- Apply row with sub-scores `(5,4,5,4,5,4)` → composite reads `27`, color class is `text-green-400`.
- Each classification → correct color class (apply/consider/skip/reject).
- NULL classification → `—` glyph, color class `text-slate-600`, no tooltip body.
- All 5s → composite `30` (max).
- All 3s → composite `18` (min Apply boundary).
- Tooltip text contains all 6 sub-score axis labels in order.
- `fit_analysis = NULL` → tooltip omits Strength and Gap lines, sub-scores remain.
- `fit_analysis.strengths = []` → omit Strength line.
- `fit_analysis.gaps = []` → omit Gap line.
- Strength text > 120 chars → truncated with `…` suffix.
- `data-sort-score` attribute equals `classification_rank * 100 + sub_score_sum` (e.g., `427` for Apply with sum 27, `122` for Reject with sum 22).
- Freshness star renders when `first_seen >= freshness_cutoff`.
- OOB swap: rescore route returns updated `_score_cell.html` partial; `data-sort-score` recomputes; existing OOB swap tests still pass.

No DB tests change. No sort-logic tests change. No fixture changes.

## Out of Scope (Explicit YAGNI)

- Color intensity gradient within class
- Score column header rename to "Fit"
- Adding composite to DB schema as stored column
- Flat sort by composite (ignoring classification floor)
- Sub-score breakdown in compact row (stays in expanded view only)
- Per-axis user-configurable weights
- Tooltip JS framework or libraries
- Native HTML `title` fallback path

## Risks

- **Tooltip extra DOM per row.** Each compact row now carries one extra hidden `<div>` for the tooltip. Job board renders ~50–200 rows; ~200 extra divs is negligible for browsers. No paginated rendering implications.
- **Tailwind `group-hover` keyboard accessibility.** CSS-only tooltips trigger on mouse hover only; keyboard-focus-driven tooltip is a future improvement. Acceptable for single-user local app.
- **Color palette accessibility.** Existing badge palette (green/amber/slate/red on dark theme) carries forward unchanged; no new contrast risk.
- **Rescore OOB swap correctness.** `data-sort-score` packed-key change requires the rescore route's score-cell render to use the new partial. Tests cover this; rescore is the only OOB-update path for score cell.
