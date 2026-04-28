# Scoring Recalibration Phase 6: Rollout Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wholesale re-score all existing jobs with the Phase 4 winning prompt + the Phase 2 fixes (profile injection, synthesis-free enrichment, low_signal classification). Optionally set up a weekly regression-mode harness cron. Produce a milestone close-out summary.

**Architecture:** A single one-shot script nullifies `classification`, `sub_scores_json`, and `fit_analysis` for all rows, then triggers the existing batch scorer to re-process them. Atomic from the user's perspective: when overnight completes, the table is consistent. The optional regression cron is a thin APScheduler job that runs the harness in regression mode and alerts on gate failures.

**Tech Stack:** Python 3.13, SQLite, existing batch_scoring blueprint, APScheduler 3.11 (already installed).

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 6, decisions D-6.1 through D-6.4).

**Predecessor plan:** `2026-04-27-scoring-phase-4-rubric-redesign.md`
**Successor plan:** None — this is the final plan in the milestone.

---

## File Structure

### Created files

| File | Responsibility |
|---|---|
| `scripts/wholesale_rescore.py` | One-shot: nullify classifications, kick off batch scorer |
| `tests/test_wholesale_rescore.py` | Tests for the script's nullify-and-trigger logic |

### Created files (deferred — only if Step 6.4 happens)

| File | Responsibility |
|---|---|
| Section in `job_finder/web/scheduler.py` | New APScheduler job for weekly regression harness |
| `tests/test_regression_cron.py` | Tests for cron trigger logic |

### Modified files

| File | Lines | Responsibility |
|---|---|---|
| `.planning/STATE.md` | edited | Mark recalibration milestone complete |
| `.planning/MILESTONES.md` | edited | Append recalibration entry |

---

## Test Strategy

The wholesale-rescore script is destructive (nullifies columns), so the tests run against a temp DB. The mock pattern: spawn a temp DB with a few scored rows, run the script in `--dry-run`, assert the row counts match, then run for real and assert columns are NULL.

```bash
uv run --active pytest tests/test_wholesale_rescore.py -q --tb=short
```

---

## Task 6.1: Wholesale re-score script (TDD)

**Files:**
- Create: `tests/test_wholesale_rescore.py`
- Create: `scripts/wholesale_rescore.py`

- [ ] **Step 1: Write failing test**

```python
"""Tests for wholesale_rescore.py — nullify and trigger."""

import sqlite3
import pytest


@pytest.fixture
def db_with_scored_rows(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        classification TEXT, sub_scores_json TEXT, fit_analysis TEXT,
        gold_classification TEXT
    )""")
    rows = [
        ("a|1", "apply", '{"x":3}', '{"strengths":[]}', None),
        ("a|2", "consider", '{"x":3}', '{"strengths":[]}', None),
        ("a|3", "reject", '{"x":3}', '{"strengths":[]}', "skip"),  # gold-labeled
    ]
    for r in rows:
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", r)
    conn.commit()
    return str(db)


def test_dry_run_does_not_modify(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications
    nullify_classifications(db_with_scored_rows, dry_run=True)
    conn = sqlite3.connect(db_with_scored_rows)
    cls_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
    ).fetchone()[0]
    assert cls_count == 3, "Dry-run should not change anything"


def test_real_run_nullifies_classifications(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications
    nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")
    conn = sqlite3.connect(db_with_scored_rows)
    cls_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
    ).fetchone()[0]
    assert cls_count == 0


def test_preserves_gold_columns(db_with_scored_rows):
    """gold_classification MUST NOT be nullified — it's user data, not derived."""
    from scripts.wholesale_rescore import nullify_classifications
    nullify_classifications(db_with_scored_rows, dry_run=False, confirm="yes")
    conn = sqlite3.connect(db_with_scored_rows)
    n_gold = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE gold_classification IS NOT NULL"
    ).fetchone()[0]
    assert n_gold == 1


def test_aborts_without_confirmation(db_with_scored_rows):
    from scripts.wholesale_rescore import nullify_classifications
    with pytest.raises(SystemExit):
        nullify_classifications(db_with_scored_rows, dry_run=False, confirm="no")
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run --active pytest tests/test_wholesale_rescore.py -q --tb=short
```

Expected: ImportError.

- [ ] **Step 3: Implement the script**

```python
#!/usr/bin/env python3
"""One-shot: nullify classifications across all jobs to trigger a wholesale re-score
under the new (Phase 4 winning) prompt + Phase 2 enrichment fixes.

Preserves gold_* columns (user data, independent of derived classification).

Usage:
    uv run python scripts/wholesale_rescore.py [--db jobs.db] [--dry-run]
"""

import argparse
import sqlite3
import sys


def count_to_nullify(db_path: str) -> tuple[int, int]:
    """Returns (total_scored, total_excluded_via_gold)."""
    conn = sqlite3.connect(db_path)
    total = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL"
    ).fetchone()[0]
    return total, 0  # gold_* is NOT excluded; it's a separate column


def nullify_classifications(db_path: str, *, dry_run: bool = False, confirm: str = "no") -> int:
    """Nullify classification, sub_scores_json, fit_analysis on all rows.

    Returns rows affected. Aborts with SystemExit if confirm != 'yes' (unless dry_run).
    """
    total, _ = count_to_nullify(db_path)
    print(f"Would nullify classification on {total} rows.")

    if dry_run:
        print("--dry-run: no changes written.")
        return 0

    if confirm.lower() != "yes":
        print(f"Aborting: confirmation '{confirm}' is not 'yes'.")
        sys.exit(2)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("""
        UPDATE jobs
        SET classification = NULL,
            sub_scores_json = NULL,
            fit_analysis = NULL,
            scoring_provider = NULL,
            scoring_model = NULL
        WHERE classification IS NOT NULL
    """)
    conn.commit()
    print(f"Nullified {cur.rowcount} rows.")
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        nullify_classifications(args.db, dry_run=True)
        return 0

    print("\nThis will nullify classification on all scored rows in the DB.")
    print("After this completes, run the batch scorer (web UI or CLI) to re-score.")
    print("Estimated re-score time: ~6s/job × N rows ≈ overnight on Ollama qwen2.5:14b.")
    confirm = input("\nProceed? Type 'yes' to confirm: ").strip()
    nullify_classifications(args.db, dry_run=False, confirm=confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run --active pytest tests/test_wholesale_rescore.py -q --tb=short
```

- [ ] **Step 5: Commit**

```bash
git add scripts/wholesale_rescore.py tests/test_wholesale_rescore.py
git commit -m "$(cat <<'EOF'
feat(scripts): wholesale re-score one-shot

Nullifies classification/sub_scores_json/fit_analysis/scoring_provider/
scoring_model across all jobs to trigger a full re-score under the
new Phase 4 prompt and Phase 2 enrichment fixes. Preserves gold_*
columns (user data).

Estimated re-score: ~6s/job × ~5000 jobs ≈ overnight on Ollama.

Phase 6 task 1/3.
EOF
)"
```

---

## Task 6.2: Execute wholesale re-score (manual checkpoint)

This task is operationally heavy. Plan ~9 hours overnight wall time.

- [ ] **Step 1: Verify Phase 2, 4 are fully shipped**

```bash
git log --oneline -20  # confirm latest commits include Phase 2 + Phase 4 work
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
print('user_version:', conn.execute('PRAGMA user_version').fetchone()[0])
"
```

Expected: `user_version >= 44` (post-migrations).

- [ ] **Step 2: Run dry-run**

```bash
uv run python scripts/wholesale_rescore.py --dry-run
```

Verify the count is reasonable (~5,000 rows).

- [ ] **Step 3: Take a backup of jobs.db**

```bash
cp jobs.db jobs.db.bak.pre-rescore-$(date +%Y%m%d-%H%M%S)
ls -lh jobs.db.bak.pre-rescore-*
```

This is a non-trivial precaution: if the wholesale re-score has a bug, a backup is the only recovery path. The DB is gitignored, so this is the only protection.

- [ ] **Step 4: Run for real**

```bash
uv run python scripts/wholesale_rescore.py
# Type 'yes' at the confirm prompt
```

- [ ] **Step 5: Trigger the batch scorer**

Use the web UI's "Score all" button OR the CLI batch entry point (whichever is the project's primary path). The scorer should pick up all NULL-classification rows.

If the batch scorer doesn't have a "score everything" mode, manually invoke it:

```bash
uv run python -c "
from job_finder.web.blueprints.batch_scoring import score_unclassified_jobs
score_unclassified_jobs(db_path='jobs.db', config=...)
"
```

(Adapt to actual API; the project should have a "score-all" entry point. If not, this task creates one.)

- [ ] **Step 6: Verify after completion**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
rows = conn.execute('SELECT classification, COUNT(*) FROM jobs WHERE classification IS NOT NULL GROUP BY classification').fetchall()
total = sum(r[1] for r in rows)
print(f'Re-scored: {total}')
for r in rows: print(f'  {r[0]:<12} {r[1]:>5}  ({r[1]/total*100:5.1f}%)')
"
```

Verify the apply rate is materially lower than the pre-recalibration 55% (reasonable target: ≤25% per spec soft target, but the actual number depends on Phase 4 winner). Also verify `low_signal` rows are present (positive number, not zero).

- [ ] **Step 7: Spot-check the 3 anchor cases**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
for query in [
    \"title LIKE '%TMF Manager%' AND company LIKE '%Vera%'\",
    \"title LIKE '%Machine Learning Engineer%' AND company LIKE '%Latent%'\",
    \"title LIKE '%Frontier Safety%' AND company LIKE '%DeepMind%'\",
]:
    r = conn.execute(f'SELECT title, company, classification, sub_scores_json FROM jobs WHERE {query} LIMIT 1').fetchone()
    print(r)
"
```

Expected: NONE classified `apply`. If any of the 3 still apply, that's a regression — investigate before proceeding.

---

## Task 6.3: Milestone close-out

**Files:**
- Modify: `.planning/STATE.md`
- Modify: `.planning/MILESTONES.md` (or equivalent project record)
- Optional: write `.planning/SCORING-RECALIBRATION-RETROSPECTIVE.md`

- [ ] **Step 1: Update `.planning/STATE.md`**

Mark the recalibration milestone as complete. Update:
- `last_activity`: date + brief description
- Phase status table: append recalibration milestone with status=complete
- Architectural decisions log: cross-reference the spec doc

- [ ] **Step 2: Run final regression check**

```bash
BASELINE_RUN_ID=$(uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
r = conn.execute('SELECT run_id FROM eval_runs WHERE variant_name=\"v4_finalist\" ORDER BY timestamp DESC LIMIT 1').fetchone()
print(r[0] if r else '')
")
uv run python -m job_finder.eval --variant v4_finalist --baseline $BASELINE_RUN_ID --runs 3
```

Expected: results within noise of the prior finalist run. If they materially diverge, that's a sign of unintended drift; investigate.

- [ ] **Step 3: Commit milestone close-out**

```bash
git add .planning/STATE.md .planning/MILESTONES.md
git commit -m "$(cat <<'EOF'
docs(planning): scoring recalibration milestone complete

Six-phase milestone shipped: literature survey, profile injection,
enrichment cascade rewrite, low_signal classification, gold-set
labeling, eval harness, rubric redesign, wholesale re-score.

Headline outcomes:
  - Apply false-positive rate: 55% → <new pct>%
  - All 3 anchor cases (Vera, Latent, DeepMind) moved out of apply
  - low_signal classification: <count> rows now honestly flagged
  - Enrichment cascade synthesis-free; ~310 lines of LLM-synthesis
    code deleted

Spec: docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md
Phase plans: docs/superpowers/plans/2026-04-27-scoring-phase-{1..6}-*.md
EOF
)"
```

---

## Task 6.4 (deferred): Weekly regression cron

**Per spec D-6.4, this is deferred to post-ship — implement only if/when desired.**

- [ ] **Step 1: Add APScheduler job**

In `job_finder/web/scheduler.py`, add a new job that runs `job_finder.eval.harness.run` in regression mode against the gold set on a weekly cron.

```python
@scheduler.scheduled_job(CronTrigger.from_crontab("0 9 * * 0"))  # Sundays 9am
def regression_check():
    from job_finder.eval.harness import run
    report_path = run(db_path="jobs.db", variant_name="<production variant>", runs=3, ...)
    metrics = load_latest_metrics_from_eval_runs(...)
    failures = check_acceptance_gates(metrics)
    if failures:
        log_alert(f"Acceptance gates failing: {failures}; report: {report_path}")
```

- [ ] **Step 2: Add config knob**

`scoring.regression_check_cron` in `config.example.yaml` (default: `"0 9 * * 0"`, disabled by default with a `regression_check_enabled: false` knob).

- [ ] **Step 3: Test, commit**

Standard TDD: mock APScheduler, verify the job is registered and triggers `run` with the right args.

---

## Acceptance criteria for Phase 6

- [ ] Wholesale re-score script tested and committed
- [ ] DB backup taken before destructive run
- [ ] Wholesale re-score completed; all rows have new classifications
- [ ] Apply rate materially lower than pre-recalibration 55%
- [ ] All 3 anchor cases classified as NOT `apply`
- [ ] `low_signal` classification populated for genuinely-no-signal jobs
- [ ] `.planning/STATE.md` and milestone records updated
- [ ] Final regression-mode harness pass shows results stable vs Phase 4 finalist run
- [ ] Optional: regression cron implemented (Task 6.4) — defer unless explicitly desired

## Milestone is complete when

- All 6 phase plans' acceptance criteria are met
- The 3 anchor cases (Vera, Latent, DeepMind) no longer classify as `apply`
- The user can read the table and feel that scores match their judgment in spot-checks
- The eval harness baseline run is durably stored in `eval_runs` for future regression checks

## Out of scope for this plan

- Verbalized confidence per axis (parked)
- Pairwise / listwise scoring (parked)
- Multi-judge ensembles (parked)
- Active-learning gold-set expansion (parked)
- Replacing qwen2.5:14b (parked unless next milestone justifies)
- Consolidating profile storage between config.yaml and experience_profile.json (parked separate refactor)
- `low_signal` UI affordance beyond color (separate trivial follow-up)
