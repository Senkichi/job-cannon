# Scoring Recalibration Phase 3: Gold Set Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 40-job stratified gold set with per-axis user labels stored as columns on the `jobs` table, plus a CLI labeling tool. Output: 40 rows with `gold_classification`, `gold_sub_scores_json`, `gold_notes`, `gold_labeled_at` populated, ready to drive the eval harness in Phase 5.

**Architecture:** New migration adds 4 nullable columns to `jobs`. A sampling helper picks rows by stratum (anchors, score-bucketed apply/consider/reject, source spread). A CLI script walks unlabeled gold-set rows, prints context, prompts for input, writes back. The labeling task itself is human (~60–80 minutes) and produces the data; the script is the harness.

**Tech Stack:** Python 3.13, SQLite (raw SQL), pytest, click or argparse for the CLI.

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 3, decisions D-3.1 through D-3.7).

**Predecessor plan:** `2026-04-27-scoring-phase-2-bug-fixes.md` (must land first; gold-set labels are made against the *fixed* scorer's outputs)
**Successor plan:** `2026-04-27-scoring-phase-5-eval-harness.md`

---

## File Structure

### Created files

| File | Responsibility |
|---|---|
| `job_finder/scripts/__init__.py` | Empty (if scripts/ is not yet a package) |
| `job_finder/scripts/sample_gold_set.py` | Sampling helper: pick 33 pre-Phase-2 rows + 7 post-Phase-2 rows by stratum |
| `job_finder/scripts/label_gold_set.py` | Interactive CLI labeling tool |
| `tests/test_sample_gold_set.py` | Tests for the sampling SQL |
| `tests/test_label_gold_set.py` | Tests for the labeling CLI (input/output behavior) |

### Modified files

| File | Lines (approx) | Responsibility |
|---|---|---|
| `job_finder/web/db_migrate.py` | +1 migration | Migration 43: add gold_* columns to jobs |

### Files explicitly NOT touched

- `job_finder/web/job_scorer.py` — gold labels are independent of scoring code
- `job_finder/web/templates/` — no UI surface for the gold set (CLI-only per D-3.4)
- `job_finder/web/blueprints/` — gold set is not a web feature

### Reference files

- `job_finder/web/db_migrate.py` — existing migration list pattern (multi-string list)
- `job_finder/web/db_helpers.py` — DB connection patterns (use `standalone_connection` for scripts)

---

## Test Strategy

Sampling tests use a temp SQLite DB seeded with synthetic rows; assertions verify the SQL returns rows from each stratum at the right count.

Labeling-CLI tests use `monkeypatch` to fake `input()` and verify the script writes the expected columns. Avoid testing the interactive flow end-to-end; test the labeling-step function in isolation.

```bash
uv run --active pytest tests/test_sample_gold_set.py tests/test_label_gold_set.py -q --tb=short
```

---

## Task 3.1: Migration 43 — gold_* columns

**Files:**
- Modify: `job_finder/web/db_migrate.py`
- Modify: `tests/test_db_migrate.py` — add coverage

- [ ] **Step 1: Inspect existing migration list**

```bash
grep -n "user_version\|MIGRATIONS" job_finder/web/db_migrate.py | head -10
```

Confirm current `user_version` is 42 (post-Phase-2). Migration 43 sits on top.

- [ ] **Step 2: Append migration 43**

In `db_migrate.py`, append to the migrations list:

```python
# Migration 43: gold-set labeling columns (Phase 3)
"""ALTER TABLE jobs ADD COLUMN gold_classification TEXT
   CHECK (gold_classification IS NULL
          OR gold_classification IN ('apply', 'consider', 'skip', 'reject', 'low_signal'))""",
"""ALTER TABLE jobs ADD COLUMN gold_sub_scores_json TEXT""",
"""ALTER TABLE jobs ADD COLUMN gold_notes TEXT""",
"""ALTER TABLE jobs ADD COLUMN gold_labeled_at TIMESTAMP""",
```

Bump `user_version` to 43 (whatever the project's pattern for setting this is — see existing migrations).

- [ ] **Step 3: Add migration test**

In `tests/test_db_migrate.py` (or wherever migrations are tested):

```python
def test_migration_43_adds_gold_columns(tmp_db_path):
    import sqlite3
    from job_finder.web.db_migrate import run_migrations
    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    assert "gold_classification" in cols
    assert "gold_sub_scores_json" in cols
    assert "gold_notes" in cols
    assert "gold_labeled_at" in cols
    # CHECK constraint enforces enum
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (dedup_key, title, company, gold_classification) "
                     "VALUES ('a|b', 'T', 'C', 'invalid_enum_value')")
```

- [ ] **Step 4: Run tests**

```bash
uv run --active pytest tests/test_db_migrate.py -q --tb=short
```

Expected: green.

- [ ] **Step 5: Apply migration to live DB**

```bash
uv run python -c "from job_finder.web.db_migrate import run_migrations; run_migrations('jobs.db')"
uv run python -c "import sqlite3; conn=sqlite3.connect('jobs.db'); print(conn.execute('PRAGMA user_version').fetchone())"
```

Expected: `(43,)`.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/db_migrate.py tests/test_db_migrate.py
git commit -m "$(cat <<'EOF'
feat(db): migration 43 — gold-set labeling columns

Adds gold_classification (with CHECK enforcing the 5-value enum),
gold_sub_scores_json, gold_notes, gold_labeled_at. All nullable.
Enables Phase 3 user labeling alongside scored data with no
sync hazard (D-3.3).

Phase 3 task 1/3.
EOF
)"
```

---

## Task 3.2: Sampling helper (TDD)

**Files:**
- Create: `tests/test_sample_gold_set.py`
- Create: `job_finder/scripts/sample_gold_set.py`

The sampler picks 33 pre-Phase-2 rows by stratum and writes a list of dedup_keys to a JSON manifest. The actual gold-column writes happen in the labeling CLI; sampling just selects which rows to label.

- [ ] **Step 1: Write failing test**

```python
"""Tests for gold-set sampling: 33 rows pre-Phase-2 + 7 low_signal post-Phase-2."""

import json
import sqlite3
import pytest


@pytest.fixture
def seeded_db(tmp_path):
    """A test DB with synthetic rows spanning all strata."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        title TEXT, company TEXT, classification TEXT,
        sub_scores_json TEXT, sources TEXT, jd_full TEXT,
        enrichment_tier TEXT, gold_classification TEXT
    )""")
    # 50 apply with high composite (≥24)
    for i in range(50):
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (f"hi_apply_{i}", f"T{i}", f"C{i}", "apply",
                      '{"title_fit":5,"location_fit":5,"comp_fit":5,"domain_match":5,"seniority_match":4,"skills_match":4}',
                      '["linkedin"]', "X" * 5000, "free", None))
    # 100 apply with mid composite (18-23)
    for i in range(100):
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (f"mid_apply_{i}", f"T{i}", f"C{i}", "apply",
                      '{"title_fit":3,"location_fit":3,"comp_fit":3,"domain_match":3,"seniority_match":3,"skills_match":3}',
                      '["glassdoor"]', "X" * 5000, "free", None))
    # 50 consider, 30 reject — abbreviated for brevity
    # ...
    conn.commit()
    return str(db_path)


def test_sampling_returns_correct_strata_counts(seeded_db, tmp_path):
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata
    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=[])
    keys = manifest["dedup_keys"]
    assert len(keys) == 33  # 6+6+6+4+8 = 30 + 3 anchor = 33
    # Per-stratum counts verifiable from the manifest's stratum metadata
    assert manifest["strata"]["apply_high"] == 6
    assert manifest["strata"]["apply_mid"] == 6
    assert manifest["strata"]["consider"] == 6
    assert manifest["strata"]["reject"] == 4
    assert manifest["strata"]["cross_source"] == 8
    assert manifest["strata"]["anchors"] == 3


def test_sampling_includes_anchors_first(seeded_db):
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata
    anchors = ["vera|tmf", "latent|ml", "google|deepmind"]
    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=anchors)
    assert manifest["dedup_keys"][:3] == anchors


def test_sample_low_signal_rows_post_phase_2(seeded_db):
    """After Phase 2, sample 7 low_signal rows for the final stratum."""
    from job_finder.scripts.sample_gold_set import sample_low_signal_stratum
    keys = sample_low_signal_stratum(seeded_db)
    assert len(keys) <= 7  # 7 if available, fewer if not enough low_signal rows
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run --active pytest tests/test_sample_gold_set.py -q --tb=short
```

Expected: ImportError.

- [ ] **Step 3: Implement the sampler**

```python
"""Gold-set sampling helper.

Picks 33 rows pre-Phase-2 (anchors + score strata + source spread) and
7 rows post-Phase-2 (low_signal stratum). Writes a JSON manifest with
the chosen dedup_keys; the labeling CLI reads the manifest and walks
through the rows.
"""

import json
import sqlite3
from pathlib import Path

ANCHOR_DEDUP_KEYS = [
    "vera therapeutics|tmf manager, clinical qa",
    "latent (ca)|machine learning engineer",
    "google deepmind|research engineer, frontier safety mitigations, deepmind",
]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def sample_pre_phase_2_strata(db_path: str, anchor_dedup_keys: list = None) -> dict:
    """Sample 33 rows: 3 anchors + 6+6+6+4 by classification × score + 8 cross-source."""
    if anchor_dedup_keys is None:
        anchor_dedup_keys = ANCHOR_DEDUP_KEYS
    conn = _connect(db_path)
    keys: list[str] = list(anchor_dedup_keys)
    strata = {"anchors": len(anchor_dedup_keys)}

    # apply_high: 6 rows with classification='apply' AND composite ≥24
    rows = conn.execute("""
        SELECT dedup_key,
               (json_extract(sub_scores_json,'$.title_fit')
                + json_extract(sub_scores_json,'$.location_fit')
                + json_extract(sub_scores_json,'$.comp_fit')
                + json_extract(sub_scores_json,'$.domain_match')
                + json_extract(sub_scores_json,'$.seniority_match')
                + json_extract(sub_scores_json,'$.skills_match')) AS composite
        FROM jobs
        WHERE classification='apply' AND dedup_key NOT IN (%s)
        HAVING composite >= 24
        ORDER BY RANDOM() LIMIT 6
    """ % ",".join("?" * len(keys)), keys).fetchall()
    keys += [r["dedup_key"] for r in rows]
    strata["apply_high"] = len(rows)

    # apply_mid: 6 rows with classification='apply' AND 18 ≤ composite ≤ 23
    rows = conn.execute("""
        SELECT dedup_key,
               (json_extract(sub_scores_json,'$.title_fit') + json_extract(sub_scores_json,'$.location_fit')
                + json_extract(sub_scores_json,'$.comp_fit') + json_extract(sub_scores_json,'$.domain_match')
                + json_extract(sub_scores_json,'$.seniority_match') + json_extract(sub_scores_json,'$.skills_match')) AS composite
        FROM jobs
        WHERE classification='apply' AND dedup_key NOT IN (%s)
        HAVING composite BETWEEN 18 AND 23
        ORDER BY RANDOM() LIMIT 6
    """ % ",".join("?" * len(keys)), keys).fetchall()
    keys += [r["dedup_key"] for r in rows]
    strata["apply_mid"] = len(rows)

    # consider: 6
    rows = conn.execute("""
        SELECT dedup_key FROM jobs
        WHERE classification='consider' AND dedup_key NOT IN (%s)
        ORDER BY RANDOM() LIMIT 6
    """ % ",".join("?" * len(keys)), keys).fetchall()
    keys += [r["dedup_key"] for r in rows]
    strata["consider"] = len(rows)

    # reject: 4
    rows = conn.execute("""
        SELECT dedup_key FROM jobs
        WHERE classification='reject' AND dedup_key NOT IN (%s)
        ORDER BY RANDOM() LIMIT 4
    """ % ",".join("?" * len(keys)), keys).fetchall()
    keys += [r["dedup_key"] for r in rows]
    strata["reject"] = len(rows)

    # cross_source: 8 rows, 2 per source from {linkedin, glassdoor, dataforseo, careers/ATS}
    for source_pattern in ('%linkedin%', '%glassdoor%', '%dataforseo%', '%Workday%'):
        rows = conn.execute("""
            SELECT dedup_key FROM jobs
            WHERE sources LIKE ? AND classification IS NOT NULL
              AND dedup_key NOT IN (%s)
            ORDER BY RANDOM() LIMIT 2
        """ % ",".join("?" * len(keys)), [source_pattern] + keys).fetchall()
        keys += [r["dedup_key"] for r in rows]
    strata["cross_source"] = len(keys) - sum([strata[k] for k in ("anchors", "apply_high", "apply_mid", "consider", "reject")])

    return {"dedup_keys": keys, "strata": strata, "phase": "pre_phase_2"}


def sample_low_signal_stratum(db_path: str, n: int = 7) -> list[str]:
    """Sample n rows with classification='low_signal'. Run AFTER Phase 2 lands."""
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT dedup_key FROM jobs
        WHERE classification='low_signal' AND gold_classification IS NULL
        ORDER BY RANDOM() LIMIT ?
    """, (n,)).fetchall()
    return [r["dedup_key"] for r in rows]


def write_manifest(manifest: dict, path: str) -> None:
    Path(path).write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--out", default=".planning/gold_set_manifest.json")
    parser.add_argument("--phase", choices=("pre_phase_2", "low_signal"), default="pre_phase_2")
    args = parser.parse_args()

    if args.phase == "pre_phase_2":
        m = sample_pre_phase_2_strata(args.db)
    else:
        keys = sample_low_signal_stratum(args.db)
        m = {"dedup_keys": keys, "strata": {"low_signal": len(keys)}, "phase": "low_signal"}
    write_manifest(m, args.out)
    print(f"Wrote {len(m['dedup_keys'])} dedup_keys to {args.out}")
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run --active pytest tests/test_sample_gold_set.py -q --tb=short
```

Expected: green. (May need to refine the test fixture seed data to match.)

- [ ] **Step 5: Commit**

```bash
git add job_finder/scripts/__init__.py job_finder/scripts/sample_gold_set.py tests/test_sample_gold_set.py
git commit -m "$(cat <<'EOF'
feat(scripts): gold-set stratified sampler

sample_pre_phase_2_strata picks 33 rows: 3 anchors + 6 apply-high
+ 6 apply-mid + 6 consider + 4 reject + 8 cross-source. Writes
a JSON manifest of dedup_keys consumed by the labeling CLI.

sample_low_signal_stratum picks 7 low_signal rows post-Phase-2.

Phase 3 task 2/3. Spec D-3.5.
EOF
)"
```

---

## Task 3.3: Labeling CLI (TDD)

**Files:**
- Create: `tests/test_label_gold_set.py`
- Create: `job_finder/scripts/label_gold_set.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for the labeling CLI's per-job step (input/output isolation)."""

import json
import sqlite3
from io import StringIO
from unittest.mock import patch
import pytest


@pytest.fixture
def db_with_one_job(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        title TEXT, company TEXT, location TEXT, jd_full TEXT,
        sources TEXT, classification TEXT, sub_scores_json TEXT,
        gold_classification TEXT, gold_sub_scores_json TEXT,
        gold_notes TEXT, gold_labeled_at TIMESTAMP
    )""")
    conn.execute("""INSERT INTO jobs VALUES (
        'k|t', 'Test Engineer', 'Acme', 'Remote',
        'A long JD ' || zeroblob(2000),
        '["linkedin"]', 'apply',
        '{"title_fit":4,"location_fit":5,"comp_fit":3,"domain_match":3,"seniority_match":4,"skills_match":3}',
        NULL, NULL, NULL, NULL
    )""")
    conn.commit()
    return str(db)


def test_label_one_writes_gold_columns(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one
    inputs = iter([
        "consider",  # gold_classification
        "3", "5", "3", "3", "4", "3",  # gold sub-scores
        "Title is close but seniority off",  # note
    ])
    with patch("builtins.input", lambda *_: next(inputs)):
        label_one(db_with_one_job, "k|t")
    conn = sqlite3.connect(db_with_one_job)
    row = conn.execute(
        "SELECT gold_classification, gold_sub_scores_json, gold_notes "
        "FROM jobs WHERE dedup_key='k|t'"
    ).fetchone()
    assert row[0] == "consider"
    sub = json.loads(row[1])
    assert sub == {"title_fit": 3, "location_fit": 5, "comp_fit": 3,
                   "domain_match": 3, "seniority_match": 4, "skills_match": 3}
    assert "seniority off" in row[2]


def test_label_one_rejects_invalid_classification(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one
    inputs = iter([
        "invalid",  # bad classification — should re-prompt
        "consider",
        "3", "3", "3", "3", "3", "3",
        "",
    ])
    with patch("builtins.input", lambda *_: next(inputs)):
        label_one(db_with_one_job, "k|t")
    conn = sqlite3.connect(db_with_one_job)
    cls = conn.execute("SELECT gold_classification FROM jobs WHERE dedup_key='k|t'").fetchone()[0]
    assert cls == "consider"


def test_label_one_rejects_out_of_range_subscore(db_with_one_job):
    from job_finder.scripts.label_gold_set import label_one
    inputs = iter([
        "apply",
        "6",  # out of 1-5 range — should re-prompt
        "5", "5", "5", "5", "5", "5",
        "",
    ])
    with patch("builtins.input", lambda *_: next(inputs)):
        label_one(db_with_one_job, "k|t")
    conn = sqlite3.connect(db_with_one_job)
    sub = json.loads(conn.execute("SELECT gold_sub_scores_json FROM jobs WHERE dedup_key='k|t'").fetchone()[0])
    assert sub["title_fit"] == 5
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run --active pytest tests/test_label_gold_set.py -q --tb=short
```

- [ ] **Step 3: Implement the CLI**

```python
"""Interactive gold-set labeling CLI.

Walks through unlabeled rows in the gold-set manifest, prints context,
prompts for classification + per-axis sub-scores + optional note, writes
to gold_* columns. Resumable.

Usage:
    uv run python -m job_finder.scripts.label_gold_set [--manifest .planning/gold_set_manifest.json] [--db jobs.db]
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VALID_CLASSIFICATIONS = ("apply", "consider", "skip", "reject", "low_signal")


def _prompt_classification() -> str:
    while True:
        v = input("Classification [apply|consider|skip|reject|low_signal]: ").strip().lower()
        if v in VALID_CLASSIFICATIONS:
            return v
        print(f"Invalid. Must be one of: {', '.join(VALID_CLASSIFICATIONS)}")


def _prompt_int_in_range(label: str, lo: int, hi: int) -> int:
    while True:
        try:
            v = int(input(f"{label} [{lo}-{hi}]: ").strip())
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"Invalid. Must be an integer between {lo} and {hi}.")


def label_one(db_path: str, dedup_key: str) -> None:
    """Prompt for labels for one row and persist."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT title, company, location, jd_full, sources, "
        "       classification, sub_scores_json "
        "FROM jobs WHERE dedup_key=?",
        (dedup_key,),
    ).fetchone()
    if not row:
        print(f"Row not found: {dedup_key}")
        return

    print("\n" + "=" * 70)
    print(f"Title:    {row['title']}")
    print(f"Company:  {row['company']}")
    print(f"Location: {row['location']}")
    print(f"Sources:  {row['sources']}")
    print(f"\nCurrent model classification: {row['classification']}")
    print(f"Current model sub-scores: {row['sub_scores_json']}")
    print(f"\nJD excerpt (first 600 chars):\n{(row['jd_full'] or '')[:600]}")
    print("=" * 70)

    cls = _prompt_classification()
    sub_scores = {
        "title_fit": _prompt_int_in_range("title_fit", 1, 5),
        "location_fit": _prompt_int_in_range("location_fit", 1, 5),
        "comp_fit": _prompt_int_in_range("comp_fit", 1, 5),
        "domain_match": _prompt_int_in_range("domain_match", 1, 5),
        "seniority_match": _prompt_int_in_range("seniority_match", 1, 5),
        "skills_match": _prompt_int_in_range("skills_match", 1, 5),
    }
    note = input("Note (optional, ≤1 sentence): ").strip()

    conn.execute("""
        UPDATE jobs SET
            gold_classification = ?,
            gold_sub_scores_json = ?,
            gold_notes = ?,
            gold_labeled_at = ?
        WHERE dedup_key = ?
    """, (cls, json.dumps(sub_scores), note or None,
          datetime.now(timezone.utc).isoformat(), dedup_key))
    conn.commit()
    print("Saved.\n")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=".planning/gold_set_manifest.json")
    parser.add_argument("--db", default="jobs.db")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    keys = manifest["dedup_keys"]

    conn = sqlite3.connect(args.db)
    unlabeled = [
        k for k in keys
        if conn.execute(
            "SELECT gold_classification FROM jobs WHERE dedup_key=?", (k,)
        ).fetchone()[0] is None
    ]

    print(f"Gold-set progress: {len(keys) - len(unlabeled)}/{len(keys)} labeled")
    if not unlabeled:
        print("All rows already labeled.")
        return

    for i, key in enumerate(unlabeled, start=1):
        print(f"\n--- Job {i}/{len(unlabeled)} ---")
        label_one(args.db, key)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run --active pytest tests/test_label_gold_set.py -q --tb=short
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add job_finder/scripts/label_gold_set.py tests/test_label_gold_set.py
git commit -m "$(cat <<'EOF'
feat(scripts): interactive gold-set labeling CLI

Walks unlabeled rows from the gold-set manifest, prints context
(title, company, location, JD excerpt, current model scoring),
prompts for classification + 6 sub-scores + optional note, writes
to gold_* columns. Resumable; tracks progress.

Phase 3 task 3/3. Spec D-3.4.
EOF
)"
```

---

## Task 3.4: Run sampling (pre-Phase-2 strata) and label

**Files:**
- Created (by sampling): `.planning/gold_set_manifest.json`

- [ ] **Step 1: Run the pre-Phase-2 sampler**

```bash
uv run python -m job_finder.scripts.sample_gold_set --phase pre_phase_2
```

Expected: writes `.planning/gold_set_manifest.json` with 33 dedup_keys.

- [ ] **Step 2: Inspect the manifest**

```bash
cat .planning/gold_set_manifest.json | head -40
```

Verify: 3 anchor keys (Vera, Latent, DeepMind) appear first; remaining 30 keys distributed across the strata.

- [ ] **Step 3: Run the labeling CLI**

```bash
uv run python -m job_finder.scripts.label_gold_set
```

Walk through 33 jobs interactively. ~60 minutes. The CLI is resumable — quit any time with `Ctrl+C` and re-run to continue.

- [ ] **Step 4: Verify all 33 are labeled**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
n = conn.execute('SELECT COUNT(*) FROM jobs WHERE gold_classification IS NOT NULL').fetchone()[0]
print(f'Labeled: {n}')
print('By classification:')
for r in conn.execute('SELECT gold_classification, COUNT(*) FROM jobs WHERE gold_classification IS NOT NULL GROUP BY gold_classification').fetchall():
    print(f'  {r[0]:<12} {r[1]}')
"
```

Expected: 33 total, distributed across all 4 (or 5 if you used low_signal) classifications.

- [ ] **Step 5: Commit the manifest as a checkpoint**

```bash
git add .planning/gold_set_manifest.json
git commit -m "$(cat <<'EOF'
chore(scoring-recalibration): pre-Phase-2 gold-set manifest (33 jobs)

Stratified sampling output: 3 anchors + 6 apply-high + 6 apply-mid
+ 6 consider + 4 reject + 8 cross-source. Manifest references rows
in jobs.db; gold labels themselves are stored in gold_* columns
on those rows.
EOF
)"
```

The actual labels are in `jobs.db`, not in git. They're not committed because they're user data.

---

## Task 3.5: Run sampling (low_signal stratum) post-Phase-2

**Pre-condition:** Phase 2 must be fully shipped before running this — `low_signal` rows don't exist before then.

- [ ] **Step 1: Verify low_signal rows exist**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
n = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE classification='low_signal'\").fetchone()[0]
print(f'low_signal rows: {n}')
"
```

Expected: ≥ 7. If fewer than 7, the gold set's `low_signal` stratum runs short — note in the harness as a known small-N caveat.

- [ ] **Step 2: Run the low_signal sampler**

```bash
uv run python -m job_finder.scripts.sample_gold_set --phase low_signal --out .planning/gold_set_manifest_low_signal.json
```

- [ ] **Step 3: Run labeling for the new keys**

Either: merge the low_signal manifest into the main one and re-run `label_gold_set`, or run it with `--manifest .planning/gold_set_manifest_low_signal.json`.

```bash
uv run python -m job_finder.scripts.label_gold_set --manifest .planning/gold_set_manifest_low_signal.json
```

- [ ] **Step 4: Verify total gold-set size = 40**

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
n = conn.execute('SELECT COUNT(*) FROM jobs WHERE gold_classification IS NOT NULL').fetchone()[0]
print(f'Total labeled: {n}')
"
```

Expected: 40 (or fewer if `low_signal` rows are scarce).

- [ ] **Step 5: Commit the low_signal manifest**

```bash
git add .planning/gold_set_manifest_low_signal.json
git commit -m "$(cat <<'EOF'
chore(scoring-recalibration): low_signal gold-set manifest (Phase 3 stratum)

Post-Phase-2 sampling of 7 low_signal rows for the gold set.
Brings total gold set to 40 jobs.
EOF
)"
```

---

## Acceptance criteria for Phase 3

- [ ] Migration 43 applied; jobs.db has gold_* columns with CHECK constraint enforcing the 5-value enum
- [ ] `sample_gold_set.py` produces a JSON manifest with exactly 33 dedup_keys (pre-Phase-2) or 7 (low_signal)
- [ ] `label_gold_set.py` walks unlabeled rows, validates input, writes gold_* columns
- [ ] All 3 anchor cases (Vera, Latent, DeepMind) labeled and present in `gold_classification`
- [ ] Total gold-set size = 40 (or documented if `low_signal` stratum runs short)
- [ ] Tests green: `uv run --active pytest tests/test_sample_gold_set.py tests/test_label_gold_set.py tests/test_db_migrate.py -q --tb=short`

## What this unlocks

Phase 5 (eval harness) requires the gold set to exist. Once Phase 3 is complete, Phase 5 has labeled ground truth to score variants against.

## Out of scope for this plan

- Re-labeling logic if Phase 4 changes the axis schema (acknowledged in spec; mitigated by locking the 6-axis schema for Phase 4)
- Web UI for labeling (CLI-only per D-3.4)
- Sampling more than 40 (active learning, expansion to N=100+ — parked per spec)
