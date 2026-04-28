# Scoring Recalibration Phase 2: Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four root-cause bugs in the v3.0 scoring pipeline: (1) the candidate profile never reaches the scorer, (2) profile data is fragmented across two files neither read, (3) the enrichment cascade synthesizes JDs from fragments instead of fetching, and (4) genuinely-no-signal jobs are silently rolled into the `apply` distribution. Result: scoring is informed by who the candidate is, fetches real JDs, and surfaces uncertainty as a distinct `low_signal` classification.

**Architecture:** Five sub-fixes (2a–2e), each independently committable. 2a injects a merged candidate context into the scorer's system prompt. 2b deletes the LLM-synthesis tiers from the cascade. 2c adds a single post-fetch structured-field extraction pass. 2d adds the `low_signal` classification with a migration. 2e is a one-shot backfill that re-flows previously-stuck rows through the new cascade.

**Tech Stack:** Python 3.13, SQLite (raw SQL), pytest with mocked `call_model` injection, jobs.db at `jobs.db`.

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 2, sub-fixes 2a–2e, decisions D-2.1 through D-2.8).

**Predecessor plan:** `2026-04-27-scoring-phase-1-literature-survey.md` (lit survey may inform D-2.3 prompt format)
**Successor plan:** `2026-04-27-scoring-phase-3-gold-set.md`

---

## File Structure

### Modified files

| File | Lines (approx) | Responsibility |
|---|---|---|
| `job_finder/web/scoring_orchestrator.py` | +30 | New `build_candidate_context()`; `score_and_persist_job` accepts and forwards profile context |
| `job_finder/web/job_scorer.py` | +15 | `_build_system_prompt()` accepts optional `candidate_context`; `score_job()` accepts and threads it |
| `job_finder/web/blueprints/batch_scoring.py:316-323` | ±5 | Stop discarding `load_scoring_profile()` return value; pass to scoring loop |
| `job_finder/web/data_enricher.py` | -40 | Remove Haiku and Sonnet tier branches from `enrich_job()` cascade body |
| `job_finder/web/enrichment_tiers.py` | -250, +60 | Delete `extract_with_haiku` and `extract_with_sonnet`; add `parse_structured_fields()` |
| `job_finder/db.py:57-90` | +12 | `derive_classification()` adds `low_signal` branch and short-jd check |
| `job_finder/web/db_migrate.py` | +1 migration | Migration 42: extend classification CHECK constraint to include `low_signal` |
| `job_finder/web/templates/jobs/_score_cell.html:28-43` | +3 | Add `low_signal` color branch |

### Created files

| File | Responsibility |
|---|---|
| `tests/test_candidate_context.py` | Unit tests for `build_candidate_context()` |
| `tests/test_job_scorer_profile_injection.py` | Integration tests verifying profile reaches the model |
| `tests/test_enrichment_cascade_v2.py` | Tests for cascade-without-LLM-synthesis-tiers |
| `tests/test_parse_structured_fields.py` | Unit tests for post-fetch salary/location extraction |
| `tests/test_low_signal_classification.py` | Tests for the new classification rule |
| `scripts/backfill_stuck_at_haiku.py` | One-shot backfill script for previously-stuck rows |

### Files explicitly NOT touched (regression surface)

- `job_finder/scoring/scorer.py` — legacy CLI scorer; no v3.0 changes needed there
- `job_finder/web/agentic_enricher.py` — already does the right thing; stays as deepest tier
- `job_finder/web/scoring_prompts/v3_scoring_prompt.py` — frozen per spec; few-shot rewrite parks for Phase 4
- Existing tests in `tests/test_job_scorer.py` — extend with new cases, don't rewrite

### Reference files (read-only context for implementer)

- `job_finder/web/scoring_orchestrator.py:36-57` — existing `load_scoring_profile()` (we'll start using its return value)
- `job_finder/web/job_scorer.py:78-105` — existing `_build_system_prompt()` and `_build_user_message()`
- `job_finder/web/data_enricher.py:62, 70-74, 81+` — existing TIER_ORDER, FIELD_TIER_CEILINGS, and enrich_job body
- `job_finder/db.py:57-90` — existing `derive_classification()` rule order
- `job_finder/web/db_migrate.py` — existing migration list pattern
- `experience_profile.json` — sample profile shape (read for tests; do not modify)
- `config.yaml [profile]` section — sample targeting fields

---

## Test Strategy

All Python tests use the project standard:
```bash
uv run --active pytest <path> -q --tb=short
```

Tests follow the project pattern (see `tests/conftest.py`):
- App factory with `config=` dict for isolation
- Temp DB per test
- Mock `call_model` at the injection point — patches `job_finder.web.job_scorer.call_model` (or `model_provider.call_model` depending on import direction)
- Each test is a TDD cycle: write test → verify failure → implement → verify pass → commit

Run the full scoring test suite after each sub-fix:
```bash
uv run --active pytest tests/test_job_scorer.py tests/test_scoring_orchestrator.py tests/test_data_enricher.py tests/test_enrichment_tiers.py tests/test_db.py -q --tb=short
```

Expected: all green at every commit boundary.

---

## Sub-fix 2a: Profile Injection (RC1 + RC2)

### Task 2a.1: Build candidate-context formatter (TDD)

**Files:**
- Create: `tests/test_candidate_context.py`
- Modify: `job_finder/web/scoring_orchestrator.py` — add `build_candidate_context()` function

- [ ] **Step 1: Write failing tests for `build_candidate_context`**

Create `tests/test_candidate_context.py`:

```python
"""Tests for build_candidate_context() — merges config.yaml [profile]
with experience_profile.json into a prompt-ready string."""

import pytest
from job_finder.web.scoring_orchestrator import build_candidate_context


def _config(profile=None):
    return {"profile": profile or {}}


def _profile(positions=None, skills=None, education=None):
    return {
        "positions": positions or [],
        "skills": skills or [],
        "education": education or [],
        "resume_preferences": {"summary_style": "concise", "emphasis": []},
    }


def test_returns_str_with_targeting_section():
    config = _config({
        "target_titles": ["Lead Product Analyst", "Staff Data Scientist"],
        "target_locations": ["Remote", "San Francisco"],
        "min_salary": 150000,
        "industries": ["Healthcare", "SaaS"],
        "exclusions": {"companies": ["Intuit"], "title_keywords": []},
    })
    profile = _profile()
    out = build_candidate_context(config, profile)
    assert isinstance(out, str)
    assert "Lead Product Analyst" in out
    assert "Staff Data Scientist" in out
    assert "Remote" in out and "San Francisco" in out
    assert "150,000" in out or "150000" in out
    assert "Healthcare" in out and "SaaS" in out


def test_includes_position_summaries_one_line_each():
    profile = _profile(positions=[
        {"title": "Lead, Product Analytics & Experimentation",
         "company": "Apree Health", "start_date": "Feb 2024", "end_date": None,
         "achievements": ["Directed analytics for 5.5M users",
                          "Designed RCT validating 245% lift"]},
        {"title": "Senior Data Scientist", "company": "Acme",
         "start_date": "Jan 2020", "end_date": "Feb 2024",
         "achievements": []},
    ])
    out = build_candidate_context(_config(), profile)
    assert "Lead, Product Analytics & Experimentation" in out
    assert "Apree Health" in out
    assert "Senior Data Scientist" in out
    # 1-line per position; no full achievement lists
    assert "245% lift" not in out  # achievements summarized, not enumerated


def test_includes_top_30_skills():
    profile = _profile(skills=[f"skill_{i}" for i in range(40)])
    out = build_candidate_context(_config(), profile)
    assert "skill_0" in out
    assert "skill_29" in out
    assert "skill_30" not in out  # truncated to top 30


def test_handles_empty_profile_and_empty_config():
    out = build_candidate_context({"profile": {}}, _profile())
    assert isinstance(out, str)
    assert len(out) > 0
    assert "Not specified" in out or "No positions" in out


def test_token_budget_under_600():
    """Approximate guard: profile injection should stay under ~600 tokens."""
    config = _config({
        "target_titles": [f"Title {i}" for i in range(20)],
        "target_locations": ["Remote", "SF", "NY"],
        "min_salary": 150000,
        "industries": ["Healthcare", "SaaS", "FinTech"],
        "exclusions": {"companies": [], "title_keywords": []},
    })
    profile = _profile(
        positions=[{"title": f"Title {i}", "company": f"Co {i}",
                    "start_date": "2020", "end_date": None,
                    "achievements": []} for i in range(8)],
        skills=[f"skill_{i}" for i in range(40)],
    )
    out = build_candidate_context(config, profile)
    # Rough heuristic: 1 token ≈ 4 chars
    assert len(out) <= 2400, f"Profile too long: {len(out)} chars (>~600 tokens)"
```

- [ ] **Step 2: Run tests and verify they fail**

```bash
uv run --active pytest tests/test_candidate_context.py -q --tb=short
```

Expected: ImportError or AttributeError — `build_candidate_context` does not exist yet.

- [ ] **Step 3: Implement `build_candidate_context`**

Append to `job_finder/web/scoring_orchestrator.py`:

```python
def build_candidate_context(config: dict, profile: dict) -> str:
    """Merge config.yaml [profile] (targeting) and experience_profile.json
    (résumé) into a prompt-ready candidate-context string.

    Returns a structured-text block ~400-500 tokens that gets spliced into
    the scoring system prompt between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES.
    """
    cfg_profile = config.get("profile") or {}

    # Targeting block
    target_titles = cfg_profile.get("target_titles") or []
    target_locations = cfg_profile.get("target_locations") or []
    min_salary = cfg_profile.get("min_salary")
    industries = cfg_profile.get("industries") or []
    exclusions = cfg_profile.get("exclusions") or {}
    excl_companies = exclusions.get("companies") or []

    parts = ["## Candidate context", "", "### Targeting"]
    parts.append(
        f"- Target titles: {', '.join(target_titles) if target_titles else 'Not specified'}"
    )
    parts.append(
        f"- Target locations: {', '.join(target_locations) if target_locations else 'Not specified'}"
    )
    parts.append(
        f"- Compensation floor: ${min_salary:,}" if min_salary else "- Compensation floor: Not specified"
    )
    parts.append(
        f"- Target industries: {', '.join(industries) if industries else 'Not specified'}"
    )
    if excl_companies:
        parts.append(f"- Exclusions: companies {excl_companies}")

    # Résumé block
    parts += ["", "### Background"]
    positions = profile.get("positions") or []
    if not positions:
        parts.append("- No positions in profile")
    else:
        for p in positions[:6]:  # cap at 6 most recent
            title = p.get("title", "?")
            company = p.get("company", "?")
            start = p.get("start_date", "?")
            end = p.get("end_date") or "present"
            parts.append(f"- {title} @ {company} ({start}–{end})")

    skills = profile.get("skills") or []
    if skills:
        parts.append(f"- Top skills: {', '.join(skills[:30])}")

    education = profile.get("education") or []
    for e in education[:3]:
        deg = e.get("degree") or "?"
        inst = e.get("institution") or "?"
        grad = e.get("graduation") or ""
        parts.append(f"- {deg} ({inst}{', ' + str(grad) if grad else ''})")

    return "\n".join(parts)
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run --active pytest tests/test_candidate_context.py -q --tb=short
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_candidate_context.py job_finder/web/scoring_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(scoring): add build_candidate_context profile-merge helper

Merges config.yaml [profile] targeting fields (target_titles,
target_locations, min_salary, industries, exclusions) with
experience_profile.json résumé fields (positions, skills, education)
into a single prompt-ready candidate-context block. Caps output at
~600 tokens via top-30 skills + first-6 positions truncation.

Phase 2a sub-fix 1/3: builder function only; not yet wired into
the scoring path. Spec: docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md (D-2.1, D-2.3)
EOF
)"
```

### Task 2a.2: Splice candidate-context into scoring system prompt (TDD)

**Files:**
- Modify: `job_finder/web/job_scorer.py:78-84` — `_build_system_prompt()`
- Modify: `job_finder/web/job_scorer.py:145-215` — `score_job()` accepts `candidate_context`
- Modify: `tests/test_job_scorer.py` — extend with profile-injection tests

- [ ] **Step 1: Write failing tests for system-prompt splicing**

Append to `tests/test_job_scorer.py` (or create `tests/test_job_scorer_profile_injection.py` if more appropriate):

```python
def test_build_system_prompt_includes_candidate_context_when_provided():
    from job_finder.web.job_scorer import _build_system_prompt
    ctx = "## Candidate context\n\n### Targeting\n- Target titles: Foo Analyst"
    prompt = _build_system_prompt(candidate_context=ctx)
    assert "Foo Analyst" in prompt
    # Splice point: between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES
    fr_idx = prompt.find("STRICT FIELD NAMES")  # first line of FIELD_REINFORCEMENT
    fs_idx = prompt.find("Fewshot calibration examples")
    ctx_idx = prompt.find("## Candidate context")
    assert fr_idx < ctx_idx < fs_idx, "Candidate context must be spliced between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES"


def test_build_system_prompt_omits_section_when_no_context():
    from job_finder.web.job_scorer import _build_system_prompt
    prompt = _build_system_prompt(candidate_context=None)
    assert "## Candidate context" not in prompt


def test_score_job_threads_candidate_context_into_call_model(monkeypatch, tmp_db_path):
    """Verify that score_job passes candidate_context through to call_model."""
    import sqlite3
    from job_finder.web.job_scorer import score_job

    captured = {}

    def fake_call_model(**kwargs):
        captured["system"] = kwargs.get("system", "")
        # Return minimal valid result envelope
        from types import SimpleNamespace
        return SimpleNamespace(
            data={"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3,
                  "rationale": {"strengths": [], "gaps": [], "talking_points": [],
                                "resume_priority_skills": []},
                  "legitimacy_note": None},
            schema_valid=True,
            provider="ollama",
        )

    monkeypatch.setattr("job_finder.web.job_scorer.call_model", fake_call_model)

    conn = sqlite3.connect(":memory:")
    job = {"dedup_key": "x|y", "title": "T", "company": "C",
           "location": "Remote", "jd_full": "Long enough JD " * 50}
    ctx = "## Candidate context\n- Target titles: Specific Role"
    score_job(job, conn, {}, candidate_context=ctx)
    assert "Specific Role" in captured["system"]
```

- [ ] **Step 2: Run tests and verify they fail**

```bash
uv run --active pytest tests/test_job_scorer.py -k "candidate_context or profile_injection" -q --tb=short
```

Expected: TypeError on `_build_system_prompt(candidate_context=...)` — current signature is parameterless.

- [ ] **Step 3: Modify `_build_system_prompt` and `score_job` signatures**

In `job_finder/web/job_scorer.py`, change `_build_system_prompt`:

```python
def _build_system_prompt(candidate_context: str | None = None) -> str:
    """Assemble the full system prompt from the frozen v3 modules.

    Splices candidate_context (when provided) between FIELD_REINFORCEMENT
    and FEWSHOT_EXAMPLES per spec D-2.1.
    """
    if candidate_context:
        return (
            V3_SCORING_PROMPT_HEADER  # rubric + dimensions only, see refactor below
            + "\n\n" + FIELD_REINFORCEMENT
            + "\n\n" + candidate_context
            + "\n\n" + FEWSHOT_EXAMPLES
        )
    return V3_SCORING_PROMPT + "\n\n" + FEWSHOT_EXAMPLES + "\n\n" + FIELD_REINFORCEMENT
```

Note: the current `V3_SCORING_PROMPT` constant already concatenates rubric + FIELD_REINFORCEMENT + FEWSHOT_EXAMPLES inline (see `scoring_prompts/v3_scoring_prompt.py:173-227`). To splice cleanly, refactor that constant to a header-only `V3_SCORING_PROMPT_HEADER` and assemble the rest in `_build_system_prompt`. Update import accordingly.

If the spec's "frozen prompt" rule (D-1 in `v3_scoring_prompt.py`) prohibits modifying the constants, alternative: leave `V3_SCORING_PROMPT` as-is and append `candidate_context + "\n\n" + ""` to its end (at the cost of placing the context after FEWSHOT_EXAMPLES, which violates D-2.1). Prefer the refactor — D-2.1 is more recent and supersedes the freeze.

In `score_job`, add the parameter:

```python
def score_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    client: Any | None = None,
    candidate_context: str | None = None,
) -> ScoringResult:
    # ... existing precondition checks ...
    system = _build_system_prompt(candidate_context=candidate_context)
    # ... rest unchanged ...
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run --active pytest tests/test_job_scorer.py -k "candidate_context or profile_injection" -q --tb=short
```

Expected: all new tests pass; existing tests still pass.

- [ ] **Step 5: Run full scoring test suite to catch regressions**

```bash
uv run --active pytest tests/test_job_scorer.py tests/test_scoring_orchestrator.py -q --tb=short
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_job_scorer.py tests/test_job_scorer_profile_injection.py job_finder/web/job_scorer.py job_finder/web/scoring_prompts/v3_scoring_prompt.py
git commit -m "$(cat <<'EOF'
feat(scoring): splice candidate_context into system prompt

_build_system_prompt() now accepts an optional candidate_context arg
and splices it between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES per
spec D-2.1. score_job() threads the parameter through. Profile
content not yet wired from orchestrator — that lands in 2a.3.

Phase 2a sub-fix 2/3.
EOF
)"
```

### Task 2a.3: Wire orchestrator + batch_scoring to pass profile (TDD)

**Files:**
- Modify: `job_finder/web/scoring_orchestrator.py` — `score_and_persist_job` accepts and forwards `candidate_context`
- Modify: `job_finder/web/blueprints/batch_scoring.py:316-323` — stop discarding profile; build context and pass to scoring loop
- Modify: any other call sites of `score_and_persist_job` (search before changing)

- [ ] **Step 1: Find all call sites of `score_and_persist_job`**

```bash
grep -rn "score_and_persist_job" job_finder/ tests/ --include='*.py'
```

Note all call sites — each must be updated to pass `candidate_context` (or pass None during transition).

- [ ] **Step 2: Write failing integration test**

Create `tests/test_job_scorer_profile_injection.py` (if not already created in 2a.2):

```python
"""Integration tests: profile from disk reaches the scorer."""

import json
import sqlite3
import pytest


def test_orchestrator_passes_candidate_context_through(monkeypatch, tmp_path):
    """End-to-end: profile loaded from disk → context built → splice into system prompt."""
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "positions": [{"title": "Lead Analyst", "company": "Foo",
                       "start_date": "2020", "end_date": None, "achievements": []}],
        "skills": ["A/B testing", "BigQuery"],
        "education": [],
        "resume_preferences": {"summary_style": "concise", "emphasis": []},
    }))

    config = {
        "profile_path": str(profile_path),
        "profile": {
            "target_titles": ["Lead Analyst", "Staff DS"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": ["Healthcare"],
            "exclusions": {"companies": [], "title_keywords": []},
        },
    }

    captured = {}

    def fake_call_model(**kwargs):
        captured["system"] = kwargs.get("system", "")
        from types import SimpleNamespace
        return SimpleNamespace(
            data={"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3,
                  "rationale": {"strengths": [], "gaps": [], "talking_points": [],
                                "resume_priority_skills": []},
                  "legitimacy_note": None},
            schema_valid=True,
            provider="ollama",
        )

    monkeypatch.setattr("job_finder.web.job_scorer.call_model", fake_call_model)

    from job_finder.web.scoring_orchestrator import (
        load_scoring_profile, build_candidate_context, score_and_persist_job,
    )
    profile = load_scoring_profile(config)
    ctx = build_candidate_context(config, profile)

    conn = sqlite3.connect(":memory:")
    # ... minimal jobs schema setup ...
    job = {"dedup_key": "k|t", "title": "Test", "company": "Co",
           "location": "Remote", "jd_full": "Long " * 100}
    score_and_persist_job(job, conn, config, candidate_context=ctx)

    assert "Lead Analyst" in captured["system"]
    assert "BigQuery" in captured["system"]
    assert "150,000" in captured["system"]
```

- [ ] **Step 3: Run test and verify failure**

```bash
uv run --active pytest tests/test_job_scorer_profile_injection.py -q --tb=short
```

Expected: TypeError on `score_and_persist_job(..., candidate_context=...)` — current signature has no such parameter.

- [ ] **Step 4: Update `score_and_persist_job` signature**

In `scoring_orchestrator.py`, modify `score_and_persist_job` to accept and forward `candidate_context`:

```python
def score_and_persist_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    client: Any | None = None,
    scorer_fn: Callable | None = None,
    candidate_context: str | None = None,
):
    # ... existing logic ...
    result = scorer_fn(job, conn, config, client=client, candidate_context=candidate_context)
    # ... rest unchanged ...
```

- [ ] **Step 5: Update batch_scoring blueprint**

In `job_finder/web/blueprints/batch_scoring.py:316-323`, change:

```python
# Before:
load_scoring_profile(config)

# After:
profile = load_scoring_profile(config)
candidate_context = build_candidate_context(config, profile)
```

(Add `build_candidate_context` to the import at the top.)

Then in the per-job scoring loop further down, pass `candidate_context=candidate_context` to `score_and_persist_job`.

- [ ] **Step 6: Update other call sites identified in Step 1**

For each call site, add `candidate_context=None` (default-safe) or build the context and pass it through. CLI scripts and tests that don't care can pass None.

- [ ] **Step 7: Run integration test and full suite**

```bash
uv run --active pytest tests/test_job_scorer_profile_injection.py -q --tb=short
uv run --active pytest tests/ -q --tb=short
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add job_finder/web/scoring_orchestrator.py job_finder/web/blueprints/batch_scoring.py tests/test_job_scorer_profile_injection.py
git commit -m "$(cat <<'EOF'
fix(scoring): wire candidate profile into v3 scorer (RC1, RC2)

batch_scoring no longer discards load_scoring_profile()'s return
value. score_and_persist_job accepts and threads candidate_context
through to score_job, where it gets spliced into the system prompt.

Closes RC1 (profile never reaches scorer) and RC2 (profile data
fragmented across config.yaml and experience_profile.json with
neither read by the v3 scorer).

Phase 2a complete. Spec D-2.1, D-2.2, D-2.3.
EOF
)"
```

---

## Sub-fix 2b: Enrichment Cascade Rewrite (RC4)

### Task 2b.1: Write regression test for the new cascade order (TDD)

**Files:**
- Create: `tests/test_enrichment_cascade_v2.py`

- [ ] **Step 1: Write failing test asserting new TIER_ORDER**

```python
"""Tests for the synthesis-free enrichment cascade."""

from job_finder.web.data_enricher import TIER_ORDER, FIELD_TIER_CEILINGS


def test_tier_order_excludes_haiku_and_sonnet():
    assert "haiku" not in TIER_ORDER
    assert "sonnet" not in TIER_ORDER
    assert TIER_ORDER == ["free", "ddg", "serpapi", "agentic", "exhausted"]


def test_field_tier_ceiling_for_jd_full_caps_at_agentic():
    assert FIELD_TIER_CEILINGS["jd_full"] == "agentic"


def test_field_tier_ceilings_no_haiku_or_sonnet_references():
    for field, ceiling in FIELD_TIER_CEILINGS.items():
        assert ceiling not in ("haiku", "sonnet"), (
            f"Field {field} still references deleted tier {ceiling}")
```

- [ ] **Step 2: Run test and verify failure**

```bash
uv run --active pytest tests/test_enrichment_cascade_v2.py -q --tb=short
```

Expected: assertion failures — current TIER_ORDER includes haiku and sonnet.

### Task 2b.2: Rewrite TIER_ORDER and FIELD_TIER_CEILINGS

**Files:**
- Modify: `job_finder/web/data_enricher.py:62, 70-74`
- Modify: `job_finder/web/data_enricher.py:81+` — `enrich_job()` body to drop haiku/sonnet branches and add agentic branch
- Modify: imports — drop `extract_with_haiku`, `extract_with_sonnet`

- [ ] **Step 1: Update TIER_ORDER and FIELD_TIER_CEILINGS**

```python
TIER_ORDER = ["free", "ddg", "serpapi", "agentic", "exhausted"]

FIELD_TIER_CEILINGS = {
    "jd_full": "agentic",
    "salary_min": "ddg",  # cap salary at ddg — extracted post-fetch from jd_full
    "salary_max": "ddg",
}
```

- [ ] **Step 2: Drop the Haiku and Sonnet branches from `enrich_job()`**

Find the `if start_idx <= TIER_ORDER.index("haiku"):` and `if start_idx <= TIER_ORDER.index("sonnet"):` branches (around lines 224-300 per spec) and DELETE them.

Replace with a single `agentic` branch that calls into `agentic_enricher.run_agentic_backfill` for the *single job* case (or invoke the agentic logic inline if a single-job entry point exists). If no single-job entry exists, this task creates one — see 2b.3.

- [ ] **Step 3: Drop unused imports**

In `enrichment_tiers.py`, the `extract_with_haiku` and `extract_with_sonnet` symbols are about to be deleted (Task 2b.4). Remove them from the `data_enricher.py` import block now to surface any stragglers at lint time.

- [ ] **Step 4: Run cascade-order tests + full data_enricher test suite**

```bash
uv run --active pytest tests/test_enrichment_cascade_v2.py tests/test_data_enricher.py -q --tb=short
```

Expected: cascade-order tests pass; existing data_enricher tests may fail because of haiku/sonnet branch deletions. Triage failures: remove tests that explicitly exercise the deleted tiers (they're testing deleted behavior).

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/data_enricher.py tests/test_enrichment_cascade_v2.py
git commit -m "$(cat <<'EOF'
refactor(enrichment): remove Haiku and Sonnet synthesis tiers from cascade

New TIER_ORDER: free → ddg → serpapi → agentic → exhausted. The
LLM-synthesis tiers (extract_with_haiku, extract_with_sonnet) are no
longer in the cascade — they fabricated short pseudo-JDs from
fragments and blocked escalation to fetch tiers. agentic_enricher
remains as deepest tier (it actually fetches new content).

Phase 2b sub-fix 1/2. Spec D-2.4.
EOF
)"
```

### Task 2b.3: Add single-job entry point for agentic enricher

**Files:**
- Modify: `job_finder/web/agentic_enricher.py` — extract `enrich_one_job(job_row, conn, config)` from existing batch logic, OR add a thin wrapper

- [ ] **Step 1: Find the existing per-job loop body in `run_agentic_backfill`**

Read `agentic_enricher.py` and locate the inner loop body. Extract it into a function:

```python
def enrich_one_job(job_row: dict, conn: Any, config: dict) -> dict:
    """Single-job entry point for the agentic enricher.

    Returns dict with any of: jd_full, salary_min, salary_max.
    Empty dict if no JD found.
    """
    # ... extracted body ...
```

- [ ] **Step 2: Update `run_agentic_backfill` to call `enrich_one_job` per row**

Make `run_agentic_backfill` a thin loop over `enrich_one_job`. Preserves existing batch behavior.

- [ ] **Step 3: Update `data_enricher.enrich_job()` agentic branch to call `enrich_one_job`**

```python
if start_idx <= TIER_ORDER.index("agentic") and jd_still_missing:
    from job_finder.web.agentic_enricher import enrich_one_job
    result = enrich_one_job(job_row, conn, config)
    if result.get("jd_full") and len(result["jd_full"]) >= MIN_FETCH_JD_CHARS:
        # ... persist ...
```

Define `MIN_FETCH_JD_CHARS = 200` near the top of `data_enricher.py` (real fetched pages are at least this size; below this is auth-wall noise that slipped past `is_short_auth_page`).

- [ ] **Step 4: Add test for the new single-job entry**

```python
def test_enrich_one_job_returns_dict_with_jd_full_when_fetch_succeeds(monkeypatch):
    from job_finder.web.agentic_enricher import enrich_one_job
    # Mock the inner ollama/playwright calls; assert dict shape
    # (mocking pattern depends on existing agentic_enricher test file)
```

- [ ] **Step 5: Run tests**

```bash
uv run --active pytest tests/test_data_enricher.py tests/test_agentic_enricher.py -q --tb=short
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/agentic_enricher.py job_finder/web/data_enricher.py tests/test_agentic_enricher.py
git commit -m "$(cat <<'EOF'
feat(enrichment): add per-job entry point for agentic enricher

Extracts enrich_one_job() from run_agentic_backfill's inner loop
so the synchronous cascade in data_enricher.enrich_job() can invoke
agentic enrichment as its deepest tier without going through batch
infrastructure.

Phase 2b sub-fix 2/2.
EOF
)"
```

### Task 2b.4: Delete `extract_with_haiku` and `extract_with_sonnet`

**Files:**
- Modify: `job_finder/web/enrichment_tiers.py` — delete the two functions
- Modify: any test file that imports them — delete dead tests

- [ ] **Step 1: Find all imports of these symbols**

```bash
grep -rn "extract_with_haiku\|extract_with_sonnet" job_finder/ tests/ --include='*.py'
```

- [ ] **Step 2: Delete the two functions and their schemas**

In `enrichment_tiers.py`:
- Delete `_ENRICH_HAIKU_SCHEMA` (lines 43-52)
- Delete `_ENRICH_SONNET_SCHEMA` (lines 54-62)
- Delete `extract_with_haiku()` function body (~lines 542-650)
- Delete `extract_with_sonnet()` function body (~lines 306-420)

- [ ] **Step 3: Delete tests that exercise the deleted functions**

In any test file (likely `tests/test_enrichment_tiers.py` if it exists), remove tests that import or call `extract_with_haiku` or `extract_with_sonnet`.

- [ ] **Step 4: Run full enrichment test suite**

```bash
uv run --active pytest tests/test_enrichment_tiers.py tests/test_data_enricher.py -q --tb=short
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/enrichment_tiers.py tests/test_enrichment_tiers.py
git commit -m "$(cat <<'EOF'
refactor(enrichment): delete extract_with_haiku and extract_with_sonnet

Synthesis tiers removed from the cascade in 2b.2; the functions
themselves (and their schemas, tests) are now dead code. ~310 lines
deleted from enrichment_tiers.py.

Phase 2b complete. Spec D-2.4.
EOF
)"
```

---

## Sub-fix 2c: Post-fetch Structured-Field Extraction

### Task 2c.1: Write failing tests for `parse_structured_fields` (TDD)

**Files:**
- Create: `tests/test_parse_structured_fields.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for parse_structured_fields() — Haiku extraction of salary/location
from a fully-fetched jd_full, post-cascade."""

from unittest.mock import MagicMock
from job_finder.web.enrichment_tiers import parse_structured_fields


def test_extracts_salary_range_from_text(monkeypatch):
    fake_call = MagicMock(return_value=MagicMock(
        data={"salary_min": 150000, "salary_max": 200000, "location": "Remote US"},
        schema_valid=True,
    ))
    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    out = parse_structured_fields(
        jd_full="...The salary range is $150,000 - $200,000...",
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    assert out == {"salary_min": 150000, "salary_max": 200000, "location": "Remote US"}


def test_does_not_emit_jd_full_field(monkeypatch):
    """Schema MUST NOT include jd_full — the model cannot summarize the description."""
    from job_finder.web.enrichment_tiers import _STRUCTURED_FIELDS_SCHEMA
    assert "jd_full" not in _STRUCTURED_FIELDS_SCHEMA["properties"]


def test_returns_empty_dict_on_no_signal(monkeypatch):
    fake_call = MagicMock(return_value=MagicMock(data={}, schema_valid=True))
    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    out = parse_structured_fields(
        jd_full="A short description with no salary mentioned",
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    assert out == {}


def test_runs_on_full_jd_not_truncated_fragments(monkeypatch):
    """Confirm we send the full jd_full, not a truncated 2000-char prefix."""
    captured = {}

    def fake_call(**kwargs):
        # Concatenate user message contents to verify the full text reached
        msg = kwargs["messages"][0]["content"]
        captured["msg_len"] = len(msg)
        return MagicMock(data={}, schema_valid=True)

    monkeypatch.setattr("job_finder.web.enrichment_tiers.call_model", fake_call)
    long_jd = "Lorem ipsum " * 800  # ~9600 chars
    parse_structured_fields(
        jd_full=long_jd,
        job_row={"dedup_key": "x|y", "title": "T", "company": "C"},
        conn=MagicMock(),
        config={},
    )
    # Allow some prompt overhead but message must include most of the JD
    assert captured["msg_len"] >= 8000
```

- [ ] **Step 2: Run tests and verify they fail**

```bash
uv run --active pytest tests/test_parse_structured_fields.py -q --tb=short
```

Expected: ImportError — `parse_structured_fields` does not exist.

### Task 2c.2: Implement `parse_structured_fields`

**Files:**
- Modify: `job_finder/web/enrichment_tiers.py` — add new function + schema

- [ ] **Step 1: Add the schema and the function**

```python
_STRUCTURED_FIELDS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "salary_min": {"type": "integer"},
        "salary_max": {"type": "integer"},
        "location": {"type": "string"},
    },
}


def parse_structured_fields(
    jd_full: str,
    job_row: dict,
    conn: Any,
    config: dict,
) -> dict:
    """Extract salary and location from a fully-fetched jd_full.

    Runs ONCE post-cascade, on the actual fetched description (no
    fragment truncation). Schema deliberately excludes jd_full so the
    model cannot summarize.
    """
    if not jd_full or len(jd_full) < 200:
        return {}

    title = job_row.get("title", "")
    company = job_row.get("company", "")
    job_id = job_row.get("dedup_key")

    system_prompt = (
        "You extract structured fields from a job description. "
        "Return ONLY a JSON object with optional fields: "
        "salary_min (integer USD annual), salary_max (integer USD annual), "
        "location (string). Omit fields that cannot be determined. "
        "Do not invent data."
    )
    user_prompt = (
        f"Job: {title} at {company}\n\n"
        f"Description:\n{jd_full}\n\n"
        f"Extract structured fields as JSON. Include only fields explicitly mentioned."
    )

    try:
        result = call_model(
            tier="haiku",  # cheap; structured-extraction task
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=_STRUCTURED_FIELDS_SCHEMA,
            job_id=job_id,
            purpose="parse_structured_fields",
            max_tokens=256,
        )
    except Exception as exc:
        logger.warning("parse_structured_fields: error for %s: %s", job_id, exc)
        return {}

    if not result.data or not result.schema_valid:
        return {}

    out = {}
    for k in ("salary_min", "salary_max", "location"):
        v = result.data.get(k)
        if v is not None:
            out[k] = v
    return out
```

- [ ] **Step 2: Run tests and verify they pass**

```bash
uv run --active pytest tests/test_parse_structured_fields.py -q --tb=short
```

Expected: 4 passed.

### Task 2c.3: Wire `parse_structured_fields` into `enrich_job`

**Files:**
- Modify: `job_finder/web/data_enricher.py:81+` — call `parse_structured_fields` after `jd_full` is populated by any cascade tier

- [ ] **Step 1: Insert post-fetch call**

After the cascade loop in `enrich_job()`, before return:

```python
# Post-fetch structured-field extraction (single Haiku call, full JD)
if jd_full_now_present and (salary_min_missing or salary_max_missing or location_missing):
    fields = parse_structured_fields(
        jd_full=current_jd_full,
        job_row=job_row,
        conn=conn,
        config=config,
    )
    # Merge fields into the persisted result (don't overwrite existing values)
    for k, v in fields.items():
        if not job_row.get(k):  # only fill empty
            persisted[k] = v
```

(Adapt to actual variable names in `enrich_job`.)

- [ ] **Step 2: Add integration test**

```python
def test_enrich_job_runs_parse_structured_fields_after_fetch(monkeypatch):
    """Verify parse_structured_fields is called once after jd_full is populated."""
    # ... setup; mock fetch_direct_jd to return a real JD; mock parse_structured_fields ...
```

- [ ] **Step 3: Run test and verify**

```bash
uv run --active pytest tests/test_data_enricher.py tests/test_parse_structured_fields.py -q --tb=short
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/enrichment_tiers.py job_finder/web/data_enricher.py tests/test_parse_structured_fields.py tests/test_data_enricher.py
git commit -m "$(cat <<'EOF'
feat(enrichment): post-fetch structured-field extraction (salary, location)

parse_structured_fields() runs once after a successful cascade fetch,
on the actual fetched jd_full (no truncation). Replaces the salary-
extraction side-effect of the deleted Haiku/Sonnet synthesis tiers.

Schema deliberately excludes jd_full so the model cannot summarize.

Phase 2c complete.
EOF
)"
```

---

## Sub-fix 2d: `low_signal` Classification

### Task 2d.1: Migration for extended classification CHECK constraint

**Files:**
- Modify: `job_finder/web/db_migrate.py` — append migration 42

- [ ] **Step 1: Inspect current migration list and current schema**

```bash
grep -n "user_version\|MIGRATIONS\|CHECK" job_finder/web/db_migrate.py | head -30
```

Note current `user_version` (should be 41 per memory).

- [ ] **Step 2: Append migration 42**

SQLite does not support altering a CHECK constraint in place; the canonical pattern is rebuild-the-table. But since the existing `classification` column may not have a CHECK constraint at all (text column accepts any value), check first:

```bash
uv run python -c "import sqlite3; conn = sqlite3.connect('jobs.db'); print([r for r in conn.execute(\"SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'\").fetchall()])"
```

If `classification` is a plain TEXT column with no CHECK, migration 42 is a no-op DDL change at the schema-doc level — just bump `user_version` and document the new allowed enum values.

If there IS a CHECK constraint, the migration rebuilds the column:

```python
# Migration 42: extend classification enum to include 'low_signal'
"""ALTER TABLE jobs RENAME TO jobs_old_42""",
"""CREATE TABLE jobs (... full schema with classification CHECK including low_signal ...)""",
"""INSERT INTO jobs SELECT * FROM jobs_old_42""",
"""DROP TABLE jobs_old_42""",
```

(See `db_migrate.py` for the project's pattern of multi-statement migrations as a list of strings.)

- [ ] **Step 3: Bump `user_version` to 42**

In `db_migrate.py`, ensure the migration applies and `PRAGMA user_version = 42` after.

- [ ] **Step 4: Run migration test**

```bash
uv run --active pytest tests/test_db_migrate.py -q --tb=short
```

Expected: green; new migration applies cleanly to a fresh DB and an existing-at-v41 DB.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/db_migrate.py tests/test_db_migrate.py
git commit -m "$(cat <<'EOF'
feat(db): migration 42 — extend classification enum with low_signal

New 5th classification value distinct from apply/consider/skip/reject.
Surfaces genuinely-no-signal jobs (enrichment exhausted, jd_full short)
honestly instead of rolling them into the apply distribution.

Phase 2d sub-fix 1/4. Spec D-2.5.
EOF
)"
```

### Task 2d.2: Update `derive_classification` to emit `low_signal`

**Files:**
- Modify: `job_finder/db.py:57-90` — add low_signal branch
- Modify: `job_finder/db.py` — `persist_job_assessment` call site needs access to `enrichment_tier` and `jd_full` length

- [ ] **Step 1: Write failing test**

Create `tests/test_low_signal_classification.py`:

```python
"""Tests for the low_signal classification rule."""

from job_finder.db import derive_classification


def test_low_signal_when_exhausted_and_short_jd():
    sub_scores = {"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3}
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


def test_not_low_signal_when_jd_long_enough():
    sub_scores = {"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3}
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=5000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


def test_not_low_signal_when_enrichment_not_exhausted():
    """Short JD with enrichment_tier=NULL is a re-enrichment candidate, not low_signal."""
    sub_scores = {"title_fit": 3, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3}
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "apply"  # standard rule applies; enrichment can still run


def test_legitimacy_note_overrides_low_signal():
    sub_scores = {k: 3 for k in ("title_fit", "location_fit", "comp_fit",
                                 "domain_match", "seniority_match", "skills_match")}
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note="scam pattern",
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "reject"


def test_any_axis_one_after_low_signal_check_does_not_promote():
    """If a job is low_signal, that wins over the any-1 reject path."""
    sub_scores = {"title_fit": 1, "location_fit": 3, "comp_fit": 3,
                  "domain_match": 3, "seniority_match": 3, "skills_match": 3}
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    # Spec D-2.5: low_signal sits between legitimacy and any-axis-1; this is reject
    # because any-axis-1 → reject and low_signal can't override that.
    # WAIT — spec says rule precedence: legitimacy → reject; low_signal check → low_signal;
    # any axis 1 → reject. So low_signal wins over any-1.
    assert cls == "low_signal"
```

- [ ] **Step 2: Run test and verify failure**

```bash
uv run --active pytest tests/test_low_signal_classification.py -q --tb=short
```

Expected: TypeError on the new keyword arguments.

- [ ] **Step 3: Update `derive_classification` signature and body**

In `job_finder/db.py:57-90`:

```python
def derive_classification(
    sub_scores: dict,
    legitimacy_note: str | None,
    enrichment_tier: str | None = None,
    jd_full_length: int = 0,
    low_signal_threshold: int = 1500,
) -> str:
    """Python-derived 5-way classification per spec D-2.5.

    Rule order (precedence):
      1. legitimacy_note truthy            -> 'reject'
      2. enrichment exhausted + short jd   -> 'low_signal'
      3. any sub-score == 1                -> 'reject'
      4. all sub-scores >= 3               -> 'apply'
      5. all sub-scores >= 2               -> 'consider'
      6. otherwise                         -> 'skip'
    """
    if legitimacy_note:
        return "reject"
    if enrichment_tier == "exhausted" and jd_full_length < low_signal_threshold:
        return "low_signal"
    if any(v == 1 for v in sub_scores.values()):
        return "reject"
    if all(v >= 3 for v in sub_scores.values()):
        return "apply"
    if all(v >= 2 for v in sub_scores.values()):
        return "consider"
    return "skip"
```

- [ ] **Step 4: Update `persist_job_assessment` to pass the new args**

Find `persist_job_assessment` in `db.py`. It currently calls `derive_classification(sub_scores, legitimacy_note)`. It needs to also fetch `enrichment_tier` and `length(jd_full)` for the row, plus read `scoring.low_signal_jd_chars` from config.

- [ ] **Step 5: Run all tests, verify green**

```bash
uv run --active pytest tests/test_low_signal_classification.py tests/test_db.py -q --tb=short
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add job_finder/db.py tests/test_low_signal_classification.py
git commit -m "$(cat <<'EOF'
feat(scoring): low_signal classification rule

derive_classification gains a low_signal branch: when enrichment
is exhausted AND jd_full is below threshold (default 1500 chars),
the row is classified low_signal instead of being rolled into
apply/consider/skip via the rubric outputs (which are unreliable
when the model has no real description to read).

Rule precedence (per spec D-2.5):
  1. legitimacy → reject
  2. exhausted + short → low_signal
  3. any axis=1 → reject
  4. all ≥3 → apply
  5. all ≥2 → consider
  6. else → skip

Phase 2d sub-fix 2/4.
EOF
)"
```

### Task 2d.3: Add config knob for `low_signal_jd_chars`

**Files:**
- Modify: `config.example.yaml` — add `scoring.low_signal_jd_chars: 1500`
- Modify: `config.yaml` — same (Edit tool, NOT Write — surgical only per CLAUDE.md)

- [ ] **Step 1: Add the knob to the example config**

```bash
grep -n "scoring:" config.example.yaml
```

Use `Edit` tool to insert `  low_signal_jd_chars: 1500` under the `scoring:` block, near `min_score_threshold`.

- [ ] **Step 2: Add the knob to the live config**

CRITICAL: `config.yaml` must be modified with the **Edit tool only** (never Write — see CLAUDE.md). Use Edit to insert the same line under `scoring:`.

- [ ] **Step 3: Wire knob into `persist_job_assessment` callers**

`derive_classification` reads the threshold as a kwarg; the caller (`persist_job_assessment` in `db.py`) needs to fetch it from config and pass it. This may require threading config through `persist_job_assessment` if it's not already there. Check the signature; thread `config` if needed.

- [ ] **Step 4: Run tests**

```bash
uv run --active pytest tests/test_db.py tests/test_low_signal_classification.py -q --tb=short
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add config.example.yaml config.yaml job_finder/db.py
git commit -m "$(cat <<'EOF'
feat(config): scoring.low_signal_jd_chars knob (default 1500)

Threshold below which an exhausted-enrichment job is classified
low_signal instead of via the standard rubric output. Configurable
because empirical verification by sampling borderline (1000-2000 char)
jobs may shift the right value.

Phase 2d sub-fix 3/4.
EOF
)"
```

### Task 2d.4: Score-cell template renders `low_signal` color

**Files:**
- Modify: `job_finder/web/templates/jobs/_score_cell.html:28-43`
- Modify: `tests/test_views.py` — extend `TestCompositeScoreCell` with a low_signal case

- [ ] **Step 1: Write failing test**

Add to `tests/test_views.py` in `TestCompositeScoreCell`:

```python
def test_score_cell_renders_low_signal_in_muted_gray(self, app_with_scored_jobs):
    """A row with classification='low_signal' shows a distinct color."""
    # ... fixture extension to include a low_signal row ...
    client = app_with_scored_jobs.test_client()
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    # low_signal class should be present and distinct from skip's slate-400
    assert "text-zinc-500" in body or "text-gray-500" in body, "low_signal needs a distinct muted color"
```

- [ ] **Step 2: Run test and verify failure**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -k low_signal -q --tb=short
```

- [ ] **Step 3: Add the branch in `_score_cell.html`**

In `_score_cell.html:28-43` add:

```jinja
{% elif cls == 'low_signal' %}
  {% set color_class = 'text-zinc-500' %}
  {% set rank = 0 %}  {# below skip; appears at bottom of sort order #}
```

(Pick a `rank` that puts low_signal below `skip` in sort order — these jobs need attention but aren't actionable.)

- [ ] **Step 4: Run test and verify pass**

```bash
uv run --active pytest tests/test_views.py::TestCompositeScoreCell -q --tb=short
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/templates/jobs/_score_cell.html tests/test_views.py
git commit -m "$(cat <<'EOF'
feat(ui): low_signal classification gets distinct muted color

Score-cell renders low_signal rows in zinc-500 (distinct from skip's
slate-400). Sort rank below skip — these jobs need re-enrichment, not
action.

Phase 2d sub-fix 4/4. Phase 2d complete. Spec D-2.5.
EOF
)"
```

---

## Sub-fix 2e: Backfill Stuck-at-Haiku Rows

### Task 2e.1: Write the backfill script

**Files:**
- Create: `scripts/backfill_stuck_at_haiku.py`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""One-shot backfill: reset enrichment_tier for rows stuck on a stale tier
so they re-flow through the new (synthesis-free) cascade.

Targets rows with enrichment_tier IN ('haiku', 'free', 'ddg') AND short jd_full.
After this script runs, the next enrichment cycle picks them up at NULL
(start of cascade) and fetches a real JD if available, or honestly marks
them exhausted.

Usage:
    uv run python scripts/backfill_stuck_at_haiku.py [--dry-run]
"""

import argparse
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--threshold", type=int, default=1500,
                        help="JD-length threshold; rows below this with stale tier get reset")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT enrichment_tier, COUNT(*) AS n
        FROM jobs
        WHERE enrichment_tier IN ('haiku', 'free', 'ddg')
          AND length(jd_full) < ?
        GROUP BY enrichment_tier
    """, (args.threshold,)).fetchall()

    total = sum(r["n"] for r in rows)
    print(f"Rows to reset (jd_full < {args.threshold} chars):")
    for r in rows:
        print(f"  {r['enrichment_tier']:<10} {r['n']:>5}")
    print(f"  {'total':<10} {total:>5}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    if total == 0:
        print("\nNo rows to reset; exiting.")
        return 0

    confirm = input(f"\nReset enrichment_tier=NULL for {total} rows? [yes/N] ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return 1

    cur = conn.execute("""
        UPDATE jobs
        SET enrichment_tier = NULL
        WHERE enrichment_tier IN ('haiku', 'free', 'ddg')
          AND length(jd_full) < ?
    """, (args.threshold,))
    conn.commit()
    print(f"Reset {cur.rowcount} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Make executable and verify it runs**

```bash
chmod +x scripts/backfill_stuck_at_haiku.py
uv run python scripts/backfill_stuck_at_haiku.py --dry-run
```

Expected: prints the count breakdown without writing. Should show ~1006 rows at the haiku tier (per RC4 diagnostic data).

- [ ] **Step 3: Commit (do not run for real yet)**

```bash
git add scripts/backfill_stuck_at_haiku.py
git commit -m "$(cat <<'EOF'
chore(scripts): one-shot backfill for stuck-at-haiku rows

Resets enrichment_tier=NULL for rows with stale tiers (haiku/free/ddg)
and short jd_full, so they re-flow through the new synthesis-free
cascade. Manual confirmation required; --dry-run for preview.

Run AFTER 2b lands and the new cascade is verified on a small sample.
DO NOT run automatically — invoke manually.

Phase 2e. Spec D-2.7.
EOF
)"
```

### Task 2e.2: Verify cascade on a small sample, then execute backfill

**Files:**
- (Manual checkpoint — no file changes)

- [ ] **Step 1: Pick 5 stuck-at-haiku rows for sample verification**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
rows = conn.execute('''
    SELECT dedup_key, title, company, length(jd_full) AS jd_len
    FROM jobs
    WHERE enrichment_tier = \"haiku\" AND length(jd_full) < 1500
    ORDER BY RANDOM()
    LIMIT 5
''').fetchall()
for r in rows: print(r)
"
```

- [ ] **Step 2: Reset just those 5 rows manually and run enrichment**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
keys = [...]  # paste the 5 dedup_keys from Step 1
for k in keys:
    conn.execute('UPDATE jobs SET enrichment_tier = NULL WHERE dedup_key = ?', (k,))
conn.commit()
"
# Then trigger enrichment for those rows via the web UI or CLI
```

Inspect the resulting `jd_full` values. If they look like real JDs (or the rows correctly land at `enrichment_tier='exhausted'` with no fake-fill), proceed.

- [ ] **Step 3: Run the full backfill**

```bash
uv run python scripts/backfill_stuck_at_haiku.py
# Confirm with "yes" when prompted
```

- [ ] **Step 4: Trigger enrichment for the now-NULL rows**

The nightly agentic backfill (3:30 AM, per memory) picks them up. Or run manually via the web UI's "rerun enrichment" button. Whichever path matches the project's existing operator workflow.

- [ ] **Step 5: After enrichment completes, verify distribution**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
print('Post-backfill enrichment_tier distribution:')
for r in conn.execute('SELECT enrichment_tier, COUNT(*) FROM jobs GROUP BY enrichment_tier').fetchall():
    print(f'  {r[0]:<15} {r[1]}')
"
```

Expected: significantly fewer rows at `haiku` (likely zero, since that tier no longer exists in the cascade); more rows at `serpapi` / `agentic` / `exhausted`.

---

## Acceptance criteria for Phase 2

- [ ] All 5 sub-fixes (2a–2e) committed with green tests at each commit boundary
- [ ] `derive_classification` emits the 5 expected values: `apply`, `consider`, `skip`, `reject`, `low_signal`
- [ ] `enrich_job()` cascade contains no Haiku or Sonnet synthesis tiers
- [ ] `parse_structured_fields` is the only LLM call in the enrichment pipeline; runs once post-fetch
- [ ] Profile reaches the scorer end-to-end: scoring a job with the 3 anchor cases (Vera/Latent/DeepMind) shows the merged candidate context in the system prompt (verifiable by adding a temporary debug log)
- [ ] Migration 42 applies cleanly to a fresh DB (`pytest tests/test_db_migrate.py`)
- [ ] Score-cell template renders `low_signal` rows in distinct muted color
- [ ] Backfill script runs cleanly in --dry-run; full run executed manually after sample verification
- [ ] Full test suite green: `uv run --active pytest tests/ -q --tb=short`

## What this unlocks

After Phase 2, the scorer is *informed* (RC1+RC2 fixed), the cascade *fetches* (RC4 fixed), and uncertainty is *surfaced honestly* (low_signal addresses RC3's downstream symptom). The structural rubric problem (RC3 itself — default-to-3 + all-≥3-apply) is **not** fixed in Phase 2; that's Phase 4's domain. But Phase 3 can now build the gold set against an *informed* scorer, so labels reflect calibration error rather than bug noise.

## Out of scope for this plan

- Few-shot example rewriting (Phase 4)
- Rubric structural changes (Phase 4)
- Eval harness (Phase 5)
- Wholesale re-score (Phase 6)
- `low_signal` UI affordance beyond color (badge + manual re-enrichment button) — separate trivial follow-up
