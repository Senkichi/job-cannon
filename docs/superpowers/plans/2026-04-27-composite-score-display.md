# Composite Score Display Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the classification badge in the job board's Score column with a colored composite number (sum of 6 sub-scores, range 6–30) plus a Tailwind hover-tooltip showing sub-scores + top strength + top gap.

**Architecture:** Pure presentation-layer change. No DB schema, no migrations, no SQL changes, no Python logic outside the template. The composite is derived inline in `_score_cell.html` from the already-parsed `sub_scores_json` and `fit_analysis` JSON columns. Sort logic in `db.py` is unchanged — `data-sort-score` carries a packed key (`classification_rank * 100 + sub_score_sum`) so the existing single-key client-side resort in `index.html` continues to work.

**Tech Stack:** Jinja2, Tailwind CSS (CDN, no build step), Flask, pytest with Flask test client.

**Spec:** `docs/superpowers/specs/2026-04-27-composite-score-display-design.md`

---

## File Structure

### Modified files

| File | Lines | Responsibility |
|---|---|---|
| `job_finder/web/templates/jobs/_score_cell.html` | ~44 → ~70 | Render composite number, classification color, tooltip; preserve OOB target id, freshness star, packed sort-score |
| `tests/test_views.py` | append ~150 | Test cases for new score-cell rendering, tooltip, packed sort key, NULL handling |

### Files explicitly NOT touched (regression surface)

- `job_finder/web/templates/jobs/_row_expanded.html` — Fit Breakdown bars stay
- `job_finder/web/templates/jobs/_row_detail.html` — Detail page stays
- `job_finder/web/templates/jobs/index.html` — `resortByScore()` JS works unchanged once template writes packed key
- `job_finder/web/blueprints/jobs.py` — `fit_analysis` already in `JOBS_ALL_COLUMNS` row context; no change needed
- `job_finder/db.py` — `_classification_score_order`, `_SUB_SCORE_SUM_SQL` unchanged

### Reference files (read-only context for implementer)

- `job_finder/db.py:530-575` — existing classification rank + sub_score_sum sort logic
- `job_finder/web/templates/jobs/_row_expanded.html:171-200` — existing sub-score bar pattern (same axis order, same labels)
- `job_finder/web/blueprints/jobs.py:440-451, 559, 563-571` — three call sites that render `_score_cell.html` (rescore OOB, paste-jd OOB, score-cell GET endpoint)

---

## Test Strategy

Tests use Flask test client + HTML assertions (existing pattern in `test_views.py`). Each task is a TDD cycle:

1. Write the failing test
2. Run to confirm it fails
3. Implement the minimal template change
4. Run to confirm it passes
5. Commit

Tests live in `tests/test_views.py` inside a new class `TestCompositeScoreCell` (added near `TestUXPolish` around line 1866). Reusing existing fixtures where possible; adding one new fixture for varied scoring states.

**Test command (project standard):**
```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```

---

## Task 1: Add fixture for varied scoring states

**Files:**
- Modify: `tests/test_views.py` — append fixture near other app fixtures (around line 1855, after `app_with_jd_full_job`)

**Why this fixture:** Existing fixtures cover one or two classifications. To verify all four colors + NULL + composite math + packed sort key in clean isolation, we need a fixture with one row per state.

- [ ] **Step 1: Write the fixture**

Append after `app_with_jd_full_job` (around line 1855 in `tests/test_views.py`):

```python
@pytest.fixture
def app_with_scored_jobs(tmp_db_path, app_factory):
    """App with one job per scoring state for score-cell rendering tests.

    Each row exercises a distinct (classification, sub_scores) combination so
    tests can assert color/value/tooltip rendering per state without ambiguity.
    """
    import sqlite3

    app = app_factory(tmp_db_path)

    rows = [
        # (dedup_key, title, classification, sub_scores_json, fit_analysis)
        # Apply, max sum 30 (all 5s)
        ("ax|max-apply|remote", "Max Apply Role", "apply",
         '{"title_fit": 5, "location_fit": 5, "comp_fit": 5, "domain_match": 5, "seniority_match": 5, "skills_match": 5}',
         '{"strengths": ["Deep platform experience aligns with infra-heavy stack"], "gaps": ["No published Kubernetes operator work"], "talking_points": [], "resume_priority_skills": []}'),
        # Apply, min sum 18 (all 3s)
        ("ax|min-apply|remote", "Min Apply Role", "apply",
         '{"title_fit": 3, "location_fit": 3, "comp_fit": 3, "domain_match": 3, "seniority_match": 3, "skills_match": 3}',
         '{"strengths": ["Adequate match"], "gaps": ["Borderline fit"], "talking_points": [], "resume_priority_skills": []}'),
        # Consider, sum 22
        ("ax|consider-22|remote", "Consider Role", "consider",
         '{"title_fit": 4, "location_fit": 3, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 3}',
         '{"strengths": ["Strong technical match"], "gaps": ["Comp below target"], "talking_points": [], "resume_priority_skills": []}'),
        # Skip, sum 22 (one axis at 2)
        ("ax|skip-22|remote", "Skip Role", "skip",
         '{"title_fit": 5, "location_fit": 5, "comp_fit": 2, "domain_match": 5, "seniority_match": 5, "skills_match": 0}',
         '{"strengths": [], "gaps": ["Compensation well below market"], "talking_points": [], "resume_priority_skills": []}'),
        # Reject, sum 6 (all 1s would auto-reject; legitimacy_note also rejects)
        ("ax|reject-6|remote", "Reject Role", "reject",
         '{"title_fit": 1, "location_fit": 1, "comp_fit": 1, "domain_match": 1, "seniority_match": 1, "skills_match": 1}',
         '{"strengths": [], "gaps": ["Multiple critical mismatches"], "talking_points": [], "resume_priority_skills": []}'),
        # Unscored: classification + sub_scores_json + fit_analysis all NULL
        ("ax|unscored|remote", "Unscored Role", None, None, None),
        # Apply with no fit_analysis (rationale-missing edge case)
        ("ax|apply-no-rationale|remote", "Apply No Rationale", "apply",
         '{"title_fit": 5, "location_fit": 4, "comp_fit": 5, "domain_match": 4, "seniority_match": 5, "skills_match": 4}',
         None),
        # Apply with empty strengths/gaps lists
        ("ax|apply-empty-lists|remote", "Apply Empty Lists", "apply",
         '{"title_fit": 4, "location_fit": 4, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
         '{"strengths": [], "gaps": [], "talking_points": [], "resume_priority_skills": []}'),
        # Apply with very long strength (truncation test)
        ("ax|apply-long-strength|remote", "Apply Long Strength", "apply",
         '{"title_fit": 4, "location_fit": 4, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
         '{"strengths": ["' + ("A" * 200) + '"], "gaps": ["' + ("B" * 200) + '"], "talking_points": [], "resume_priority_skills": []}'),
    ]

    conn = sqlite3.connect(tmp_db_path)
    for (dk, title, cls, subs, fit) in rows:
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 source_id, salary_min, salary_max, description,
                 first_seen, last_seen, score, score_breakdown, pipeline_status,
                 classification, sub_scores_json, fit_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dk, title, "TestCo", "Remote",
             '["test"]', '["https://test/jobs/1"]', dk,
             100000, 200000, "desc",
             "2026-04-27T10:00:00", "2026-04-27T10:00:00",
             0.0, "{}", "discovered",
             cls, subs, fit),
        )
    conn.commit()
    conn.close()

    return app


@pytest.fixture
def scored_client(app_with_scored_jobs):
    return app_with_scored_jobs.test_client()
```

- [ ] **Step 2: Verify fixture creates without errors**

Run a sanity smoke test:
```bash
uv run --active pytest tests/test_views.py -q --tb=short -k "scored_client" 2>&1 | head -30
```
Expected: tests using `scored_client` may not exist yet; should report "no tests ran" or pass collection without errors. If `app_factory` or `tmp_db_path` fixture names differ in this codebase, adjust the fixture signature to match. Run `grep -n "^def app_with\|^@pytest.fixture" tests/test_views.py | head` to confirm fixture names if needed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_views.py
git commit -m "test: add scored-jobs fixture for score-cell rendering tests"
```

---

## Task 2: Composite number rendering (drop badge, add color)

**Files:**
- Test: `tests/test_views.py` — new class `TestCompositeScoreCell`
- Modify: `job_finder/web/templates/jobs/_score_cell.html`

- [ ] **Step 1: Write failing tests for composite number + color**

Append to `tests/test_views.py`:

```python
class TestCompositeScoreCell:
    """v3.0 composite-score display: number + color + tooltip + packed sort key.

    Replaces the classification-badge rendering with a colored composite number
    (sum of 6 sub-scores, range 6-30). See spec
    docs/superpowers/specs/2026-04-27-composite-score-display-design.md.
    """

    def test_apply_row_renders_composite_30(self, scored_client):
        """Apply with all 5s renders composite '30'."""
        response = scored_client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        # Locate the score cell for the max-apply job
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        assert idx != -1, "score cell for max-apply not rendered"
        # Slice forward enough to capture the cell content
        cell = html[idx:idx + 800]
        assert ">30<" in cell, f"composite '30' not rendered in cell: {cell[:300]}"

    def test_apply_row_uses_green_color(self, scored_client):
        """Apply classification renders with text-green-400."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 800]
        assert "text-green-400" in cell

    def test_consider_row_uses_amber_color(self, scored_client):
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx:idx + 800]
        assert "text-amber-400" in cell
        assert ">22<" in cell

    def test_skip_row_uses_slate_color(self, scored_client):
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cskip-22%7Cremote"')
        cell = html[idx:idx + 800]
        assert "text-slate-400" in cell

    def test_reject_row_uses_red_color(self, scored_client):
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Creject-6%7Cremote"')
        cell = html[idx:idx + 800]
        assert "text-red-400" in cell
        assert ">6<" in cell

    def test_apply_min_renders_composite_18(self, scored_client):
        """Apply with all 3s (boundary) renders '18'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmin-apply%7Cremote"')
        cell = html[idx:idx + 800]
        assert ">18<" in cell

    def test_badge_text_no_longer_present(self, scored_client):
        """Apply/Consider/Skip/Reject text labels are removed from compact row."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        # Check within score cell scope only (not status cell which is unrelated)
        # The score cell uses id="score-..."; locate each and ensure the words
        # 'Apply', 'Consider', 'Skip', 'Reject' don't appear as visible text
        # within the cell. Simpler check: the badge classes from the OLD impl
        # (bg-green-900, bg-amber-900, bg-red-900 used in score cell) are gone.
        for old_badge in ('"bg-green-900', '"bg-amber-900', '"bg-red-900', '"bg-slate-800'):
            # NOTE: These classes may legitimately appear elsewhere on page.
            # Restrict to just the score cells.
            for dk in ('ax%7Cmax-apply%7Cremote', 'ax%7Cconsider-22%7Cremote',
                       'ax%7Cskip-22%7Cremote', 'ax%7Creject-6%7Cremote'):
                idx = html.find(f'id="score-{dk}"')
                cell = html[idx:idx + 800]
                assert old_badge not in cell, (
                    f"Old badge class {old_badge} still present in score cell {dk}"
                )
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```
Expected: All tests FAIL (composite numbers not rendered yet, old badge classes still present).

- [ ] **Step 3: Rewrite `_score_cell.html` (no tooltip yet — Task 4 adds it)**

Replace the entire contents of `job_finder/web/templates/jobs/_score_cell.html` with:

```jinja
{# Score cell partial. Renders the score column as a colored composite number.

   Variables expected in scope:
     job              — job object with classification (enum: apply/consider/skip/reject),
                        sub_scores_json, fit_analysis, dedup_key, first_seen
   Optional:
     oob              — when truthy, adds hx-swap-oob="outerHTML" for out-of-band targeting
     freshness_cutoff — ISO date string; jobs with first_seen >= this get a freshness star

   v3.0 (Phase 34) numeric badge → colored composite number.
   See docs/superpowers/specs/2026-04-27-composite-score-display-design.md
#}

{% set cls = job.classification %}
{% set sub_scores = job.sub_scores_json | from_json if job.sub_scores_json else {} %}
{% set is_fresh = job.first_seen is not none and freshness_cutoff is defined and job.first_seen >= freshness_cutoff %}

{# Composite = sum of 6 sub-scores (range 6-30). 0 if sub_scores missing. #}
{% set composite = (sub_scores.get('title_fit', 0) | int)
                 + (sub_scores.get('location_fit', 0) | int)
                 + (sub_scores.get('comp_fit', 0) | int)
                 + (sub_scores.get('domain_match', 0) | int)
                 + (sub_scores.get('seniority_match', 0) | int)
                 + (sub_scores.get('skills_match', 0) | int) %}

{% if cls == 'apply' %}
  {% set color_class = 'text-green-400' %}
  {% set rank = 4 %}
{% elif cls == 'consider' %}
  {% set color_class = 'text-amber-400' %}
  {% set rank = 3 %}
{% elif cls == 'skip' %}
  {% set color_class = 'text-slate-400' %}
  {% set rank = 2 %}
{% elif cls == 'reject' %}
  {% set color_class = 'text-red-400' %}
  {% set rank = 1 %}
{% else %}
  {% set color_class = 'text-slate-600' %}
  {% set rank = 0 %}
{% endif %}

{# Packed sort key: classification_rank * 100 + composite. Preserves single-key
   client-side resort in jobs/index.html resortByScore() while matching server
   two-key sort (classification rank DESC, sub_score_sum DESC). #}
{% set sort_score = rank * 100 + composite %}

<td id="score-{{ job.dedup_key | urlencode }}"
    class="px-1 py-1.5 whitespace-nowrap text-right"
    data-sort-score="{{ sort_score }}"
    {% if oob is defined and oob %}hx-swap-oob="outerHTML"{% endif %}>
  {% if cls %}
    <span class="font-semibold {{ color_class }}">{{ composite }}</span>{% if is_fresh %}<span class="text-amber-400 text-xs ml-0.5" title="Surfaced in last 3 days">*</span>{% endif %}
  {% else %}
    <span class="{{ color_class }}">&mdash;</span>
  {% endif %}
</td>
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```
Expected: All Task 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_views.py job_finder/web/templates/jobs/_score_cell.html
git commit -m "feat(score-cell): replace badge with colored composite number"
```

---

## Task 3: Packed `data-sort-score` key

**Files:**
- Test: `tests/test_views.py::TestCompositeScoreCell` (extend existing class)
- Modify: `job_finder/web/templates/jobs/_score_cell.html` (already correct from Task 2 — write tests to lock it in)

**Note:** Task 2's template already writes the packed key. This task adds explicit tests to prevent regression.

- [ ] **Step 1: Write failing tests for packed key**

Append to `TestCompositeScoreCell`:

```python
    def test_apply_max_packed_sort_score_is_430(self, scored_client):
        """Apply (rank 4) with sum 30 → data-sort-score='430'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="430"' in cell

    def test_apply_min_packed_sort_score_is_418(self, scored_client):
        """Apply (rank 4) with sum 18 → data-sort-score='418'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmin-apply%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="418"' in cell

    def test_consider_packed_sort_score_is_322(self, scored_client):
        """Consider (rank 3) with sum 22 → data-sort-score='322'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="322"' in cell

    def test_skip_packed_sort_score_is_222(self, scored_client):
        """Skip (rank 2) with sum 22 → data-sort-score='222'.
        Demonstrates why packing matters: a 22 Skip has the same sum as a 22
        Consider, but rank floor places it below."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cskip-22%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="222"' in cell

    def test_reject_packed_sort_score_is_106(self, scored_client):
        """Reject (rank 1) with sum 6 → data-sort-score='106'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Creject-6%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="106"' in cell

    def test_unscored_packed_sort_score_is_0(self, scored_client):
        """NULL classification → data-sort-score='0'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cunscored%7Cremote"')
        cell = html[idx:idx + 800]
        assert 'data-sort-score="0"' in cell
```

- [ ] **Step 2: Run tests to verify they pass**

The template already writes the packed key correctly from Task 2. Run:

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```
Expected: All Task 3 tests PASS without further code change. (If they fail, fix the template arithmetic.)

- [ ] **Step 3: Run existing OOB sort-score tests to verify no regression**

```bash
uv run --active pytest tests/test_views.py -q --tb=short -k "data_sort_score or oob_score_cell"
```
Expected: All existing tests still PASS. (`test_rescore_oob_score_cell_has_data_sort_score`, `test_paste_jd_oob_score_cell_has_data_sort_score`, `test_compact_row_score_has_id`.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_views.py
git commit -m "test(score-cell): lock packed data-sort-score key behavior"
```

---

## Task 4: Tailwind tooltip with sub-scores + strength + gap

**Files:**
- Test: `tests/test_views.py::TestCompositeScoreCell`
- Modify: `job_finder/web/templates/jobs/_score_cell.html`

- [ ] **Step 1: Write failing tooltip tests**

Append to `TestCompositeScoreCell`:

```python
    # --- Tooltip content ---

    def test_tooltip_contains_all_six_axis_labels(self, scored_client):
        """Tooltip lists all 6 sub-score axes for an Apply row."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 1500]
        for label in ("Title", "Location", "Comp", "Domain", "Seniority", "Skills"):
            assert label in cell, f"Axis label '{label}' missing from tooltip in {cell[:300]}"

    def test_tooltip_contains_axis_values(self, scored_client):
        """Tooltip shows numeric values for each axis."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx:idx + 1500]
        # consider-22: title 4, location 3, comp 4, domain 4, seniority 4, skills 3
        # Values appear adjacent to labels; loose check that '4' and '3' both appear in cell.
        assert "4" in cell and "3" in cell

    def test_tooltip_includes_top_strength(self, scored_client):
        """Tooltip surfaces fit_analysis.strengths[0]."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 1500]
        assert "Deep platform experience aligns with infra-heavy stack" in cell

    def test_tooltip_includes_top_gap(self, scored_client):
        """Tooltip surfaces fit_analysis.gaps[0]."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 1500]
        assert "No published Kubernetes operator work" in cell

    def test_tooltip_omits_strength_when_empty(self, scored_client):
        """fit_analysis.strengths == [] → no Strength: line."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-empty-lists%7Cremote"')
        cell = html[idx:idx + 1500]
        assert "Strength:" not in cell
        assert "Gap:" not in cell  # gaps also empty in this fixture row

    def test_tooltip_works_when_fit_analysis_missing(self, scored_client):
        """fit_analysis NULL → tooltip still renders sub-scores; no Strength/Gap lines."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-no-rationale%7Cremote"')
        cell = html[idx:idx + 1500]
        # Sub-score labels still present
        assert "Title" in cell and "Skills" in cell
        # No Strength/Gap lines
        assert "Strength:" not in cell
        assert "Gap:" not in cell

    def test_tooltip_truncates_long_strength(self, scored_client):
        """Strength text > 120 chars is truncated with ellipsis."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-long-strength%7Cremote"')
        cell = html[idx:idx + 2000]
        # Original strength is 200 'A's. Truncated should be 120 'A's + '…'.
        long_a = "A" * 200
        truncated = "A" * 120 + "…"
        assert long_a not in cell, "Untruncated long strength leaked into tooltip"
        assert truncated in cell, "Expected truncated strength with ellipsis"

    def test_tooltip_uses_group_hover_pattern(self, scored_client):
        """Tooltip markup uses Tailwind group-hover (CSS-only, no JS)."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx:idx + 1500]
        assert "group-hover" in cell

    def test_unscored_no_tooltip(self, scored_client):
        """NULL classification → no tooltip markup (or minimal placeholder)."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cunscored%7Cremote"')
        cell = html[idx:idx + 1500]
        # Sub-score labels should NOT appear for unscored row
        assert "Strength:" not in cell
        assert "Gap:" not in cell
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short -k "tooltip"
```
Expected: All tooltip tests FAIL (tooltip markup not yet present).

- [ ] **Step 3: Implement Tailwind tooltip in `_score_cell.html`**

Replace the entire `_score_cell.html` from Task 2 with this expanded version (adds tooltip markup):

```jinja
{# Score cell partial. Renders the score column as a colored composite number
   plus a Tailwind hover-tooltip showing sub-scores + top strength + top gap.

   Variables expected in scope:
     job              — job object with classification (enum), sub_scores_json,
                        fit_analysis, dedup_key, first_seen
   Optional:
     oob              — when truthy, adds hx-swap-oob="outerHTML"
     freshness_cutoff — ISO date string; jobs with first_seen >= this get a star

   v3.0 (Phase 34) numeric badge → colored composite number + tooltip.
   Spec: docs/superpowers/specs/2026-04-27-composite-score-display-design.md
#}

{% set cls = job.classification %}
{% set sub_scores = job.sub_scores_json | from_json if job.sub_scores_json else {} %}
{% set fit = job.fit_analysis | from_json if job.fit_analysis else {} %}
{% set is_fresh = job.first_seen is not none and freshness_cutoff is defined and job.first_seen >= freshness_cutoff %}

{# Composite = sum of 6 sub-scores (range 6-30). 0 if sub_scores missing. #}
{% set composite = (sub_scores.get('title_fit', 0) | int)
                 + (sub_scores.get('location_fit', 0) | int)
                 + (sub_scores.get('comp_fit', 0) | int)
                 + (sub_scores.get('domain_match', 0) | int)
                 + (sub_scores.get('seniority_match', 0) | int)
                 + (sub_scores.get('skills_match', 0) | int) %}

{% if cls == 'apply' %}
  {% set color_class = 'text-green-400' %}
  {% set rank = 4 %}
{% elif cls == 'consider' %}
  {% set color_class = 'text-amber-400' %}
  {% set rank = 3 %}
{% elif cls == 'skip' %}
  {% set color_class = 'text-slate-400' %}
  {% set rank = 2 %}
{% elif cls == 'reject' %}
  {% set color_class = 'text-red-400' %}
  {% set rank = 1 %}
{% else %}
  {% set color_class = 'text-slate-600' %}
  {% set rank = 0 %}
{% endif %}

{% set sort_score = rank * 100 + composite %}

{# Tooltip text fragments. Truncate strength/gap at 120 chars (spec). #}
{% set strengths = fit.get('strengths', []) if fit else [] %}
{% set gaps = fit.get('gaps', []) if fit else [] %}
{% set top_strength = strengths[0] if strengths else '' %}
{% set top_gap = gaps[0] if gaps else '' %}
{% if top_strength and top_strength | length > 120 %}
  {% set top_strength = top_strength[:120] ~ '…' %}
{% endif %}
{% if top_gap and top_gap | length > 120 %}
  {% set top_gap = top_gap[:120] ~ '…' %}
{% endif %}

<td id="score-{{ job.dedup_key | urlencode }}"
    class="px-1 py-1.5 whitespace-nowrap text-right relative group"
    data-sort-score="{{ sort_score }}"
    {% if oob is defined and oob %}hx-swap-oob="outerHTML"{% endif %}>
  {% if cls %}
    <span class="font-semibold {{ color_class }} cursor-help">{{ composite }}</span>{% if is_fresh %}<span class="text-amber-400 text-xs ml-0.5" title="Surfaced in last 3 days">*</span>{% endif %}
    {# Tooltip: hidden by default, revealed on parent <td>.group hover. #}
    <div class="invisible group-hover:visible opacity-0 group-hover:opacity-100 transition-opacity duration-100
                absolute z-30 left-full ml-2 top-1/2 -translate-y-1/2
                w-72 p-2 rounded border border-slate-700 bg-slate-900 shadow-lg
                text-xs text-slate-200 text-left whitespace-normal pointer-events-none">
      <div class="font-mono text-slate-300">
        Title {{ sub_scores.get('title_fit', 0) }} ·
        Location {{ sub_scores.get('location_fit', 0) }} ·
        Comp {{ sub_scores.get('comp_fit', 0) }} ·
        Domain {{ sub_scores.get('domain_match', 0) }} ·
        Seniority {{ sub_scores.get('seniority_match', 0) }} ·
        Skills {{ sub_scores.get('skills_match', 0) }}
      </div>
      {% if top_strength %}
        <div class="mt-1"><span class="text-green-400 font-semibold">Strength:</span> {{ top_strength }}</div>
      {% endif %}
      {% if top_gap %}
        <div class="mt-1"><span class="text-red-400 font-semibold">Gap:</span> {{ top_gap }}</div>
      {% endif %}
    </div>
  {% else %}
    <span class="{{ color_class }}">&mdash;</span>
  {% endif %}
</td>
```

- [ ] **Step 4: Run tooltip tests to confirm pass**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```
Expected: All `TestCompositeScoreCell` tests PASS (Task 2 + Task 3 + Task 4 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_views.py job_finder/web/templates/jobs/_score_cell.html
git commit -m "feat(score-cell): add Tailwind tooltip with sub-scores + strength + gap"
```

---

## Task 5: Full regression check

**Files:** No code changes. Verification only.

- [ ] **Step 1: Run all tests in `test_views.py`**

```bash
uv run --active pytest tests/test_views.py -q --tb=short
```
Expected: ALL pass. Particularly verify:
- `test_compact_row_score_has_id`
- `test_rescore_response_has_oob_score_cell`
- `test_rescore_oob_score_cell_has_data_sort_score`
- `test_paste_jd_response_has_oob_score_cell`
- `test_paste_jd_oob_score_cell_has_data_sort_score`
- `test_score_cell_route_returns_td`
- `test_score_cell_route_404`

If any FAIL: investigate, fix template (preserving spec decisions), commit a `fix(score-cell): …` commit, re-run.

- [ ] **Step 2: Run the full project test suite**

```bash
uv run --active pytest -q --tb=short
```
Expected: All 1359+ tests PASS (per CLAUDE.md baseline).

If any test outside `test_views.py` fails: investigate. The most likely affected files are anything that asserts the OLD badge text `Apply`/`Consider`/`Skip`/`Reject` in HTML (search with `grep -rn '"Apply"\|"Consider"\|"Skip"\|"Reject"' tests/`). Update those assertions to match the new composite-number rendering.

- [ ] **Step 3: Spot-check no other template renders the old badge**

```bash
grep -rn "bg-green-900 text-green-200\|bg-amber-900 text-amber-200" job_finder/web/templates/
```
Expected: zero matches in `_score_cell.html` only — these classes may legitimately appear elsewhere (e.g., `_row_expanded.html` Fit Breakdown bars). If they appear in score-cell-equivalent rendering elsewhere, investigate.

- [ ] **Step 4: Commit (only if regression-fix commits were needed)**

If steps 1–3 produced a `fix(score-cell): …` commit, it should already be in. No commit needed otherwise.

---

## Task 6: Manual UI verification

**Files:** No code changes. Visual + interaction verification per CLAUDE.md "Verification Standards".

CLAUDE.md flags these as "human-needed" because they require real browser rendering:
- Visual aesthetic (color contrast, layout)
- Tooltip positioning (CSS `absolute` + `z-30` actually positions correctly)
- Tooltip hover smoothness (no flicker, no JS console errors)
- Sub-score breakdown in expanded view still renders correctly

- [ ] **Step 1: Start the dev server**

```bash
uv run python run.py
```
Server starts on `http://localhost:5000`.

- [ ] **Step 2: Open the job board**

Navigate to `http://localhost:5000/jobs`. Visual checklist:

- [ ] Score column shows colored numbers (not text badges)
- [ ] Apply rows are green
- [ ] Consider rows are amber
- [ ] Skip rows are slate-gray
- [ ] Reject rows are red
- [ ] Unscored rows show `—` in muted color
- [ ] Numbers are right-aligned in their column
- [ ] Freshness `*` star still appears on rows with `first_seen` ≤ 3 days ago

- [ ] **Step 3: Test tooltip hover**

Hover over a colored composite number. Tooltip checklist:

- [ ] Tooltip appears with no perceptible delay
- [ ] Sub-score line shows all 6 axes with values (e.g., `Title 5 · Location 4 · Comp 5 · Domain 4 · Seniority 5 · Skills 4`)
- [ ] Strength line appears (when present), prefixed with green "Strength:"
- [ ] Gap line appears (when present), prefixed with red "Gap:"
- [ ] Tooltip is readable (sufficient contrast on dark theme)
- [ ] Tooltip does not get clipped by table edge or page scroll
- [ ] Tooltip disappears when mouse leaves the score cell
- [ ] Browser console has no errors (DevTools → Console)

- [ ] **Step 4: Verify expanded view still works**

Click a row to expand. Verification:

- [ ] Expanded row's "Fit Breakdown" section still renders the 6 sub-score bars (per `_row_expanded.html`)
- [ ] Bar colors and labels unchanged

- [ ] **Step 5: Test rescore OOB swap**

Pick a row with a `jd_full` and click "Score with JD" (rescore button). Verification:

- [ ] Score cell updates with new color/number after a moment
- [ ] Tooltip still works on the updated cell
- [ ] No full page reload

- [ ] **Step 6: Sort by Score and visually scan**

In the filter bar, set Sort By: Score, Direction: Desc. Verification:

- [ ] Apply rows cluster at top (all green)
- [ ] Within Apply, numbers descend (e.g., 30, 28, 27, 22, 18)
- [ ] After Apply rows, Consider rows begin (amber)
- [ ] Within Consider, numbers descend
- [ ] Skip → Reject continue the pattern
- [ ] No green Apply row appears below an amber Consider row (classification floor enforced)

- [ ] **Step 7: Stop the dev server**

`Ctrl+C` in the terminal where `run.py` is running.

- [ ] **Step 8: If any visual/interaction issue surfaced — fix and re-verify**

Likely fixes:
- Tooltip clipped: increase `z-30` → `z-50`, or change positioning from `left-full ml-2` to a different edge.
- Tooltip too narrow / too wide: adjust `w-72` to `w-64` or `w-80`.
- Color contrast bad on dark theme: switch from `-400` to `-300` shades.

Each fix is an atomic commit:
```bash
git add job_finder/web/templates/jobs/_score_cell.html
git commit -m "fix(score-cell): {specific issue}"
```

---

## Done Criteria

- All tests in `tests/test_views.py::TestCompositeScoreCell` pass.
- Full project test suite passes (`uv run --active pytest -q --tb=short`).
- Manual browser verification (Task 6) checklist complete.
- No old badge classes (`bg-green-900`, `bg-amber-900`, `bg-red-900`, `bg-slate-800` in score-cell context) remain in `_score_cell.html`.
- Score column renders colored composite numbers with working tooltips.
- Sort behavior unchanged (existing tests pass).
- OOB swap path unchanged (existing tests pass).

## Out of Scope (do not implement)

Per spec, these are explicitly deferred:
- Color intensity gradient within class
- Score column header rename to "Fit"
- DB-stored composite column
- Flat sort by composite (ignoring classification)
- Sub-score breakdown in compact row
- Per-axis user-configurable weights
- Tooltip JS framework / library
- Native HTML `title` fallback
