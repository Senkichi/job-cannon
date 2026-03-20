# Wave 4: Error & Failure Audit Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit all error handling in background processes, produce a categorized findings report, and implement fixes for each finding.

**Architecture:** Two-phase: (1) static analysis + data analysis audit producing a findings doc, (2) implement fixes categorized as quick-fixes (add logging), data-fixes (migration), and code-fixes (structural changes).

**Tech Stack:** Python, SQLite, logging module

**Spec:** `docs/superpowers/specs/2026-03-18-wave4-error-audit-design.md`

---

## Chunk 1: Audit Phase

### Task 1: Run the static analysis audit

**Files:**
- Create: `docs/superpowers/findings/2026-03-18-error-audit-findings.md`

- [ ] **Step 1: Audit all files listed in the spec**

Read every file in the audit scope (listed in spec) and categorize each `try/except` block. For each finding, document:
- File and line numbers
- What it catches, what it logs, what it does on error
- Severity (High/Medium/Low)
- Concrete fix

Files to audit (from spec):
- `web/scheduler.py`
- `web/pipeline_runner.py`
- `web/data_enricher.py`
- `web/ats_scanner.py`
- `web/claude_client.py`
- `web/haiku_scorer.py`
- `web/sonnet_evaluator.py`
- `web/stale_detector.py`
- `web/pipeline_detector.py`
- `web/careers_scraper.py`
- `web/expiry_checker.py`
- `web/resume_generator.py`
- `web/interview_prep.py`
- `web/resume_feedback.py`
- `web/description_reformatter.py`
- `web/rejection_analyzer.py`
- `parsers/*.py`
- `sources/gmail_source.py`
- `sources/serpapi_source.py`

Use the `Explore` agent type for this — it's a thorough codebase search task.

- [ ] **Step 2: Run the data analysis**

Query the live DB for evidence of past failures:
```python
python -c "
import sqlite3, os
conn = sqlite3.connect('jobs.db')
# Check parse failures directory
pf_count = len(os.listdir('data/parse_failures')) if os.path.exists('data/parse_failures') else 0
# Check runs table for errors
error_runs = conn.execute(\"SELECT source, COUNT(*) FROM runs GROUP BY source\").fetchall()
# Check enrichment tier distribution
tiers = conn.execute(\"SELECT enrichment_tier, COUNT(*) FROM jobs GROUP BY enrichment_tier\").fetchall()
# Check cost tracking
costs = conn.execute(\"SELECT purpose, COUNT(*), SUM(cost_usd) FROM ai_cost_log GROUP BY purpose\").fetchall()
print(f'Parse failures on disk: {pf_count}')
print(f'Runs by source: {dict(error_runs)}')
print(f'Enrichment tiers: {dict(tiers)}')
print(f'AI costs by purpose: {[(r[0], r[1], round(r[2] or 0, 4)) for r in costs]}')
conn.close()
"
```

- [ ] **Step 3: Write the findings document**

Write findings to `docs/superpowers/findings/2026-03-18-error-audit-findings.md` with this structure:

```markdown
# Error Audit Findings

## High Severity
### [Finding title]
**File:** path:lines
**Evidence:** ...
**Fix:** ...

## Medium Severity
...

## Low Severity
...

## Data Analysis Results
...
```

- [ ] **Step 4: Commit the findings**

```bash
git add docs/superpowers/findings/
git commit -m "audit: error handling findings across background processes"
```

## Chunk 2: Implement Fixes

### Task 2: Quick fixes — add logging to bare except blocks

- [ ] **Step 1: For each bare `except Exception: pass` or `except: pass` found in the audit**

Replace with `except Exception as e: logger.debug(...)` pattern. These are mechanical changes — the fix is always the same:

Before: `except Exception: pass`
After: `except Exception as e: logger.debug("Context description (non-fatal): %s", e)`

Apply across all affected files identified in the findings.

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "fix: add debug logging to bare except blocks across background modules"
```

### Task 3: Code fixes — structural error handling improvements

- [ ] **Step 1: Implement each code fix from the findings**

These are case-by-case fixes identified in the audit. Apply each one, run tests after each change.

- [ ] **Step 2: Run full test suite after all code fixes**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit per category**

```bash
git commit -m "fix: improve error handling in [category] — [brief description]"
```

### Task 4: Data fixes — migration for any DB cleanup needed

- [ ] **Step 1: If the audit identifies corrupted data beyond what Wave 2 already handles**

Add migration 16 (or later, depending on Wave 2's migration 15) to `db_migrate.py`.

- [ ] **Step 2: Commit**

```bash
git add job_finder/web/db_migrate.py
git commit -m "fix: add migration N — [description of data cleanup]"
```

Note: This task is conditional — it only applies if the audit finds data issues not already addressed by Wave 2.
