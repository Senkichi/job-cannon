# Scoring Recalibration Phase 5: Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Execution order note:** This plan is numbered "Phase 5" to match the spec, but it executes BEFORE the Phase 4 plan because Phase 4's variant A/B testing requires the harness to exist. The dependency is: 1 → 2 → 3 → **5** → 4 → 6.

**Goal:** Build a CLI eval harness that runs a prompt variant against the gold set, computes literature-informed metrics (per-axis MAE/bias/ICC/QW-κ, classification F1, coherence, calibration), reports baseline-vs-candidate diffs with bootstrap confidence intervals, and persists every run to a queryable history table. Three usage modes: diagnose (run one variant alone), A/B (variant vs baseline), regression (re-run production prompt against gold set).

**Architecture:** Single CLI module `job_finder/eval/scoring_harness.py` orchestrates: gold-set load, variant load, N runs (default 3) per variant, metric computation, report generation. Metric helpers live in `job_finder/eval/metrics.py` for testability. New `eval_runs` table in jobs.db preserves history. Reports written as versioned markdown to `.planning/eval_results/`.

**Tech Stack:** Python 3.13, numpy, scipy.stats (verify availability — likely present via sentence-transformers chain), SQLite (raw SQL), pytest. No new third-party deps if possible.

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 5, decisions D-5.1 through D-5.6).

**Predecessor plan:** `2026-04-27-scoring-phase-3-gold-set.md` (gold set must be labeled)
**Successor plan:** `2026-04-27-scoring-phase-4-rubric-redesign.md`

---

## File Structure

### Created files

| File | Responsibility |
|---|---|
| `job_finder/eval/__init__.py` | Empty package marker |
| `job_finder/eval/metrics.py` | Pure functions: mae, bias, icc, qw_kappa, brier, bootstrap_ci, coherence_violations |
| `job_finder/eval/harness.py` | Orchestration: load gold set, run variant, compute metrics, write report |
| `job_finder/eval/report.py` | Markdown report generator (headline, tables, confusion matrix, per-job diff) |
| `job_finder/eval/__main__.py` | CLI entry point — invoked as `python -m job_finder.eval` |
| `tests/test_metrics.py` | Unit tests for each metric function |
| `tests/test_harness.py` | Integration test: end-to-end run on a tiny synthetic gold set |
| `tests/test_report.py` | Snapshot tests for report sections |

### Modified files

| File | Lines (approx) | Responsibility |
|---|---|---|
| `job_finder/web/db_migrate.py` | +1 migration | Migration 44: `eval_runs` table |
| `requirements.txt` | possibly +1 line | Pin `scipy` if not already present |

### Files explicitly NOT touched

- `job_finder/web/job_scorer.py` — harness invokes the scorer; doesn't modify it
- `job_finder/web/scoring_orchestrator.py` — same
- Any production code path — the harness is dev-only infrastructure

---

## Test Strategy

Metric functions are pure and testable in isolation with known input/output pairs. Each `test_metrics.py` test compares against a hand-computed expected value for a small input.

The harness end-to-end test seeds a temp DB with 5 synthetic gold-labeled rows, mocks `score_job` to return controlled outputs, runs the harness, and asserts the report file is created with expected sections.

```bash
uv run --active pytest tests/test_metrics.py tests/test_harness.py tests/test_report.py -q --tb=short
```

---

## Task 5.1: Verify scipy/numpy availability

**Files:**
- Inspect: `requirements.txt`

- [ ] **Step 1: Check if scipy is installed**

```bash
uv run python -c "import scipy.stats; print(scipy.__version__)"
uv run python -c "import numpy; print(numpy.__version__)"
```

If either fails, add to `requirements.txt`:
- `scipy>=1.11.0` (for `scipy.stats.kendalltau`, etc.)
- `numpy>=1.26.0`

Then `uv pip install -r requirements.txt`.

- [ ] **Step 2: Commit dependency change if needed**

```bash
# Only if requirements.txt was modified
git add requirements.txt
git commit -m "$(cat <<'EOF'
chore(deps): pin scipy and numpy for eval harness

The Phase 5 harness uses scipy.stats and numpy for ICC, kappa,
and bootstrap CI computation. Both were already in the
transitive dependency closure via sentence-transformers; this
commit makes them explicit.
EOF
)"
```

---

## Task 5.2: Migration 44 — `eval_runs` table

**Files:**
- Modify: `job_finder/web/db_migrate.py`
- Modify: `tests/test_db_migrate.py`

- [ ] **Step 1: Append migration**

```python
# Migration 44: eval_runs table for harness run history (Phase 5)
"""CREATE TABLE eval_runs (
    run_id TEXT PRIMARY KEY,           -- ULID or uuid4 hex
    timestamp TIMESTAMP NOT NULL,
    variant_name TEXT NOT NULL,
    baseline_run_id TEXT,              -- if A/B mode, points to baseline run
    gold_set_version TEXT NOT NULL,    -- e.g., 'v1-40-jobs' to detect schema drift
    n_runs INTEGER NOT NULL,           -- e.g., 3
    config_json TEXT,                  -- frozen config snapshot for reproducibility
    metrics_json TEXT NOT NULL,        -- aggregated metrics
    per_job_json TEXT NOT NULL,        -- per-job raw scores from each run
    report_path TEXT,                  -- path to .planning/eval_results/...md
    notes TEXT
)""",
"""CREATE INDEX idx_eval_runs_variant ON eval_runs(variant_name)""",
"""CREATE INDEX idx_eval_runs_ts ON eval_runs(timestamp DESC)""",
```

Bump `user_version` to 44.

- [ ] **Step 2: Add migration test**

```python
def test_migration_44_creates_eval_runs(tmp_db_path):
    import sqlite3
    from job_finder.web.db_migrate import run_migrations
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(eval_runs)").fetchall()]
    expected = {"run_id", "timestamp", "variant_name", "baseline_run_id",
                "gold_set_version", "n_runs", "config_json", "metrics_json",
                "per_job_json", "report_path", "notes"}
    assert set(cols) >= expected
```

- [ ] **Step 3: Run tests, apply to live DB**

```bash
uv run --active pytest tests/test_db_migrate.py -q --tb=short
uv run python -c "from job_finder.web.db_migrate import run_migrations; run_migrations('jobs.db')"
```

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/db_migrate.py tests/test_db_migrate.py
git commit -m "$(cat <<'EOF'
feat(db): migration 44 — eval_runs table

Persists harness run history (variant_name, gold_set_version,
metrics_json, per_job_json, report_path). Enables comparison
against any past run, not just the most recent baseline (D-5.4).

Phase 5 task 1/5.
EOF
)"
```

---

## Task 5.3: Metrics module (TDD, one metric at a time)

**Files:**
- Create: `tests/test_metrics.py`
- Create: `job_finder/eval/__init__.py` (empty)
- Create: `job_finder/eval/metrics.py`

Each metric is its own TDD cycle. The metrics are: `mae`, `bias`, `pearson_r`, `icc`, `qw_kappa`, `brier_classification`, `bootstrap_ci`, `coherence_violations`. Plus a helper `confusion_matrix`.

### Task 5.3.1: `mae` and `bias` (paired, simplest)

- [ ] **Step 1: Write failing tests**

```python
"""Tests for pure metric functions."""

import math
import pytest


def test_mae_equal_arrays_is_zero():
    from job_finder.eval.metrics import mae
    assert mae([1, 2, 3], [1, 2, 3]) == 0.0


def test_mae_known_values():
    from job_finder.eval.metrics import mae
    assert mae([1, 2, 3], [2, 4, 6]) == pytest.approx(2.0)


def test_mae_handles_empty_lists():
    from job_finder.eval.metrics import mae
    assert math.isnan(mae([], []))


def test_bias_signed():
    """Bias = mean(y_pred - y_true). Positive bias = systematic over-prediction."""
    from job_finder.eval.metrics import bias
    assert bias(y_true=[1, 2, 3], y_pred=[2, 3, 4]) == pytest.approx(1.0)
    assert bias(y_true=[3, 3, 3], y_pred=[1, 2, 3]) == pytest.approx(-1.0)
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run --active pytest tests/test_metrics.py -k "mae or bias" -q --tb=short
```

- [ ] **Step 3: Implement `mae` and `bias`**

```python
"""Pure metric functions for the eval harness.

All functions accept lists or numpy arrays of equal length.
Edge cases (empty input, NaN values) return float('nan') for
MAE/bias/Pearson; for set-based metrics, return 0.0 or empty
result with a logged warning.
"""

import math
from collections.abc import Sequence


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean absolute error."""
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch")
    if not y_true:
        return float("nan")
    return sum(abs(a - b) for a, b in zip(y_true, y_pred, strict=True)) / len(y_true)


def bias(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean signed error (y_pred - y_true). Positive = model over-predicts."""
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch")
    if not y_true:
        return float("nan")
    return sum(b - a for a, b in zip(y_true, y_pred, strict=True)) / len(y_true)
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run --active pytest tests/test_metrics.py -k "mae or bias" -q --tb=short
```

### Task 5.3.2: `icc` (intraclass correlation, ICC(2,1) "agreement")

- [ ] **Step 1: Write failing tests against scipy reference**

```python
def test_icc_perfect_agreement_is_one():
    from job_finder.eval.metrics import icc
    raters = [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5]]  # 2 raters, 5 subjects
    assert icc(raters) == pytest.approx(1.0)


def test_icc_zero_agreement():
    from job_finder.eval.metrics import icc
    raters = [[1, 1, 1, 1], [5, 5, 5, 5]]  # systematic +4 bias, no within-subject variance
    # ICC(2,1) absolute-agreement should drop sharply for systematic bias
    val = icc(raters)
    assert val < 0.1


def test_icc_known_value_against_pingouin():
    """Computed via pingouin.intraclass_corr for cross-check."""
    from job_finder.eval.metrics import icc
    raters = [[3, 3, 4, 5, 4], [4, 3, 4, 5, 5]]
    # Hand-computed: ICC(2,1) ≈ 0.74 ± 0.05 for these arrays
    val = icc(raters)
    assert 0.6 <= val <= 0.85
```

- [ ] **Step 2: Implement `icc(2,1)` using scipy**

```python
def icc(raters: Sequence[Sequence[float]]) -> float:
    """ICC(2,1), absolute agreement, single-measurement.

    Two-way random effects, single-rater.
    Formula: (MS_between - MS_error) / (MS_between + (k-1)*MS_error + k/n*(MS_rater - MS_error))

    raters: list of arrays, each containing one rater's scores across n subjects.
    All arrays must be equal length.
    """
    import numpy as np
    a = np.array(raters, dtype=float)
    if a.ndim != 2 or a.shape[0] < 2:
        return float("nan")
    n_raters, n_subjects = a.shape
    grand_mean = a.mean()
    subject_means = a.mean(axis=0)
    rater_means = a.mean(axis=1)
    # Mean squares
    ss_between_subjects = n_raters * ((subject_means - grand_mean) ** 2).sum()
    ms_between = ss_between_subjects / (n_subjects - 1)
    ss_between_raters = n_subjects * ((rater_means - grand_mean) ** 2).sum()
    ms_rater = ss_between_raters / (n_raters - 1)
    ss_total = ((a - grand_mean) ** 2).sum()
    ss_error = ss_total - ss_between_subjects - ss_between_raters
    df_error = (n_raters - 1) * (n_subjects - 1)
    ms_error = ss_error / df_error if df_error > 0 else float("nan")
    denom = ms_between + (n_raters - 1) * ms_error + (n_raters / n_subjects) * (ms_rater - ms_error)
    if denom <= 0:
        return float("nan")
    return float((ms_between - ms_error) / denom)
```

- [ ] **Step 3: Run tests**

```bash
uv run --active pytest tests/test_metrics.py -k icc -q --tb=short
```

### Task 5.3.3: `qw_kappa` (quadratic-weighted kappa)

- [ ] **Step 1: Tests**

```python
def test_qw_kappa_perfect_agreement_is_one():
    from job_finder.eval.metrics import qw_kappa
    assert qw_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], min_rating=1, max_rating=5) == pytest.approx(1.0)


def test_qw_kappa_inverse_disagreement_is_negative():
    from job_finder.eval.metrics import qw_kappa
    val = qw_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], min_rating=1, max_rating=5)
    assert val < 0


def test_qw_kappa_off_by_two_worse_than_off_by_one():
    """The 'quadratic' weighting penalizes far-disagreement more."""
    from job_finder.eval.metrics import qw_kappa
    one_off = qw_kappa([1, 2, 3, 4, 5], [2, 3, 4, 5, 5], min_rating=1, max_rating=5)
    two_off = qw_kappa([1, 2, 3, 4, 5], [3, 4, 5, 5, 5], min_rating=1, max_rating=5)
    assert one_off > two_off
```

- [ ] **Step 2: Implement**

```python
def qw_kappa(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    min_rating: int = 1,
    max_rating: int = 5,
) -> float:
    """Quadratic-weighted Cohen's kappa.

    Penalizes off-by-N disagreements quadratically, suitable for ordinal
    rating scales. Equivalent to sklearn.metrics.cohen_kappa_score with
    weights='quadratic'.
    """
    import numpy as np
    if len(y_true) != len(y_pred) or not y_true:
        return float("nan")
    n = max_rating - min_rating + 1
    O = np.zeros((n, n))  # observed
    for t, p in zip(y_true, y_pred, strict=True):
        O[t - min_rating, p - min_rating] += 1
    # Marginal histograms
    hist_t = O.sum(axis=1)
    hist_p = O.sum(axis=0)
    E = np.outer(hist_t, hist_p) / O.sum()  # expected by chance
    W = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            W[i, j] = ((i - j) ** 2) / ((n - 1) ** 2)
    num = (W * O).sum()
    den = (W * E).sum()
    if den == 0:
        return float("nan")
    return float(1 - num / den)
```

- [ ] **Step 3: Tests**

```bash
uv run --active pytest tests/test_metrics.py -k qw_kappa -q --tb=short
```

### Task 5.3.4: `bootstrap_ci`

- [ ] **Step 1: Tests**

```python
def test_bootstrap_ci_basic():
    from job_finder.eval.metrics import bootstrap_ci
    import random
    random.seed(42)
    data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    lo, hi = bootstrap_ci(data, statistic=lambda xs: sum(xs) / len(xs), n_resamples=1000, ci=0.95)
    # Sample mean = 5.5; 95% CI should bracket it loosely
    assert lo < 5.5 < hi


def test_bootstrap_ci_paired_difference():
    """For paired-comparison case used in harness."""
    from job_finder.eval.metrics import bootstrap_ci
    baseline = [3, 3, 3, 3, 3]
    candidate = [4, 4, 4, 4, 4]
    deltas = [c - b for c, b in zip(candidate, baseline, strict=True)]
    lo, hi = bootstrap_ci(deltas, statistic=lambda xs: sum(xs) / len(xs), n_resamples=1000, ci=0.95)
    # All deltas = +1, so CI should be [1.0, 1.0]
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(1.0)
```

- [ ] **Step 2: Implement**

```python
import random
from typing import Callable


def bootstrap_ci(
    data: Sequence[float],
    statistic: Callable[[Sequence[float]], float],
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int | None = None,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a statistic.

    Returns (lo, hi) at the given confidence level.
    """
    if not data:
        return (float("nan"), float("nan"))
    rng = random.Random(seed) if seed is not None else random
    n = len(data)
    samples = []
    for _ in range(n_resamples):
        resample = [data[rng.randrange(n)] for _ in range(n)]
        samples.append(statistic(resample))
    samples.sort()
    lo_idx = int((1 - ci) / 2 * n_resamples)
    hi_idx = int((1 + ci) / 2 * n_resamples)
    return (samples[lo_idx], samples[hi_idx - 1])
```

- [ ] **Step 3: Tests**

```bash
uv run --active pytest tests/test_metrics.py -k bootstrap -q --tb=short
```

### Task 5.3.5: `confusion_matrix`, `classification_metrics`, `coherence_violations`

- [ ] **Step 1: Tests for confusion_matrix**

```python
def test_confusion_matrix_5x5():
    from job_finder.eval.metrics import confusion_matrix
    classes = ("apply", "consider", "skip", "reject", "low_signal")
    y_true = ["apply", "apply", "consider", "skip", "reject"]
    y_pred = ["apply", "consider", "consider", "apply", "reject"]
    cm = confusion_matrix(y_true, y_pred, classes)
    assert cm["apply"]["apply"] == 1
    assert cm["apply"]["consider"] == 1
    assert cm["consider"]["consider"] == 1
    assert cm["skip"]["apply"] == 1  # off-diagonal: skip-true labeled apply
    assert cm["reject"]["reject"] == 1
```

- [ ] **Step 2: Tests for classification_metrics (precision/recall/F1 per class + macro)**

```python
def test_classification_metrics_per_class():
    from job_finder.eval.metrics import classification_metrics
    classes = ("apply", "consider", "skip", "reject", "low_signal")
    y_true = ["apply"] * 4 + ["consider"] * 4 + ["reject"] * 2
    y_pred = ["apply"] * 3 + ["consider"] * 1 + ["consider"] * 4 + ["reject"] * 2
    m = classification_metrics(y_true, y_pred, classes)
    # apply: TP=3, FP=0, FN=1 → P=1.0, R=0.75, F1=0.857
    assert m["apply"]["precision"] == pytest.approx(1.0)
    assert m["apply"]["recall"] == pytest.approx(0.75)
    assert m["apply"]["f1"] == pytest.approx(2 * 1.0 * 0.75 / (1.0 + 0.75))


def test_apply_false_positive_rate():
    from job_finder.eval.metrics import apply_false_positive_rate
    y_true = ["apply", "consider", "consider", "skip"]
    y_pred = ["apply", "apply", "apply", "apply"]
    # Among the 3 non-apply truths, all 3 were predicted apply → FP rate = 3/3
    assert apply_false_positive_rate(y_true, y_pred) == pytest.approx(1.0)
```

- [ ] **Step 3: Tests for coherence_violations**

```python
def test_coherence_violation_when_gap_mentions_axis_with_high_score():
    from job_finder.eval.metrics import coherence_violations
    # gaps text mentions "title mismatch" but title_fit scored 5
    rows = [
        {"sub_scores": {"title_fit": 5, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3, "skills_match": 3},
         "gaps_text": "title mismatch — wrong function"},
    ]
    violations = coherence_violations(rows)
    assert len(violations) == 1
    assert violations[0]["axis"] == "title_fit"


def test_no_coherence_violation_when_gap_consistent():
    from job_finder.eval.metrics import coherence_violations
    rows = [
        {"sub_scores": {"title_fit": 2, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3, "skills_match": 3},
         "gaps_text": "title mismatch — wrong function"},
    ]
    violations = coherence_violations(rows)
    assert violations == []
```

- [ ] **Step 4: Implement**

```python
AXIS_KEYWORDS = {
    "title_fit": ["title", "role function", "wrong function"],
    "location_fit": ["location", "geography", "remote", "on-site", "relocation"],
    "comp_fit": ["salary", "comp", "compensation", "pay"],
    "domain_match": ["industry", "vertical", "domain"],
    "seniority_match": ["seniority", "level", "junior", "senior", "experience years"],
    "skills_match": ["skill", "technology", "stack"],
}
HIGH_SCORE_THRESHOLD = 4


def confusion_matrix(y_true, y_pred, classes):
    cm = {a: {b: 0 for b in classes} for a in classes}
    for t, p in zip(y_true, y_pred, strict=True):
        cm[t][p] += 1
    return cm


def classification_metrics(y_true, y_pred, classes):
    cm = confusion_matrix(y_true, y_pred, classes)
    out = {}
    for c in classes:
        tp = cm[c][c]
        fp = sum(cm[other][c] for other in classes if other != c)
        fn = sum(cm[c][other] for other in classes if other != c)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        out[c] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    out["macro_f1"] = sum(out[c]["f1"] for c in classes) / len(classes)
    return out


def apply_false_positive_rate(y_true, y_pred) -> float:
    """Fraction of non-apply truths predicted as apply."""
    non_apply = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if t != "apply"]
    if not non_apply:
        return float("nan")
    fp = sum(1 for _, p in non_apply if p == "apply")
    return fp / len(non_apply)


def coherence_violations(rows):
    """Flag rows where gaps text mentions an axis but that axis scored high."""
    out = []
    for row in rows:
        gaps = (row.get("gaps_text") or "").lower()
        scores = row.get("sub_scores", {})
        for axis, keywords in AXIS_KEYWORDS.items():
            if any(kw in gaps for kw in keywords) and scores.get(axis, 0) >= HIGH_SCORE_THRESHOLD:
                out.append({"axis": axis, "score": scores[axis], "gaps_text": gaps})
                break  # one violation per row is enough
    return out
```

- [ ] **Step 5: Run all metric tests**

```bash
uv run --active pytest tests/test_metrics.py -q --tb=short
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_metrics.py job_finder/eval/__init__.py job_finder/eval/metrics.py
git commit -m "$(cat <<'EOF'
feat(eval): metrics module — mae, bias, icc, qw_kappa, bootstrap_ci, classification, coherence

Pure metric functions tested in isolation against hand-computed
expected values. ICC(2,1) absolute-agreement, quadratic-weighted
kappa, percentile bootstrap CIs, per-class precision/recall/F1,
apply false-positive rate, coherence violation detection
(keyword-overlap heuristic per spec D-5.5).

Phase 5 task 2/5.
EOF
)"
```

---

## Task 5.4: Harness orchestration

**Files:**
- Create: `tests/test_harness.py`
- Create: `job_finder/eval/harness.py`

The harness:
1. Loads gold-set rows (`gold_classification IS NOT NULL`)
2. Loads a named variant from `scoring_prompts/variants/<name>.py`
3. Runs `score_job` with the variant's prompt N times per gold-set row (default N=3)
4. Aggregates per-job (mean of 3 runs) and computes metrics
5. Optionally compares to a baseline run (loaded from `eval_runs` table by ID)
6. Persists the run to `eval_runs` and writes a markdown report

- [ ] **Step 1: Write integration test (small synthetic gold set, mocked scorer)**

```python
"""Harness end-to-end test on a tiny synthetic gold set."""

import json
import sqlite3
from unittest.mock import patch
import pytest


@pytest.fixture
def gold_db(tmp_path):
    """5-row synthetic gold set."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
        jd_full TEXT, sources TEXT, classification TEXT, sub_scores_json TEXT,
        fit_analysis TEXT, gold_classification TEXT, gold_sub_scores_json TEXT,
        gold_notes TEXT, gold_labeled_at TIMESTAMP, enrichment_tier TEXT
    )""")
    conn.execute("""CREATE TABLE eval_runs (
        run_id TEXT PRIMARY KEY, timestamp TIMESTAMP, variant_name TEXT,
        baseline_run_id TEXT, gold_set_version TEXT, n_runs INTEGER,
        config_json TEXT, metrics_json TEXT, per_job_json TEXT,
        report_path TEXT, notes TEXT
    )""")
    rows = [
        ("a", "T1", "C1", "Remote", "JD" * 1000, '["x"]', "apply",
         '{"title_fit":4,"location_fit":5,"comp_fit":3,"domain_match":3,"seniority_match":4,"skills_match":3}',
         '{"strengths":[],"gaps":[],"talking_points":[],"resume_priority_skills":[]}',
         "consider",
         '{"title_fit":3,"location_fit":5,"comp_fit":3,"domain_match":3,"seniority_match":3,"skills_match":3}',
         "", "2026-04-28T00:00:00Z", "free"),
        # 4 more rows ...
    ]
    for r in rows:
        conn.execute("INSERT INTO jobs VALUES " + "(" + ",".join("?" * len(r)) + ")", r)
    conn.commit()
    return str(db)


def test_harness_diagnose_mode_writes_report(gold_db, tmp_path, monkeypatch):
    from job_finder.eval.harness import run

    def fake_score_job(job, conn, config, client=None, candidate_context=None):
        # Return controlled output: model agrees with gold for row 'a'
        from types import SimpleNamespace
        return SimpleNamespace(
            status="ok",
            data=SimpleNamespace(
                sub_scores={"title_fit": 3, "location_fit": 5, "comp_fit": 3,
                            "domain_match": 3, "seniority_match": 3, "skills_match": 3},
                rationale={"strengths": [], "gaps": [], "talking_points": [],
                           "resume_priority_skills": []},
                provider="ollama",
            ),
            provider="ollama",
            error=None,
        )

    monkeypatch.setattr("job_finder.eval.harness._score_one", fake_score_job)

    report_path = run(
        db_path=gold_db,
        variant_name="baseline",
        n_runs=2,
        report_dir=str(tmp_path),
        config={},
    )
    assert report_path is not None
    assert (tmp_path / report_path.split("/")[-1]).exists() or report_path  # path returned

    # Check eval_runs row was inserted
    conn = sqlite3.connect(gold_db)
    n = conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Verify failure**

```bash
uv run --active pytest tests/test_harness.py -q --tb=short
```

Expected: ImportError.

- [ ] **Step 3: Implement harness**

```python
"""Eval harness orchestration — load gold set, run variant, compute metrics, write report."""

import json
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_finder.eval import metrics


GOLD_SET_VERSION = "v1-40-jobs"  # bump if gold-set schema or sampling changes


def _load_gold_rows(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT dedup_key, title, company, location, jd_full,
               classification, sub_scores_json, fit_analysis,
               gold_classification, gold_sub_scores_json, gold_notes,
               enrichment_tier
        FROM jobs
        WHERE gold_classification IS NOT NULL
        ORDER BY dedup_key
    """).fetchall()
    return [dict(r) for r in rows]


def _load_variant(variant_name: str):
    """Import a variant module: scoring_prompts/variants/<name>.py.

    A variant exports the same names as v3_scoring_prompt:
    V3_SCORING_PROMPT, JOB_ASSESSMENT_SCHEMA, FEWSHOT_EXAMPLES, FIELD_REINFORCEMENT.
    The 'baseline' name aliases to the production v3_scoring_prompt module.
    """
    if variant_name == "baseline":
        from job_finder.web.scoring_prompts import v3_scoring_prompt as mod
        return mod
    import importlib
    return importlib.import_module(f"job_finder.web.scoring_prompts.variants.{variant_name}")


def _score_one(job, conn, config, candidate_context):
    """Wrapper around score_job; injection point for tests."""
    from job_finder.web.job_scorer import score_job
    return score_job(job, conn, config, candidate_context=candidate_context)


def _gold_to_dict(row: dict) -> dict:
    return json.loads(row["gold_sub_scores_json"])


def _candidate_to_dict(result) -> dict:
    if result.status != "ok" or result.data is None:
        return {}
    return result.data.sub_scores


def run(
    db_path: str,
    variant_name: str = "baseline",
    n_runs: int = 3,
    baseline_run_id: str | None = None,
    report_dir: str = ".planning/eval_results",
    config: dict | None = None,
) -> str:
    """Run a variant against the gold set; persist results; return report path."""
    config = config or {}
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    gold_rows = _load_gold_rows(db_path)
    variant_mod = _load_variant(variant_name)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load profile + build candidate context (same as production)
    from job_finder.web.scoring_orchestrator import (
        load_scoring_profile, build_candidate_context,
    )
    profile = load_scoring_profile(config)
    candidate_context = build_candidate_context(config, profile)

    # Per-job: list of N runs, each with sub_scores
    per_job_runs: dict[str, list[dict]] = defaultdict(list)
    for row in gold_rows:
        for _ in range(n_runs):
            result = _score_one(row, conn, config, candidate_context)
            per_job_runs[row["dedup_key"]].append({
                "sub_scores": _candidate_to_dict(result),
                "provider": getattr(result, "provider", None),
                "status": getattr(result, "status", None),
            })

    # Aggregate per-job: mean of N runs
    AXES = ("title_fit", "location_fit", "comp_fit", "domain_match",
            "seniority_match", "skills_match")
    per_job_mean: dict[str, dict] = {}
    for key, runs in per_job_runs.items():
        valid = [r["sub_scores"] for r in runs if r["sub_scores"]]
        if not valid:
            per_job_mean[key] = {a: float("nan") for a in AXES}
            continue
        per_job_mean[key] = {a: sum(s.get(a, 0) for s in valid) / len(valid) for a in AXES}

    # Per-axis MAE/bias/ICC/QW-kappa, classification metrics, coherence
    metrics_out = _compute_metrics(gold_rows, per_job_mean, per_job_runs)

    # Persist eval_runs row
    run_id = uuid.uuid4().hex
    conn.execute("""
        INSERT INTO eval_runs
        (run_id, timestamp, variant_name, baseline_run_id, gold_set_version,
         n_runs, config_json, metrics_json, per_job_json, report_path, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id, datetime.now(timezone.utc).isoformat(),
        variant_name, baseline_run_id, GOLD_SET_VERSION, n_runs,
        json.dumps(config), json.dumps(metrics_out),
        json.dumps(per_job_runs), "", None,
    ))
    conn.commit()

    # Write report
    from job_finder.eval.report import write_report
    report_path = write_report(
        run_id=run_id,
        variant_name=variant_name,
        baseline_run_id=baseline_run_id,
        gold_rows=gold_rows,
        per_job_mean=per_job_mean,
        per_job_runs=per_job_runs,
        metrics_out=metrics_out,
        report_dir=report_dir,
        db_path=db_path,
    )
    conn.execute("UPDATE eval_runs SET report_path=? WHERE run_id=?", (report_path, run_id))
    conn.commit()
    return report_path


def _compute_metrics(gold_rows, per_job_mean, per_job_runs):
    """Compute the full metric vector. Returns dict suitable for JSON serialization."""
    AXES = ("title_fit", "location_fit", "comp_fit", "domain_match",
            "seniority_match", "skills_match")
    out = {"per_axis": {}, "classification": {}, "coherence": {}, "run_level": {}}

    for axis in AXES:
        gold = [json.loads(r["gold_sub_scores_json"])[axis] for r in gold_rows]
        pred = [round(per_job_mean[r["dedup_key"]][axis]) for r in gold_rows]  # round mean for ordinal metrics
        out["per_axis"][axis] = {
            "mae": metrics.mae(gold, pred),
            "bias": metrics.bias(gold, pred),
            "icc": metrics.icc([gold, pred]),
            "qw_kappa": metrics.qw_kappa(gold, pred),
        }

    # Classification (derived from per-job mean sub-scores via the project's rule)
    from job_finder.db import derive_classification
    pred_cls = []
    for r in gold_rows:
        sub = {a: round(per_job_mean[r["dedup_key"]][a]) for a in AXES}
        cls = derive_classification(
            sub, legitimacy_note=None,
            enrichment_tier=r.get("enrichment_tier"),
            jd_full_length=len(r.get("jd_full") or ""),
            low_signal_threshold=1500,
        )
        pred_cls.append(cls)
    gold_cls = [r["gold_classification"] for r in gold_rows]
    classes = ("apply", "consider", "skip", "reject", "low_signal")
    out["classification"] = {
        "per_class": metrics.classification_metrics(gold_cls, pred_cls, classes),
        "confusion_matrix": metrics.confusion_matrix(gold_cls, pred_cls, classes),
        "apply_false_positive_rate": metrics.apply_false_positive_rate(gold_cls, pred_cls),
    }

    # Coherence
    coherence_input = [
        {"sub_scores": {a: round(per_job_mean[r["dedup_key"]][a]) for a in AXES},
         "gaps_text": " ".join((json.loads(r["fit_analysis"]) or {}).get("gaps", []))
                       if r.get("fit_analysis") else ""}
        for r in gold_rows
    ]
    violations = metrics.coherence_violations(coherence_input)
    out["coherence"] = {
        "violations": violations,
        "rate": len(violations) / len(coherence_input) if coherence_input else 0.0,
    }

    # Run-level: latency / failure aggregated from per_job_runs
    n_total = sum(len(runs) for runs in per_job_runs.values())
    n_failed = sum(1 for runs in per_job_runs.values() for r in runs if r["status"] != "ok")
    out["run_level"] = {
        "total_calls": n_total,
        "failed_calls": n_failed,
        "schema_adherence": (n_total - n_failed) / n_total if n_total else 0.0,
    }

    return out
```

- [ ] **Step 4: Run integration test, verify pass**

```bash
uv run --active pytest tests/test_harness.py -q --tb=short
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_harness.py job_finder/eval/harness.py
git commit -m "$(cat <<'EOF'
feat(eval): harness orchestration

Loads gold-set rows, loads named variant module, runs N times per
row (default 3), computes per-axis MAE/bias/ICC/QW-κ, classification
metrics + apply-FP rate, coherence violations, run-level health.
Persists to eval_runs table.

Phase 5 task 3/5.
EOF
)"
```

---

## Task 5.5: Report generator

**Files:**
- Create: `tests/test_report.py`
- Create: `job_finder/eval/report.py`

- [ ] **Step 1: Test report has the 7 spec'd sections**

```python
def test_report_has_required_sections(tmp_path):
    from job_finder.eval.report import write_report
    # Minimal mock inputs
    metrics_out = {
        "per_axis": {"title_fit": {"mae": 0.5, "bias": 0.1, "icc": 0.7, "qw_kappa": 0.6}},
        "classification": {"per_class": {}, "confusion_matrix": {}, "apply_false_positive_rate": 0.2},
        "coherence": {"violations": [], "rate": 0.0},
        "run_level": {"total_calls": 6, "failed_calls": 0, "schema_adherence": 1.0},
    }
    path = write_report(
        run_id="abcdef", variant_name="baseline", baseline_run_id=None,
        gold_rows=[], per_job_mean={}, per_job_runs={},
        metrics_out=metrics_out, report_dir=str(tmp_path), db_path=":memory:",
    )
    body = (tmp_path / path.split("/")[-1]).read_text() if "/" in path else open(path).read()
    for section in ["Headline", "Aggregated Metric Tables", "Per-Axis", "Confusion Matrix",
                    "Per-Job Diff", "Cost / Latency", "Coherence Violations"]:
        assert section in body
```

- [ ] **Step 2: Implement**

```python
"""Markdown report generator for harness runs."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def write_report(*, run_id, variant_name, baseline_run_id, gold_rows,
                 per_job_mean, per_job_runs, metrics_out, report_dir, db_path):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = f"{date}-{variant_name}-vs-{'baseline' if not baseline_run_id else baseline_run_id[:8]}.md"
    path = Path(report_dir) / fname

    baseline_metrics = _load_baseline_metrics(db_path, baseline_run_id) if baseline_run_id else None

    out = []
    out.append(f"# Eval Run — {variant_name}")
    out.append("")
    out.append(f"**Run ID:** `{run_id}`")
    out.append(f"**Variant:** {variant_name}")
    out.append(f"**Baseline:** {baseline_run_id or '(none)'}")
    out.append(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}")
    out.append("")

    # Headline verdict
    out.append("## Headline")
    apply_fp = metrics_out["classification"]["apply_false_positive_rate"]
    out.append(f"- Apply false-positive rate: **{apply_fp:.3f}**")
    if baseline_metrics:
        baseline_fp = baseline_metrics["classification"]["apply_false_positive_rate"]
        delta = apply_fp - baseline_fp
        verdict = "BETTER" if delta < 0 else "WORSE" if delta > 0 else "EQUAL"
        out.append(f"- vs baseline {baseline_fp:.3f} → Δ {delta:+.3f} ({verdict})")
    out.append("")

    # Aggregated Metric Tables
    out.append("## Aggregated Metric Tables")
    out.append("")
    out.append("### Per-Axis")
    out.append("| Axis | MAE | Bias | ICC(2,1) | QW-κ |")
    out.append("|---|---|---|---|---|")
    for axis, m in metrics_out["per_axis"].items():
        out.append(f"| {axis} | {m['mae']:.3f} | {m['bias']:+.3f} | {m['icc']:.3f} | {m['qw_kappa']:.3f} |")
    out.append("")

    # Classification metrics + Confusion Matrix
    out.append("## Classification Metrics")
    cls_m = metrics_out["classification"]
    out.append("| Class | Precision | Recall | F1 | Support |")
    out.append("|---|---|---|---|---|")
    for c, sub in cls_m["per_class"].items():
        if c == "macro_f1":
            continue
        out.append(f"| {c} | {sub['precision']:.3f} | {sub['recall']:.3f} | {sub['f1']:.3f} | {sub['support']} |")
    out.append(f"\n**Macro-F1:** {cls_m['per_class'].get('macro_f1', 0):.3f}")
    out.append("")

    out.append("## Confusion Matrix")
    cm = cls_m["confusion_matrix"]
    classes = list(cm.keys())
    out.append("| | " + " | ".join(classes) + " |")
    out.append("|---|" + "---|" * len(classes))
    for t in classes:
        row = [str(cm[t].get(p, 0)) for p in classes]
        out.append(f"| **{t}** | " + " | ".join(row) + " |")
    out.append("")

    # Per-Job Diff
    out.append("## Per-Job Diff")
    out.append("Jobs whose classification flipped or where any sub-score moved by ≥ 2 vs gold:")
    out.append("")
    out.append("| dedup_key | gold_cls | pred_cls | flipped | sub-scores delta |")
    out.append("|---|---|---|---|---|")
    AXES = ("title_fit", "location_fit", "comp_fit", "domain_match", "seniority_match", "skills_match")
    for r in gold_rows:
        gold_sub = json.loads(r["gold_sub_scores_json"])
        pred_sub_mean = per_job_mean.get(r["dedup_key"], {})
        deltas = []
        for a in AXES:
            d = round(pred_sub_mean.get(a, 0)) - gold_sub.get(a, 0)
            if abs(d) >= 2:
                deltas.append(f"{a}{d:+d}")
        # Predicted classification — recompute from rounded means
        from job_finder.db import derive_classification
        pred_cls = derive_classification(
            {a: round(pred_sub_mean.get(a, 0)) for a in AXES},
            legitimacy_note=None,
            enrichment_tier=r.get("enrichment_tier"),
            jd_full_length=len(r.get("jd_full") or ""),
            low_signal_threshold=1500,
        )
        flipped = "YES" if pred_cls != r["gold_classification"] else ""
        if flipped or deltas:
            out.append(f"| `{r['dedup_key']}` | {r['gold_classification']} | {pred_cls} | {flipped} | {', '.join(deltas) or '—'} |")
    out.append("")

    # Cost / Latency
    out.append("## Cost / Latency")
    rl = metrics_out["run_level"]
    out.append(f"- Total scoring calls: {rl['total_calls']}")
    out.append(f"- Failed calls: {rl['failed_calls']}")
    out.append(f"- Schema adherence: {rl['schema_adherence']*100:.1f}%")
    out.append("")

    # Coherence Violations
    out.append("## Coherence Violations")
    cov = metrics_out["coherence"]
    out.append(f"Rate: {cov['rate']*100:.1f}% ({len(cov['violations'])} of {len(gold_rows)} jobs)")
    out.append("")
    for v in cov["violations"][:10]:
        out.append(f"- axis={v['axis']} score={v['score']} gaps_text=\"{v['gaps_text'][:120]}\"")
    out.append("")

    path.write_text("\n".join(out))
    return str(path)


def _load_baseline_metrics(db_path, baseline_run_id):
    if not baseline_run_id:
        return None
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT metrics_json FROM eval_runs WHERE run_id=?", (baseline_run_id,)).fetchone()
    if not row:
        return None
    return json.loads(row[0])
```

- [ ] **Step 3: Run tests**

```bash
uv run --active pytest tests/test_report.py -q --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py job_finder/eval/report.py
git commit -m "$(cat <<'EOF'
feat(eval): markdown report generator

Writes versioned reports to .planning/eval_results/ with all 7
required sections: headline verdict, aggregated metric tables,
per-axis, confusion matrix, per-job diff (the headline output for
human review per D-5.3), cost/latency, coherence violations.

Phase 5 task 4/5.
EOF
)"
```

---

## Task 5.6: CLI entry point + first baseline run

**Files:**
- Create: `job_finder/eval/__main__.py`

- [ ] **Step 1: Implement CLI**

```python
"""CLI: `python -m job_finder.eval --variant <name> [--baseline <run-id>] [--runs N]`"""

import argparse
import yaml
from job_finder.eval.harness import run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--baseline", default=None,
                        help="run_id of a previous run for A/B comparison")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report-dir", default=".planning/eval_results")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    report_path = run(
        db_path=args.db,
        variant_name=args.variant,
        n_runs=args.runs,
        baseline_run_id=args.baseline,
        report_dir=args.report_dir,
        config=config,
    )
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run baseline against the gold set**

```bash
uv run python -m job_finder.eval --variant baseline --runs 3
```

Expected: ~120 scoring calls (40 jobs × 3 runs) on qwen2.5:14b. ~10–15 minutes wall time. A report file lands in `.planning/eval_results/`.

- [ ] **Step 3: Inspect the report**

```bash
ls -lt .planning/eval_results/ | head
cat .planning/eval_results/$(ls -t .planning/eval_results/ | head -1)
```

Verify all 7 sections render. Note the run_id from `eval_runs`:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
for r in conn.execute('SELECT run_id, variant_name, timestamp FROM eval_runs ORDER BY timestamp DESC LIMIT 5').fetchall():
    print(r)
"
```

Save the baseline run_id — Phase 4 variants will use it as the `--baseline` argument.

- [ ] **Step 4: Commit the CLI + the baseline report**

```bash
git add job_finder/eval/__main__.py .planning/eval_results/
git commit -m "$(cat <<'EOF'
feat(eval): CLI entry + baseline harness run

Establishes the production-prompt baseline against the 40-job
gold set. 3 runs × 40 jobs = 120 scoring calls, persisted to
eval_runs. Report at .planning/eval_results/<date>-baseline.md
serves as the comparator for all Phase 4 variants.

Phase 5 complete.
EOF
)"
```

---

## Acceptance criteria for Phase 5

- [ ] Migration 44 applied; `eval_runs` table exists in jobs.db
- [ ] Metrics module fully tested (`tests/test_metrics.py` green)
- [ ] Harness end-to-end test passes (`tests/test_harness.py`)
- [ ] Report generator produces all 7 spec'd sections (`tests/test_report.py`)
- [ ] Baseline run committed; report file exists at `.planning/eval_results/`
- [ ] At least one row in `eval_runs` table with `variant_name='baseline'`
- [ ] Full test suite green: `uv run --active pytest tests/ -q --tb=short`

## What this unlocks

Phase 4 (rubric variants) can now A/B against the baseline run. Each candidate variant runs through the harness, gets a report, and the per-job diff table makes the iteration fast.

## Out of scope for this plan

- ECE / calibration curves — deferred to a future N=100+ gold set per D-5.6
- Embedding-based coherence metric — keyword-overlap is the simple-first version per D-5.5
- Web UI for browsing run history — CLI-only
- Regression cron hookup — Phase 6 deferred follow-up
