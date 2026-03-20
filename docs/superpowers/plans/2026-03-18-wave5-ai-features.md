# Wave 5: AI-Powered Features Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two AI features: (A) Guidelines import with diff preview in settings, (B) LLM-driven profile recommendations with one-click fixes.

**Architecture:** Feature A: new settings routes for preview/apply with Sonnet merge. Feature B: new profile routes for per-warning and batch recommendations via Haiku, with structured action schema for one-click apply.

**Tech Stack:** Flask, Jinja2, HTMX, Anthropic API (Haiku + Sonnet), JSON schema

**Spec:** `docs/superpowers/specs/2026-03-18-wave5-ai-features-design.md`

---

## Chunk 1: Feature A — Guidelines Import

### Task 1: Extract shared merge helper from resume_style_guide.py

**Files:**
- Modify: `job_finder/web/resume_style_guide.py:218-292`

- [ ] **Step 1: Write test for the merge helper**

```python
def test_merge_guidelines_into_guide(monkeypatch):
    """_merge_guidelines_into_guide should call Sonnet and return merged guide."""
    from unittest.mock import Mock, patch
    from job_finder.web.resume_style_guide import _merge_guidelines_into_guide

    mock_result = {"bullet_style": "dashes", "verb_tense": "past"}
    with patch("job_finder.web.resume_style_guide.call_claude", return_value=(mock_result, 0.01)):
        result = _merge_guidelines_into_guide(
            guidelines_text="Use dashes for bullets.",
            existing_guide={"bullet_style": "circles"},
            client=Mock(),
            model="test-model",
            conn=None,
            config={},
            mode="merge_updates",
        )
    assert result is not None
    assert result["bullet_style"] == "dashes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ -k "merge_guidelines_into_guide" -v`

Expected: FAIL (function doesn't exist yet)

- [ ] **Step 3: Extract the helper**

In `resume_style_guide.py`, add a new function `_merge_guidelines_into_guide` that encapsulates the shared logic from `migrate_style_guide`. The key difference is the `mode` parameter:

```python
def _merge_guidelines_into_guide(
    guidelines_text: str,
    existing_guide: dict,
    client,
    model: str,
    conn,
    config: dict,
    mode: str = "populate_new",
) -> dict | None:
    """Merge guidelines text into an existing style guide via Sonnet.

    Args:
        guidelines_text: Raw markdown text of resume generation guidelines.
        existing_guide: Current style guide dict.
        client: Anthropic client instance.
        model: Model name string.
        conn: SQLite connection for cost recording (may be None).
        config: Application config dict.
        mode: "populate_new" (only fill empty fields) or "merge_updates"
              (update fields where guidelines provide new/different guidance).

    Returns:
        Merged style guide dict, or None on error.
    """
    if mode == "populate_new":
        instruction = (
            "PRESERVE all existing field values exactly as they are. "
            "Only populate new fields that are currently missing or empty."
        )
    else:
        instruction = (
            "UPDATE fields where the new guidelines provide different or improved guidance. "
            "PRESERVE fields that the new guidelines don't address. "
            "When in conflict, prefer the new guidelines over existing values."
        )

    system = (
        "You are a resume style analyst. You have a resume generation guidelines document "
        "and an existing style guide JSON. Your task is to MERGE the guidelines into the "
        f"style guide. {instruction} Return the complete merged style guide."
    )

    user_message = (
        f"## Resume Generation Guidelines\n\n"
        f"{guidelines_text}\n\n"
        f"---\n\n"
        f"## Existing Style Guide\n\n"
        f"```json\n{json.dumps(existing_guide, indent=2)}\n```\n\n"
        f"Merge the guidelines into the style guide."
    )

    try:
        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=STYLE_GUIDE_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="style_guide_migration",
            config=config,
            max_tokens=2048,
        )
        return result
    except Exception as e:
        logger.warning("_merge_guidelines_into_guide: failed: %s", e)
        return None
```

Then refactor `migrate_style_guide` to call this helper instead of duplicating the logic.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ -k "merge_guidelines_into_guide" -v`

Expected: PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/resume_style_guide.py tests/
git commit -m "refactor: extract _merge_guidelines_into_guide helper for reuse"
```

### Task 2: Add preview and apply routes

**Files:**
- Modify: `job_finder/web/blueprints/settings.py`

- [ ] **Step 1: Add preview-guidelines-merge route**

```python
@settings_bp.route("/preview-guidelines-merge", methods=["POST"], strict_slashes=False)
def preview_guidelines_merge():
    """HTMX POST — preview what would change if guidelines are merged."""
    guidelines_text = request.form.get("guidelines_text", "").strip()
    if not guidelines_text:
        return "<div class='text-red-400 text-sm'>No guidelines text provided.</div>"

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    config = current_app.config.get("JF_CONFIG", {})

    existing = load_style_guide()
    merged = _merge_guidelines_into_guide(
        guidelines_text, existing, anthropic.Anthropic(),
        config.get("scoring", {}).get("models", {}).get("sonnet", DEFAULT_MODEL_SONNET),
        conn, config, mode="merge_updates",
    )

    if merged is None:
        return "<div class='text-red-400 text-sm'>Merge failed. Check logs.</div>"

    # Build diff view: field-by-field comparison
    diff_html = _build_diff_html(existing, merged)

    # Stash merged result in hidden field for apply without re-calling Sonnet
    import json
    stash = f'<input type="hidden" name="merged_guide" value="{json.dumps(merged).replace(chr(34), "&quot;")}">'

    return diff_html + stash + '''
        <button type="button"
                hx-post="/settings/apply-guidelines-merge"
                hx-include="[name='merged_guide'],[name='guidelines_text']"
                hx-target="#guidelines-preview"
                hx-swap="innerHTML"
                class="mt-3 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm rounded">
            Apply Changes
        </button>'''
```

- [ ] **Step 2: Add apply-guidelines-merge route**

```python
@settings_bp.route("/apply-guidelines-merge", methods=["POST"], strict_slashes=False)
def apply_guidelines_merge():
    """HTMX POST — apply the previewed merge result."""
    import json
    from pathlib import Path

    merged_json = request.form.get("merged_guide", "")
    guidelines_text = request.form.get("guidelines_text", "")

    try:
        merged = json.loads(merged_json)
    except (json.JSONDecodeError, TypeError):
        return "<div class='text-red-400 text-sm'>Invalid merge data. Please re-preview.</div>"

    # Save merged style guide
    save_style_guide(merged)

    # Save updated guidelines file
    if guidelines_text.strip():
        guidelines_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "resume_generation_guidelines.md"
        guidelines_path.write_text(guidelines_text, encoding="utf-8")

    return "<div class='text-emerald-400 text-sm'>Guidelines merged and saved successfully.</div>"
```

- [ ] **Step 3: Add `_build_diff_html` helper**

```python
def _build_diff_html(old: dict, new: dict) -> str:
    """Build an HTML diff view comparing old and new style guide values."""
    from job_finder.web.resume_style_guide import _FIELD_LABELS
    import json

    rows = []
    for field, label in _FIELD_LABELS.items():
        old_val = old.get(field)
        new_val = new.get(field)

        old_str = json.dumps(old_val, indent=2) if isinstance(old_val, (dict, list)) else str(old_val or "(empty)")
        new_str = json.dumps(new_val, indent=2) if isinstance(new_val, (dict, list)) else str(new_val or "(empty)")

        if old_str == new_str:
            rows.append(f'<div class="text-xs text-slate-600 py-1">{label}: <span class="text-slate-500">No change</span></div>')
        else:
            rows.append(f'''<div class="py-2 border-b border-slate-700">
                <div class="text-xs text-slate-300 font-semibold mb-1">{label}</div>
                <div class="text-xs" style="color: #f87171; text-decoration: line-through; max-height: 60px; overflow: hidden;">{old_str[:200]}</div>
                <div class="text-xs" style="color: #4ade80; max-height: 60px; overflow: hidden;">{new_str[:200]}</div>
            </div>''')

    return '<div class="space-y-1 max-h-96 overflow-y-auto">' + ''.join(rows) + '</div>'
```

- [ ] **Step 4: Add necessary imports at top of settings.py**

```python
from job_finder.web.resume_style_guide import load_style_guide, save_style_guide, migrate_style_guide, _merge_guidelines_into_guide, _FIELD_LABELS
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/blueprints/settings.py
git commit -m "feat: add guidelines preview and apply routes with diff view"
```

### Task 3: Add guidelines textarea UI to settings template

**Files:**
- Modify: `job_finder/web/templates/settings/index.html`

- [ ] **Step 1: Add the guidelines import section**

Below the existing "Migrate Style Guide" section in `settings/index.html`, add:

```html
    <!-- Update Guidelines -->
    <div class="border-t border-slate-700 pt-4 mt-4">
      <h3 class="text-sm font-semibold text-slate-300 mb-2">Update Resume Generation Guidelines</h3>
      <p class="text-xs text-slate-500 mb-3">
        Paste updated guidelines text below. Preview shows what would change in the style guide before applying.
      </p>
      <textarea name="guidelines_text" rows="10"
                class="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-xs text-slate-100 focus:border-violet-500 focus:outline-none resize-y font-mono"
                placeholder="Paste your updated resume generation guidelines here...">{{ guidelines_text }}</textarea>
      <div class="flex gap-2 mt-2">
        <button type="button"
                hx-post="/settings/preview-guidelines-merge"
                hx-include="[name='guidelines_text']"
                hx-target="#guidelines-preview"
                hx-swap="innerHTML"
                hx-disable-elt="this"
                class="px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white text-sm rounded transition-colors disabled:opacity-50">
          Preview Changes
          <span class="htmx-indicator ml-1 text-xs">Analyzing...</span>
        </button>
      </div>
      <div id="guidelines-preview" class="mt-3"></div>
    </div>
```

The `{{ guidelines_text }}` variable needs to be passed from the `index()` route — read the current guidelines file and pass it.

- [ ] **Step 2: Update settings index() route to pass guidelines_text**

In `settings.py`, in the `index()` route, add:

```python
    from pathlib import Path
    guidelines_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "resume_generation_guidelines.md"
    guidelines_text = ""
    try:
        guidelines_text = guidelines_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
```

And pass `guidelines_text=guidelines_text` to the template render call.

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/settings/index.html job_finder/web/blueprints/settings.py
git commit -m "feat: add guidelines import UI with textarea and preview button"
```

## Chunk 2: Feature B — LLM Profile Recommendations

### Task 4: Add recommendation routes to profile blueprint

**Files:**
- Modify: `job_finder/web/blueprints/profile.py`

- [ ] **Step 1: Add the single-warning recommendation route**

```python
@profile_bp.route("/recommendation", strict_slashes=False)
def recommendation():
    """HTMX GET — get AI recommendation for a single validation warning."""
    field = request.args.get("field", "")
    message = request.args.get("message", "")

    if not field or not message:
        return "<div class='text-red-400 text-xs'>Missing field or message.</div>"

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    config = current_app.config.get("JF_CONFIG", {})

    # Load profile for context
    profile = _load_profile()

    result = _get_recommendation(field, message, profile, conn, config)

    return render_template(
        "profile/_recommendation.html",
        rec=result,
        field=field,
    )
```

- [ ] **Step 2: Add batch recommendations route**

```python
@profile_bp.route("/recommendations-all", methods=["POST"], strict_slashes=False)
def recommendations_all():
    """HTMX POST — get AI recommendations for all validation warnings at once."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    config = current_app.config.get("JF_CONFIG", {})

    profile = _load_profile()
    warnings = _validate_profile(profile)

    results = _get_all_recommendations(warnings, profile, conn, config)

    return render_template(
        "profile/_recommendations_all.html",
        recommendations=results,
    )
```

- [ ] **Step 3: Add apply-fix route**

```python
_SAFE_ACTION_TYPES = {"add_skill", "update_field"}
_SAFE_UPDATE_FIELDS = {"summary", "skills", "target_titles", "min_salary"}

@profile_bp.route("/apply-fix", methods=["POST"], strict_slashes=False)
def apply_fix():
    """HTMX POST — apply a structured fix action to the profile."""
    action_type = request.form.get("action_type", "")
    field = request.form.get("field", "")
    value = request.form.get("value", "")

    if action_type not in _SAFE_ACTION_TYPES:
        return "<div class='text-red-400 text-xs'>Invalid action type.</div>"

    if action_type == "update_field" and field not in _SAFE_UPDATE_FIELDS:
        return "<div class='text-red-400 text-xs'>Field not editable via quick fix.</div>"

    profile = _load_profile()

    if action_type == "add_skill":
        skills = profile.get("skills", [])
        if value not in skills:
            skills.append(value)
            profile["skills"] = skills
    elif action_type == "update_field":
        profile[field] = value

    _save_profile(profile)

    # Re-validate and return updated warnings panel
    warnings = _validate_profile(profile)
    return render_template("profile/_warnings_panel.html", warnings=warnings)
```

- [ ] **Step 4: Add the `_get_recommendation` and `_get_all_recommendations` helpers**

These call Haiku with the warning context and profile section to generate guidance + structured actions. Use `call_claude` with `purpose="profile_recommendation"` and a JSON output schema.

```python
_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "guidance": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["add_skill", "update_field"]},
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["type", "value"],
            },
        },
    },
    "required": ["guidance", "actions"],
}
```

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/blueprints/profile.py
git commit -m "feat: add profile recommendation routes with Haiku AI guidance"
```

### Task 5: Create recommendation templates

**Files:**
- Create: `job_finder/web/templates/profile/_recommendation.html`
- Create: `job_finder/web/templates/profile/_recommendations_all.html`

- [ ] **Step 1: Create single recommendation template**

```html
{# Single recommendation for one validation warning #}
<div class="mt-2 pl-4 border-l-2 border-violet-700/50">
  <p class="text-xs text-slate-300 mb-2">{{ rec.guidance }}</p>
  {% if rec.actions %}
  <div class="flex flex-wrap gap-2">
    {% for action in rec.actions %}
    <button type="button"
            hx-post="/profile/apply-fix"
            hx-vals='{"action_type": "{{ action.type }}", "field": "{{ action.field | default("") }}", "value": "{{ action.value }}"}'
            hx-target="#warnings-panel"
            hx-swap="innerHTML"
            hx-disable-elt="this"
            class="px-2 py-1 bg-violet-600 hover:bg-violet-500 text-white text-xs rounded transition-colors disabled:opacity-50">
      {% if action.type == "add_skill" %}Add "{{ action.value }}"
      {% elif action.type == "update_field" %}Update {{ action.field }}
      {% endif %}
    </button>
    {% endfor %}
  </div>
  {% endif %}
</div>
```

- [ ] **Step 2: Create batch recommendations template**

This template uses OOB swaps to populate per-warning recommendation slots.

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/templates/profile/_recommendation.html job_finder/web/templates/profile/_recommendations_all.html
git commit -m "feat: add recommendation templates with action buttons"
```

### Task 6: Update profile index.html with recommendation UI

**Files:**
- Modify: `job_finder/web/templates/profile/index.html:38-68`

- [ ] **Step 1: Add per-warning expand buttons and recommendation containers**

Update each warning `<li>` to include a "How to fix?" button and a recommendation container:

```html
      {% for w in warnings %}
      <li class="flex flex-col gap-1">
        <div class="flex items-start gap-2 text-sm">
          <span class="text-amber-500 mt-0.5">&#8226;</span>
          <div class="flex-1">
            <span class="text-amber-200">{{ w.message }}</span>
            <span class="text-amber-600 text-xs ml-2">{{ w.field }}</span>
          </div>
          <button type="button"
                  hx-get="/profile/recommendation?field={{ w.field | urlencode }}&message={{ w.message | urlencode }}"
                  hx-target="#rec-{{ loop.index }}"
                  hx-swap="innerHTML"
                  hx-disable-elt="this"
                  class="px-2 py-0.5 text-xs text-violet-400 hover:text-violet-300 border border-violet-700/50 rounded transition-colors disabled:opacity-50 flex-shrink-0">
            How to fix?
            <span class="htmx-indicator ml-1">...</span>
          </button>
        </div>
        <div id="rec-{{ loop.index }}"></div>
      </li>
      {% endfor %}
```

- [ ] **Step 2: Add batch recommendation button below warnings list**

```html
    <div class="mt-3 pt-3 border-t border-amber-800/30">
      <button type="button"
              hx-post="/profile/recommendations-all"
              hx-target="#warnings-panel"
              hx-swap="innerHTML"
              hx-disable-elt="this"
              class="px-3 py-1.5 text-xs bg-violet-600 hover:bg-violet-500 text-white rounded transition-colors disabled:opacity-50">
        Get all recommendations
        <span class="htmx-indicator ml-1">Analyzing...</span>
      </button>
    </div>
```

- [ ] **Step 3: Wrap warnings in a panel div for OOB targeting**

Add `id="warnings-panel"` to the warnings container div so `apply-fix` can swap the whole panel.

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/templates/profile/index.html
git commit -m "feat: add recommendation UI to profile warnings panel"
```
