# FOLLOWUPS — 2026-05-27 round 13 (cleanup bundle: #12 corrected and landed; #11 + #4 re-framed)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 12 closed the location-parsing SPEC end-to-end (Q3 +
Q1 + Commit D + Commit E / m067 backfill, 11,908/12,383 historic
rows backfilled). Round 13 (this session) picked up the smallest
remaining item in the round-12 cleanup bundle — **#12, the paste-jd
budget-cap log-level test** — but on verification found the
handoff's diagnosis wrong, fixed the actual bug (a too-wide
context window in the test, not a stray `logger.warning` in
source), and re-framed the other two cleanup items (#11, #4) which
turned out not to be actionable in the form round 12 proposed.

## What this session shipped

One commit:

- **`a9f80dd`** — `fix(tests): tighten log-level context window for paste-jd / rescore tests`

### What round 12's handoff got wrong, and what was actually true

Round 12 carried this prescription for cleanup-bundle item #12:

> Find the offending `logger.warning("paste-jd: budget cap reached ...")`
> in `blueprints/jobs.py`, change to `logger.info`. Un-deselect the test.

Reality at session start:

- `blueprints/jobs.py:708` already read
  `logger.info("paste-jd: budget cap reached, scoring skipped for %s", dedup_key)`.
  Some prior session had already flipped the call; the handoff was stale.
- The test wasn't deselected — it just failed when run.
- The failure mode was `assert "logger.warning" not in context` where
  `context = "\n".join(lines[max(0, i - 3) : i + 1])`. The 4-line
  context window scooped up a *neighboring* statement at line 705 —
  `logger.warning("paste-jd: row vanished mid-request for %s", dedup_key)`
  — which is a different call entirely.
- Sibling test `test_rescore_budget_cap_logs_at_info` passed only because
  nothing within its window happened to be `logger.warning`. Same vulnerability,
  no collision today.

### The actual fix

`tests/test_log_levels.py` — both `test_paste_jd_budget_cap_logs_at_info`
and `test_rescore_budget_cap_logs_at_info` rewritten:

- `for i, line in enumerate(lines)` → `for line in lines`
- `context = "\n".join(lines[max(0, i - 3) : i + 1])` removed
- Assertions now scope to `line` only (single-line check)
- Both assertions kept: `logger.warning not in line` + `logger.info in line`
  — the latter is the loud-failure guard if a future refactor splits
  the call across multiple lines
- Block comments document the why (neighboring-warning collision) so
  the next session doesn't widen the window again

Result: 10/10 tests pass in `tests/test_log_levels.py`. Other 3 tests
in the file using the same wider-window pattern (`zero_job_email`,
`promoted_to_unreachable`, `blocked_wipe`) were **left alone**. Their
neighbors are clean today; touching them would be scope creep. If
they ever collide with a refactor, apply the same line-scoped pattern.

## How to verify (this session's work)

```powershell
# 10/10 pass:
.venv/Scripts/python.exe -m pytest tests/test_log_levels.py -v
# Expected: 10 passed in <1s

# Confirm the call is still logger.info at the right line:
.venv/Scripts/python.exe -c "
import inspect
from job_finder.web.blueprints import jobs
src = inspect.getsource(jobs)
print([l for l in src.splitlines() if 'paste-jd: budget cap reached' in l])
"
# Expected: ['            logger.info(\"paste-jd: budget cap reached, scoring skipped for %s\", dedup_key)']

# Sanity-check live DB state hasn't drifted (still post-m067):
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('jobs.db', timeout=5)
print('user_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('backfilled:', c.execute('SELECT COUNT(*) FROM jobs WHERE locations_structured IS NOT NULL').fetchone()[0])
print('total:', c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
"
# Expected: user_version=67, 11908/12383
```

## What I tried / considered but didn't do

- **Apply the same line-scoping fix to the other three context-window
  tests in `test_log_levels.py`** (`test_zero_job_email_routed_to_activity_feed_logs_at_debug`,
  `test_promoted_to_unreachable_logs_at_info`, `test_blocked_wipe_logs_at_debug`).
  Considered; rejected as out of scope. None of them are failing today,
  and the round-12 handoff cleanup bundle was explicitly about #12 only.
  Pattern is now documented in the in-test comments — next session can
  bulk-apply if a regression surfaces.

- **Item #11 (Pyright unused-args cleanup)**. The handoff said this was
  a mechanical sweep of `path` / `tmp_db_path` / `_ctx` / `mock_score`
  / `_i` to `_path` etc. across `tests/test_migration.py`,
  `tests/test_careers_crawler.py`, `tests/test_ats_scanner.py`,
  `tests/test_scheduler.py`. **The project's `[tool.pyright]` config in
  `pyproject.toml:269-270` excludes `**/tests`** — so `pyright` (CLI,
  with project config) reports 0 errors / 0 warnings on those files.
  `mypy` likewise reports clean.
  - I observed the diagnostics actually come from the IDE pyright LSP
    (Cursor/VS Code language server), which appears to scan tests
    despite the exclude — likely because the LSP opens files individually
    rather than walking config-controlled roots. Visible during my
    edits as `<new-diagnostics>` warnings.
  - **Either** the IDE LSP config needs to honor the exclude, **or**
    test parameters need leading-underscore renames if the project
    wants the IDE clean. Both are reasonable; neither was decidable
    without confirming user intent.
  - **Re-classified #11 from "carry forward" to "open / advisory"** —
    see below.

- **Item #4 (Workable widget endpoint switch)**. Handoff trigger
  condition was "if all 4 careers_url-tagged Workable companies return
  0 jobs, switch endpoint to `apply.workable.com/api/v3/...`". Live
  DB shows `jobs_found_total` for the 4 (lifemd / the qode / bettersleep
  / lawnstarter) is **1 / 1 / 1 / 3** — not the zero pattern. So either
  (a) the trigger condition isn't met, or (b) those counts are from old
  scans and a fresh scan would show different. Resolution needs Flask
  running and a manual `POST /admin/jobs/companies_scan/run-now`
  targeting those four — which Flask was off for at session start, same
  as round 12. Carried forward unchanged.

- **Run the full pytest suite.** Ran `tests/test_log_levels.py` only
  (10/10 pass). Diff is purely in a single test file; risk of breaking
  unrelated code is essentially zero. Skipped the ~12.5-min full run.

- **Browser-verify Commit D from round 12** (the new Country/Workplace
  dropdowns + pill renderer). Same Flask-was-off constraint as round
  12 — carried unchanged. Smoke tests cover the routes; visual rendering
  is the gap.

## What's deferred / remaining

### CARRY FORWARD (priority order)

1. **Manual browser smoke for Commit D from round 12** when Flask next
   boots (unchanged from round 12). Quick checklist:
   - Visit `/jobs`; Country dropdown shows US / IN / GB / CA / ... and
     Workplace shows REMOTE / HYBRID / ONSITE / UNSPECIFIED.
   - Filter by country=US → result count narrows to US rows.
   - Filter by workplace_type=REMOTE → ~1,375 rows visible.
   - Expand any job → pill row in detail shows "City, Region · CC · WT".
   - HTMX swap on filter change preserves dropdown state.
   - Pre-existing `_row.html` location column still readable
     (canonical or fallback to raw).

2. **#4 Workable widget endpoint verification (re-scoped):**
   The handoff's "if all return 0 jobs" trigger can't be tested from
   stale DB counts (1/1/1/3 currently). Steps when Flask is up:
   - `POST /admin/jobs/companies_scan/run-now` (or just wait for the
     next scheduled scan).
   - Re-query `companies.jobs_found_total` for the 4 Workable-tagged
     rows after the scan: id 71, 951, 1027, 1036.
   - If all 4 still return 0 (or fewer than 1 for any that had 1 before),
     switch endpoint in `_platforms_workable.py` to
     `apply.workable.com/api/v3/...`.
   - If the counts stay positive (any of them), leave the platform code
     alone and de-prioritize.

3. **Phase F Jobvite per-tenant fix (Item #10 from round 11 / #3 from
   round 12):** add per-tenant `careers_nav_recipe` overrides or a
   dedicated jv-job-list scraper for the 5 unhandled jobvite tenants:
   american-specialty-health, capcom, neogenomics, the-institutes,
   victaulic. Tier-4 escalation. Likely the biggest single commit; start
   with capcom and neogenomics (active listings on public sites).
   Verify with `POST /admin/jobs/careers_crawl/run-now` after each
   per-tenant change.

### Audit-track follow-ups (carried unchanged from rounds 7-12)

4. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Tier-4 crawler. **Scheduled as the focus
   of the next session** — see "Next session's contract" below.

5. **Manual company aliases UI** (round-3 deferred).

6. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried.

7. **Round-8 carry: does the-institutes slug need manual cleanup?**
   Their careers_url 302s to `?invalid=1`. Data issue. Flag in the
   companies UI; this also affects #3 above.

### Open / advisory items

- **#11 (Pyright unused-args) — re-framed as advisory.** Project
  `[tool.pyright]` excludes `**/tests`, so CLI `pyright` is silent
  on test files. The "noise" is IDE-only (LSP scans individual files
  regardless of exclude). Two reasonable paths:
  - **Path A (IDE-quiet):** rename test params to leading-underscore
    in the 4 files round-12 listed. Single mechanical pass. No CLI
    behavior change.
  - **Path B (config-honest):** add a pyrightconfig.json or extend
    `[tool.pyright]` exclude to match IDE LSP scan behavior, or set
    `reportUnusedParameter = "none"` globally (already set to "none"
    for `reportUnusedFunction` for similar reasons).
  - Path B is one line; Path A is ~50 lines. Pick before any next
    sweep so the work isn't redone.

- **Lever freeform strings — keep `unresolved=True` forever?** (carried
  unchanged from round 12.) m067 backfill now covers Lever rows; the
  parse_locations-inside-_to_canonical idea is lower value. De-prioritized.

- **Bare-token workplace detection in jd_full** (Q3 extension, carried
  from round 12). Hold off until empirical signal justifies the FP risk.

- **Migration count drift on future migrations** (carried). The 3 generic
  sites in `tests/test_migration.py` (lines 404, 936, 1384) still use
  exact `== NN`. Next migration will require bumping all three.

- **Production country distribution sanity** (carried). Top countries
  after m067: US 8987 (72.6%), IN 352, TH 74, GB 74, CA 65. IN / TH
  higher than expected for a US-focused search — worth a one-shot spot
  check that those rows are real postings, not parser mis-anchoring.

- **Pre-existing pyright IDE diagnostics in `test_log_levels.py`** (newly
  observed this session, not caused by my edits): 5 `caplog` parameters
  flagged unused (lines 42, 77, 151, 178, 202). Tests use `inspect.getsource`
  rather than caplog, so the fixture param is dead. Two of those tests
  *do* have caplog-using siblings (paired pattern). Cleanup is a leading-
  underscore rename of the unused ones; same Path-A-vs-B decision as #11.

## Quirks the next session should know

Rounds 3-12 quirks still apply. Additions from round 13:

- **The handoff's prescription was wrong; the prior session had already
  half-done the fix.** This is a generic warning, not specific to #12:
  when verifying a handoff item, always read the actual code rather
  than trusting that "the prior session left it in state X". The flip
  to `logger.info` had already happened — only the test diagnosis was
  carried forward. Phase 2 verification (read the artifacts, not the
  summary) is what caught this.

- **IDE pyright LSP behavior differs from CLI pyright with project
  config.** The IDE LSP appears to scan opened test files regardless
  of `[tool.pyright] exclude = ["**/tests"]`. If a future session
  considers test-file pyright noise actionable, this asymmetry is the
  reason — pick Path A or Path B from the "Open / advisory" item #11
  before any mechanical sweep.

- **The line-scoped `logger.<level>` pattern in `test_log_levels.py`
  assumes the log call and message string are on the same source line.**
  All five tests using this pattern (now consistent across paste-jd,
  rescore, and by inspection the older three) rely on this convention.
  If a future PR reformats a log call to a multi-line `logger.info(\n
  "...",\n arg\n)`, the test's `logger.info in line` assertion will
  fail loudly (intended), but the matching may also fall through if
  the message string moves to a continuation line. Easy fix at that
  point: regex-walk backward from the matched line to find the
  containing `logger.X(`. Don't pre-empt now.

## Next session's contract

**Primary focus: AI-nav recipes for in-house custom ATS** (audit-track
item #4 above). Tier-4 crawler work. The ten target companies are
Apple, Tesla, Oracle Recruiting Cloud, AMD, NVIDIA, ByteDance,
Deloitte, Genentech, Citi, Kaiser Permanente — all of which run
custom (non-standard-ATS) careers sites and currently fall through
the static/playwright tiers to the AI-navigator tier without a
working recipe.

Suggested approach:

- Start by reading `job_finder/web/careers_crawler/_ai_nav_tier.py`
  + `job_finder/web/ai_career_navigator.py` (the latter is the
  Tier-4 implementation kept from removed Phase 5 — see CLAUDE.md
  "Phase 5 (Intelligence): Removed" note). Confirm the recipe-cache
  table state (existing 16 cached recipes, ~10 active companies).
- For each target, pull the careers page once with the crawler in
  verbose mode and capture what the AI-nav tier sees. Identify
  whether the page is JS-rendered (Apple, Tesla, NVIDIA likely),
  whether it has an XHR/JSON endpoint (Oracle Recruiting Cloud
  almost certainly does), or whether it's a server-rendered
  custom listing (Deloitte / Genentech / Citi / Kaiser are
  plausibly in this bucket).
- Recipe form: each tenant gets a `careers_nav_recipe` JSON entry
  (DOM selectors + optional API endpoint hint) — same shape used
  by Phase F Jobvite item below. Order recipes by failure rate
  (which companies users are actually waiting on results for).
- Verify with `POST /admin/jobs/careers_crawl/run-now` per company.
  Each working recipe is one atomic commit.

Reasonable scope: 3-5 recipes shipped (the easiest of the ten),
not all 10. The remaining can be a follow-up. Apple + Tesla are
the highest-value but likely the hardest (heavy JS, anti-bot).
Oracle Recruiting Cloud is the highest-value mid-difficulty
(used by many companies beyond just Oracle itself).

Pre-flight before starting (~10 min total):

1. Pick **Path A or Path B** for the pyright IDE-noise question
   (item #11 in Open/advisory). One line either way; settles
   whether future test-side pyright noise is actionable.
2. Boot Flask once, run the manual browser smoke for Commit D
   (round 12's lingering item #1) and the Workable scan check
   (item #4 / #2 here). Both are quick visual / DB-state checks.
   Flask must be up for the AI-nav recipe work anyway.

Holding pattern (deferred from this session forward unless the
AI-nav work finishes early):

- Phase F Jobvite per-tenant work (#3). Same shape as AI-nav
  recipes (custom-tenant overrides), so the work is parallel —
  could be batched in the same session if AI-nav goes faster
  than expected.
- Manual company aliases UI (#5).

## Open questions

**RESOLVED in round 13 (this session):**

- ✅ **#12 paste-jd log-level test:** Root cause identified (test's
  4-line context window catching a neighboring `logger.warning`).
  Fixed by scoping assertion to matched line. Sibling rescore test
  tightened defensively. 10/10 pass.

**RE-CLASSIFIED in round 13:**

- ⚠️ **#11 Pyright unused-args:** Re-framed from "carry forward" to
  "open / advisory". CLI pyright (with project config) is silent;
  noise is IDE-only. Needs a Path A or Path B decision before any
  sweep.

- ⚠️ **#4 Workable widget endpoint:** Trigger condition (all 4 return
  0 jobs) not met by stale DB counts. Re-scoped to require Flask scan
  before any code change.

**STILL OPEN (carried from prior rounds, low-priority):**

- **`uv sync` editable-rebuild conflict with running Flask** (round-9
  carry). No new deps this round; pyproject.toml unchanged.

- **Bare-token JD body workplace detection** — see round-12 "Open /
  advisory items" #2. Hold off until empirical signal justifies FP risk.

- **Manual users of `parse_locations` outside upsert_job** (round-12
  carry). No known existing callers; worth grep-checking before any
  future signature change.
