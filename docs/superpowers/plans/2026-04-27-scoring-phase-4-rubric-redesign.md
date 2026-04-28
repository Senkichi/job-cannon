# Scoring Recalibration Phase 4: Rubric Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Execution order note:** This plan executes AFTER Phase 5 (eval harness). The dependency chain is: 1 → 2 → 3 → 5 → **4** → 6.

**Goal:** Design and test 6–8 rubric prompt variants against the gold-set baseline; select a winner that passes all Phase 6 acceptance gates (apply false-positive rate strictly improves; no per-axis MAE regresses by >0.2; schema adherence ≥95%; coherence violation rate strictly improves; all 3 anchor cases moved out of `apply`).

**Architecture:** Each variant is a Python module exporting the same 4 names as `v3_scoring_prompt.py` (`V3_SCORING_PROMPT`, `JOB_ASSESSMENT_SCHEMA`, `FEWSHOT_EXAMPLES`, `FIELD_REINFORCEMENT`). Variants live in `job_finder/web/scoring_prompts/variants/`. The harness loads them by name. Iteration is structured as: (1) screen one-dimension-at-a-time vs baseline; (2) A/B finalists; (3) commit winner and update production config.

**Tech Stack:** Python 3.13, the eval harness from Phase 5, no new infrastructure.

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 4, decisions D-4.1 through D-4.4).

**Predecessor plan:** `2026-04-27-scoring-phase-5-eval-harness.md`
**Successor plan:** `2026-04-27-scoring-phase-6-rollout.md`

---

## File Structure

### Created files

| File | Responsibility |
|---|---|
| `job_finder/web/scoring_prompts/variants/__init__.py` | Empty package marker |
| `job_finder/web/scoring_prompts/variants/baseline.py` | Re-exports v3_scoring_prompt for harness consistency |
| `job_finder/web/scoring_prompts/variants/v4a1_strict_threshold.py` | Dimension A1 — `apply ← all ≥ 4` |
| `job_finder/web/scoring_prompts/variants/v4a2_mean_floor.py` | Dimension A2 — `apply ← mean ≥ 3.5 AND min ≥ 3` |
| `job_finder/web/scoring_prompts/variants/v4b1_no_signal_code.py` | Dimension B1 — explicit `0` no-signal code |
| `job_finder/web/scoring_prompts/variants/v4b3_evidence_quote.py` | Dimension B3 — per-axis evidence quote required |
| `job_finder/web/scoring_prompts/variants/v4d1_cot_first.py` | Dimension D1 — chain-of-thought before scoring |
| `job_finder/web/scoring_prompts/variants/v4d2_per_axis_evidence.py` | Dimension D2 — per-axis `{evidence, score}` pairs |
| `job_finder/web/scoring_prompts/variants/v4_finalist.py` | Combination of winning dimensions, named at runtime |
| `tests/test_variants_loadable.py` | Sanity: every variant imports and exports the 4 required names |

### Modified files

| File | Lines (approx) | Responsibility |
|---|---|---|
| `job_finder/web/job_scorer.py` | +5 | Read `scoring.prompt_variant` config knob; load named variant module instead of hard-coded import |
| `config.example.yaml` | +1 | New knob `scoring.prompt_variant: <name>` |
| `config.yaml` | +1 | Same (Edit only — never Write) |

### Files explicitly NOT touched

- `job_finder/web/scoring_prompts/v3_scoring_prompt.py` — frozen "production" prompt; preserved as the `baseline` reference

---

## Test Strategy

This phase is **experimental**, not strictly TDD-shaped. Each task is a checkpoint with a clear deliverable but the "test" is the harness output, not a unit test. The structural tests (variants are loadable; config knob plumbing works) are TDD; the variant-selection tasks are protocol.

```bash
uv run --active pytest tests/test_variants_loadable.py -q --tb=short
```

---

## Task 4.1: Wire variant-selection config knob (TDD)

**Files:**
- Modify: `job_finder/web/job_scorer.py:78-95` — `_build_system_prompt` loads variant by name
- Modify: `config.example.yaml`, `config.yaml` — add `scoring.prompt_variant: baseline`
- Create: `tests/test_variant_selection.py`

- [ ] **Step 1: Write failing test**

```python
"""Tests: scoring uses the variant named in scoring.prompt_variant config."""

import pytest


def test_baseline_variant_loaded_when_config_set(monkeypatch):
    from job_finder.web.job_scorer import _build_system_prompt
    config = {"scoring": {"prompt_variant": "baseline"}}
    prompt = _build_system_prompt(candidate_context=None, config=config)
    # baseline = v3_scoring_prompt → contains the production header
    assert "Six dimensions — 1-5 integer scale" in prompt or "1-5 integer" in prompt


def test_variant_module_loaded_when_named(monkeypatch, tmp_path):
    """Plant a fake variant in importable space and verify it's loaded."""
    # ... use a fixture variant module that exports V3_SCORING_PROMPT="MARKER" ...
    config = {"scoring": {"prompt_variant": "fixture_variant_name"}}
    prompt = _build_system_prompt(candidate_context=None, config=config)
    assert "MARKER" in prompt


def test_unknown_variant_raises_clear_error():
    from job_finder.web.job_scorer import _build_system_prompt
    config = {"scoring": {"prompt_variant": "does_not_exist"}}
    with pytest.raises(ImportError, match="does_not_exist"):
        _build_system_prompt(candidate_context=None, config=config)
```

- [ ] **Step 2: Implement variant loader in `_build_system_prompt`**

```python
def _build_system_prompt(
    candidate_context: str | None = None,
    config: dict | None = None,
) -> str:
    """Assemble the system prompt from the named variant."""
    config = config or {}
    variant_name = config.get("scoring", {}).get("prompt_variant", "baseline")

    if variant_name == "baseline":
        from job_finder.web.scoring_prompts import v3_scoring_prompt as mod
    else:
        import importlib
        try:
            mod = importlib.import_module(
                f"job_finder.web.scoring_prompts.variants.{variant_name}"
            )
        except ImportError as e:
            raise ImportError(f"Unknown scoring prompt variant: {variant_name}") from e

    header = getattr(mod, "V3_SCORING_PROMPT_HEADER", None) or mod.V3_SCORING_PROMPT
    fewshot = mod.FEWSHOT_EXAMPLES
    field_reinforcement = mod.FIELD_REINFORCEMENT

    if candidate_context:
        return (
            header + "\n\n" + field_reinforcement
            + "\n\n" + candidate_context + "\n\n" + fewshot
        )
    return header + "\n\n" + fewshot + "\n\n" + field_reinforcement
```

Update callers: `score_job` should now pass `config=config` to `_build_system_prompt`.

- [ ] **Step 3: Add config knob**

In `config.example.yaml`, under `scoring:`:
```yaml
scoring:
  prompt_variant: baseline  # name of variant module under scoring_prompts/variants/, or 'baseline'
```

In `config.yaml`, use **Edit tool** to add the same line under `scoring:`.

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run --active pytest tests/test_variant_selection.py tests/test_job_scorer.py -q --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/job_scorer.py tests/test_variant_selection.py config.example.yaml config.yaml
git commit -m "$(cat <<'EOF'
feat(scoring): scoring.prompt_variant config knob loads named variant

_build_system_prompt() reads scoring.prompt_variant from config and
imports the variant module from scoring_prompts/variants/. Default
'baseline' maps to v3_scoring_prompt (production). Unknown variant
names raise a clear ImportError.

Phase 4 task 1/5. Spec D-4.1.
EOF
)"
```

---

## Task 4.2: Create variant module template + baseline alias

**Files:**
- Create: `job_finder/web/scoring_prompts/variants/__init__.py` (empty)
- Create: `job_finder/web/scoring_prompts/variants/baseline.py` (re-export aliases)
- Create: `tests/test_variants_loadable.py`

- [ ] **Step 1: Variant template**

Each variant module follows this skeleton:

```python
"""Variant <name>: <one-line description of what this variant tests>.

Tests dimension <X> hypothesis <Y> from spec Phase 4.
"""

# Re-import unchanged pieces from baseline; override what this variant tests.
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA,  # variant may shadow this
    FIELD_REINFORCEMENT,
    FEWSHOT_EXAMPLES,
)

# Override the prompt header (rubric anchors, classification rule, etc.)
V3_SCORING_PROMPT = """..."""  # custom

# If schema changes (e.g., adding evidence fields), shadow it:
# JOB_ASSESSMENT_SCHEMA = {...}
```

- [ ] **Step 2: Create `baseline.py` as a pure re-export**

```python
"""Variant 'baseline': aliases v3_scoring_prompt for harness consistency.

The harness loads variants by name; baseline is the production prompt
unchanged. This module exists so variant-loading code is uniform.
"""

from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA,
    V3_SCORING_PROMPT,
    FIELD_REINFORCEMENT,
    FEWSHOT_EXAMPLES,
)

__all__ = ["JOB_ASSESSMENT_SCHEMA", "V3_SCORING_PROMPT", "FIELD_REINFORCEMENT", "FEWSHOT_EXAMPLES"]
```

- [ ] **Step 3: Test all variants are loadable**

```python
"""Sanity: every variant module exports the required 4 names."""

import importlib
import pkgutil
import pytest

import job_finder.web.scoring_prompts.variants as variants_pkg


@pytest.mark.parametrize("name",
    [m.name for m in pkgutil.iter_modules(variants_pkg.__path__)
     if not m.name.startswith("_")])
def test_variant_exports_required_names(name):
    mod = importlib.import_module(f"job_finder.web.scoring_prompts.variants.{name}")
    for attr in ("JOB_ASSESSMENT_SCHEMA", "V3_SCORING_PROMPT", "FIELD_REINFORCEMENT", "FEWSHOT_EXAMPLES"):
        assert hasattr(mod, attr), f"Variant {name} missing {attr}"
```

- [ ] **Step 4: Run tests**

```bash
uv run --active pytest tests/test_variants_loadable.py -q --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/scoring_prompts/variants/ tests/test_variants_loadable.py
git commit -m "$(cat <<'EOF'
feat(scoring): variant module structure + baseline alias

Establishes scoring_prompts/variants/ as the home for prompt
variants. baseline.py re-exports v3_scoring_prompt for harness
uniformity. Test_variants_loadable parametrizes over the
directory so every new variant is automatically smoke-tested.

Phase 4 task 2/5.
EOF
)"
```

---

## Task 4.3: Author screening variants (one per dimension)

**Files (create one each):**
- `variants/v4a1_strict_threshold.py` — Dimension A1
- `variants/v4a2_mean_floor.py` — Dimension A2
- `variants/v4b1_no_signal_code.py` — Dimension B1
- `variants/v4b3_evidence_quote.py` — Dimension B3
- `variants/v4d1_cot_first.py` — Dimension D1
- `variants/v4d2_per_axis_evidence.py` — Dimension D2

**Variant authoring guidance** (per spec):
- Variants change ONE dimension at a time vs baseline (so the harness diff isolates the dimension's effect)
- Each variant cites its spec dimension in the docstring (e.g., "Dimension A1 from spec Phase 4")
- Few-shot examples updated within each variant to be **persona-correct** (analytics/data science, NOT ML engineer — fixes the few-shot persona drift identified in RC1)
- Schema changes (e.g., adding `evidence` fields) require shadowing `JOB_ASSESSMENT_SCHEMA`

**Note on Dimension A** (classification rule): A1 and A2 are *Python-derived* rules in `db.derive_classification`, not in the prompt. Variants for A only change the prompt's *guidance* (e.g., "be conservative on apply"); the actual rule shift is a parallel change in `derive_classification` gated by config. Defer the rule-change to Task 4.4 once a winning prompt is identified.

- [ ] **Step 1: Author A1 — Stricter threshold variant**

```python
"""Variant v4a1_strict_threshold: tests Dimension A1.

Hypothesis: tightening the apply threshold to all-≥4 reduces apply
false-positive rate at acceptable cost to apply recall, when combined
with the unchanged prompt. The actual rule lives in derive_classification
(gated by config); the prompt here adds explicit "be conservative" framing
so the model knows the bar moved.
"""

from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA, FEWSHOT_EXAMPLES,
)

FIELD_REINFORCEMENT = """..."""  # same as baseline + extra "apply requires every axis ≥4" line

V3_SCORING_PROMPT = """..."""  # baseline prompt with explicit "apply only when every axis is a 4 or 5" guidance
```

(Author the actual prompt text. Inherit ~95% from `v3_scoring_prompt.V3_SCORING_PROMPT`; override the dimension anchors with stricter language.)

- [ ] **Step 2: Author A2 — Mean+floor variant**

Similar to A1; the prompt guidance shifts to "consider average across axes" and the rule change is in derive_classification.

- [ ] **Step 3: Author B1 — Explicit no-signal code**

```python
"""Variant v4b1_no_signal_code: tests Dimension B1.

Hypothesis: adding an explicit '0 = no signal in JD' code (separate
from '3 = neutral evidence') prevents the default-to-3 pathology
identified in RC3.
"""

JOB_ASSESSMENT_SCHEMA = {
    # Same as baseline but each sub-score's minimum is 0, not 1
    "type": "object",
    "additionalProperties": False,
    "required": [...],
    "properties": {
        "title_fit": {"type": "integer", "minimum": 0, "maximum": 5},
        # ... rest with minimum=0
    },
}

V3_SCORING_PROMPT = """...

### Six dimensions — 0-5 integer scale

- 0 — no signal in JD; do not infer
- 1 — strong mismatch / disqualifying
- 2 — weak match, significant gaps
...
"""
```

Note: this variant requires `derive_classification` to handle 0s. Document this dependency clearly in the variant docstring; the rule shift is gated separately (Task 4.4).

- [ ] **Step 4: Author B3 — Per-axis evidence quote required**

Force the model to emit a JD quote per axis; if it can't quote, score caps at 2.

- [ ] **Step 5: Author D1 — Chain-of-thought before scoring**

```python
"""Variant v4d1_cot_first: tests Dimension D1.

Hypothesis: forcing rationale-before-scores (chain-of-thought) reduces
coherence violations (rationale contradicting scores).
"""

JOB_ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": ["rationale", "title_fit", "location_fit", ...],  # rationale first in required order
    "properties": {
        "rationale": {...},  # FIRST in property declaration order (model sees it first in some schemas)
        "title_fit": {...},
        ...
    },
}

V3_SCORING_PROMPT = """...
## Output order

Emit your reasoning under 'rationale' FIRST. Then assign each numeric score
in light of the reasoning you just wrote. Do not score before reasoning.
..."""
```

- [ ] **Step 6: Author D2 — Per-axis evidence pairs**

```python
"""Variant v4d2_per_axis_evidence: tests Dimension D2.

Each sub-score becomes {evidence: <text>, score: <int>}. Forces the
model to commit reasoning before each number.
"""

JOB_ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": [...],
    "properties": {
        "title_fit": {
            "type": "object",
            "required": ["evidence", "score"],
            "properties": {
                "evidence": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
            },
        },
        # ... same shape for each axis
    },
}
```

(Note: this requires changes in `_coerce_assessment` in `job_scorer.py` to extract `score` from the nested object. That coupling is documented in the variant.)

- [ ] **Step 7: Run loadable test, verify all 6 variants are importable**

```bash
uv run --active pytest tests/test_variants_loadable.py -q --tb=short
```

- [ ] **Step 8: Commit each variant**

Per the project's "atomic commits" convention, commit each variant separately:

```bash
for v in v4a1_strict_threshold v4a2_mean_floor v4b1_no_signal_code v4b3_evidence_quote v4d1_cot_first v4d2_per_axis_evidence; do
  git add job_finder/web/scoring_prompts/variants/${v}.py
  git commit -m "feat(scoring): variant ${v} — Phase 4 screening pass"
done
```

(Or batch commit if commits are functionally equivalent.)

---

## Task 4.4: Screening pass — run each variant against baseline

This is the core experimental work. For each of the 6 screening variants:

1. Set `scoring.prompt_variant: <name>` in config.yaml (Edit tool, surgical)
2. Run harness: `uv run python -m job_finder.eval --variant <name> --baseline <baseline_run_id> --runs 3`
3. Read the report — start with the per-job diff table
4. Note the headline result (apply-FP-rate delta vs baseline)
5. Reset `scoring.prompt_variant: baseline` before the next variant

- [ ] **Step 1: Read the baseline run_id from `eval_runs`**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
r = conn.execute('SELECT run_id FROM eval_runs WHERE variant_name=\"baseline\" ORDER BY timestamp DESC LIMIT 1').fetchone()
print(r[0])
"
```

Save this as `BASELINE_RUN_ID`.

- [ ] **Step 2: Run each variant in turn**

For each `<variant>`:

```bash
# Edit config.yaml: scoring.prompt_variant: <variant>
uv run python -m job_finder.eval --variant <variant> --baseline $BASELINE_RUN_ID --runs 3
# Reset config.yaml: scoring.prompt_variant: baseline
```

Each run takes ~10–15 minutes. Plan ~90 minutes total for the screening pass (6 variants × 15 min).

- [ ] **Step 3: Build a screening summary table**

For each variant, record from the report:
- Apply-FP rate (vs baseline delta + 95% CI)
- Per-axis MAE deltas (none should regress > 0.2)
- Coherence violation rate
- Schema adherence (must stay ≥ 95%)
- Whether anchor cases (Vera, Latent, DeepMind) moved out of `apply`

Write this summary to `.planning/eval_results/SCREENING-SUMMARY.md` for the record.

- [ ] **Step 4: Identify the top 2–3 dimensions worth combining**

Pick the dimensions whose variants most-improved the headline metric. Typical pattern:
- One A-dimension winner (classification rule)
- Possibly one B-dimension winner (semantics of "3" or no-signal code)
- One D-dimension winner (rationale-first or evidence pairs)

Variants from C (anchor density) might or might not show up depending on lit-survey findings.

---

## Task 4.5: Author and test the finalist variant

**Files:**
- Create: `job_finder/web/scoring_prompts/variants/v4_finalist.py`

The finalist combines the winning dimensions identified in Task 4.4. Often this is 2 dimensions — e.g., A1 + D1 (stricter threshold + CoT-first).

- [ ] **Step 1: Compose the finalist**

```python
"""Variant v4_finalist: combines screening winners.

Combines: <winner from A>, <winner from B if any>, <winner from D>.
Hypothesis: dimensions are independent enough that improvements stack.
"""

# ... schema and prompt combining the winning techniques ...
```

- [ ] **Step 2: Run finalist with 5 runs (not 3) for tighter variance estimate**

```bash
uv run python -m job_finder.eval --variant v4_finalist --baseline $BASELINE_RUN_ID --runs 5
```

5 runs catches non-determinism more reliably than 3 (per spec D-5.2).

- [ ] **Step 3: Check acceptance gates**

Read the finalist report and verify ALL of these (Phase 6 acceptance gates):
- [ ] All 3 anchor cases NOT in `apply`
- [ ] Apply false-positive rate strictly improves (delta < 0 with 95% CI not crossing 0)
- [ ] Per-axis MAE worsens by > 0.2 on no axis
- [ ] Schema adherence ≥ 95%
- [ ] Coherence violation rate strictly improves

If any gate fails, iterate: try a different combination, or refine the variant's prompt text. Each iteration is one harness run (~15 min).

- [ ] **Step 4: If a final winner emerges, commit**

```bash
git add job_finder/web/scoring_prompts/variants/v4_finalist.py
git commit -m "$(cat <<'EOF'
feat(scoring): finalist prompt variant — combines <list winning dimensions>

Combines screening winners across dimensions <X> and <Y>.
Acceptance gates passed:
  - All 3 anchor cases moved out of apply: ✓
  - Apply FP rate: <baseline> → <finalist> (Δ <delta>, 95% CI [<lo>, <hi>])
  - No per-axis MAE regression > 0.2
  - Schema adherence: <pct>%
  - Coherence violation rate: <baseline pct>% → <finalist pct>%

Reports: .planning/eval_results/<paths>

Phase 4 task 5/5. Phase 4 complete.
EOF
)"
```

If after 5–8 iterations no variant passes the gates, escalate per spec: re-examine gold-set labels for ambiguity, consider stronger model, or re-scope.

---

## Task 4.6: Update production config to use the winner

**Files:**
- Modify: `config.yaml` (Edit only)

- [ ] **Step 1: Update `scoring.prompt_variant`**

```bash
# Edit config.yaml — surgical change
# scoring.prompt_variant: baseline  →  scoring.prompt_variant: v4_finalist
```

- [ ] **Step 2: Run a final regression-mode harness pass on production prompt**

```bash
uv run python -m job_finder.eval --variant v4_finalist --runs 3
```

Save this run_id — it becomes the new "baseline" for any future Phase-4-style work.

- [ ] **Step 3: Commit config change**

```bash
git add config.yaml
git commit -m "$(cat <<'EOF'
chore(config): switch production prompt to v4_finalist

scoring.prompt_variant flips from baseline to v4_finalist after
Phase 4 acceptance gates passed. Re-scoring of existing data
happens in Phase 6.
EOF
)"
```

---

## Acceptance criteria for Phase 4

- [ ] At least 6 screening variants authored, each isolating one dimension
- [ ] All 6 ran against baseline; reports present in `.planning/eval_results/`
- [ ] Screening summary written at `.planning/eval_results/SCREENING-SUMMARY.md`
- [ ] Finalist variant authored combining winning dimensions
- [ ] Finalist passed all 5 acceptance gates with 95% CIs documented
- [ ] `config.yaml` flipped to use the finalist as the new production prompt
- [ ] Test suite green: `uv run --active pytest tests/test_variants_loadable.py tests/test_variant_selection.py -q --tb=short`

## What this unlocks

Phase 6 (rollout) wholesale-rescores all existing jobs with the new prompt + the rule changes from Phase 2 + the now-honest enrichment cascade. The result is a production scoring system that is: profile-informed, fetches real JDs, surfaces uncertainty as low_signal, and uses a calibrated prompt that passed measured acceptance gates.

## Out of scope for this plan

- Pairwise / listwise scoring experiments (parked)
- Multi-judge ensembles (parked)
- Replacing qwen2.5:14b (parked unless no variant passes gates)
- Active-learning gold-set expansion (parked)
- Verbalized confidence per axis (parked beyond what D2 lightly tests)
