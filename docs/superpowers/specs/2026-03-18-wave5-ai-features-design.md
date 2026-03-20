# Wave 5: AI-Powered Features

Two features that use Claude to make existing UI more actionable.

## Feature A: Guidelines Import via Settings UI

### Problem

The resume generation guidelines (`docs/resume_generation_guidelines.md`) can only be updated by editing the file on disk and clicking "Migrate Style Guide" in settings. The user wants to paste updated guidelines text directly in the UI and preview what will change before saving.

### Design

**New UI in settings page:** Below the existing "Migrate Style Guide" section, add a "Update Guidelines" section with:

1. **Textarea** for pasting updated guidelines text (pre-populated with current `resume_generation_guidelines.md` content)
2. **"Preview Changes" button** — sends the text to a new route that calls Sonnet to merge with the existing style guide, returns a diff/comparison view
3. **Diff view** — shows current vs proposed value for each style guide field that would change, with changed fields highlighted
4. **"Apply Changes" button** — saves the merged guide to `resume_style_guide.json` and updates `resume_generation_guidelines.md` on disk

### New Routes

**File:** `job_finder/web/blueprints/settings.py`

```
POST /settings/preview-guidelines-merge
```
- Accepts: `guidelines_text` from textarea
- Calls Sonnet with existing style guide + new guidelines text (same merge logic as `migrate_style_guide`, but returns result without saving)
- Returns: HTMX fragment showing before/after diff for each changed field

```
POST /settings/apply-guidelines-merge
```
- Accepts: `guidelines_text` (same text that was previewed)
- Runs the merge again and saves both:
  - `resume_style_guide.json` — merged style guide
  - `docs/resume_generation_guidelines.md` — updated guidelines text (so the file stays in sync)
- Returns: success confirmation fragment

### Merge Logic

Reuse the existing `migrate_style_guide` pattern but parameterize the guidelines text source (currently hardcoded to read from file). Extract a shared `_merge_guidelines_into_guide(guidelines_text, existing_guide, client, model, conn, config)` helper that both `migrate_style_guide` and the new routes can call.

**Important merge semantics difference:** The existing `migrate_style_guide` prompt says "Only populate new fields that are currently missing or empty." The guidelines-import feature needs different behavior — the user is uploading *updated* guidelines and expects changed fields to be overwritten. The extracted helper must accept a `mode` parameter or separate system prompt: `"populate_new"` (existing behavior) vs `"merge_updates"` (new behavior: update fields where the new guidelines provide different/improved guidance, preserve fields the new guidelines don't address).

**Cost optimization:** The preview route calls Sonnet to produce the merged result. Rather than calling Sonnet again on apply, stash the preview result (e.g., in a hidden form field as JSON) and apply it directly on confirm. This avoids paying for two identical Sonnet calls.

### Diff View

Simple field-by-field comparison. For each field in STYLE_GUIDE_SCHEMA:
- If value unchanged: show field name in grey, "No change"
- If value changed: show field name, old value (red/strikethrough), new value (green)
- If new field added: show field name, "(empty)" → new value (green)

Use inline styles (per project convention for structural CSS with Tailwind v4 CDN).

### Files Modified

| File | Change |
|------|--------|
| `settings.py` (blueprint) | Add `preview_guidelines_merge` and `apply_guidelines_merge` routes |
| `resume_style_guide.py` | Extract `_merge_guidelines_into_guide` helper from `migrate_style_guide` |
| `settings/index.html` | Add guidelines textarea, preview button, diff container, apply button |

---

## Feature B: LLM-Driven Profile Recommendations

### Problem

Profile validation warnings show what's wrong but not how to fix it. Users need actionable guidance and, where possible, one-click fixes.

### Design

**Per-warning expand:** Each warning `<li>` gets a small "How to fix?" button. Clicking it fires an HTMX GET to fetch Claude's recommendation for that specific warning. The recommendation expands inline below the warning text.

**Batch button:** Below the warnings list, a "Get all recommendations" button that calls Claude once with all warnings, returning recommendations for each. Results populate the per-warning expand slots.

**Recommendation format:** Each recommendation includes:
- **Guidance text** — human-readable explanation of what to fix and why
- **Structured actions** (v1, limited set) — machine-readable fix operations that can be applied with one click:
  - `add_skill` — add a skill string to the skills array
  - `update_field` — set a specific profile field to a new value
- **Fallback** — for complex fixes that can't be expressed as structured actions, show text guidance only

### New Routes

**File:** `job_finder/web/blueprints/profile.py`

```
GET /profile/recommendation?field=<field>&message=<message>
```
- Single-warning recommendation
- Calls Haiku (cheaper than Sonnet — these are simple recommendations) with the warning context + relevant profile section
- Returns HTMX fragment with guidance text + optional action buttons

```
POST /profile/recommendations-all
```
- Batch recommendation for all current warnings
- Sends all warnings + full profile to Haiku in one call
- Returns HTMX fragment with all recommendations, each in an expandable slot matching the warning `<li>` structure

```
POST /profile/apply-fix
```
- Accepts: `action_type` (add_skill, update_field), `field`, `value`
- Validates the action against an allowlist of safe fields
- Reads profile JSON, applies the fix, saves
- Returns updated warning panel (re-validates after fix)

### LLM Output Schema

```json
{
  "recommendations": [
    {
      "field": "skills",
      "guidance": "Your skills list is missing Python, which appears in your experience bullets.",
      "actions": [
        {"type": "add_skill", "value": "Python"}
      ]
    },
    {
      "field": "summary",
      "guidance": "Summary exceeds 4 sentences. Consider trimming to 3 sentences focused on your core value proposition.",
      "actions": []
    }
  ]
}
```

Actions array is empty when the fix requires human judgment (text guidance only).

### Safety

- `apply-fix` route validates `action_type` against `{"add_skill", "update_field"}` allowlist
- `update_field` validates `field` against a list of safe editable fields (no arbitrary JSON path injection)
- Profile JSON is backed up before any modification (same pattern as existing profile save)
- After applying a fix, re-run validation to update warning panel (fix may resolve one warning but surface another)

### UI Layout

Each warning `<li>` becomes:

```
• [warning message]  [field badge]  [How to fix? button]
  └─ [recommendation panel, initially hidden]
     [guidance text]
     [action buttons if available]
```

The recommendation panel slides open below the warning when loaded. Uses HTMX `hx-get` with `hx-target` pointing to a per-warning container div.

### Files Modified

| File | Change |
|------|--------|
| `profile.py` (blueprint) | Add `recommendation`, `recommendations_all`, `apply_fix` routes |
| `profile/index.html` | Add per-warning expand buttons, recommendation containers, batch button |
| New template: `profile/_recommendation.html` | Single recommendation fragment with guidance + action buttons |
| New template: `profile/_recommendations_all.html` | Batch recommendations fragment |

---

## Testing (both features)

### Feature A
- Paste modified guidelines text, click Preview — diff view shows changes
- Click Apply — `resume_style_guide.json` updated, `resume_generation_guidelines.md` updated
- Re-open settings — style guide reflects merged values
- Empty textarea → appropriate error message

### Feature B
- Profile page with warnings → each warning shows "How to fix?" button
- Click single "How to fix?" → recommendation loads inline
- Click "Get all recommendations" → all warnings get recommendations
- Click "Add skill" action → skill added to profile, warning panel refreshes
- Profile with no warnings → no recommendation buttons shown
- `pytest tests/` for regression
