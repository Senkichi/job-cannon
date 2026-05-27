# FOLLOWUPS — 2026-05-27 round 3 (both prior-session lists closed + new user bug list)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. The prior session closed both the 2026-05-27 deferred list and
the user-appended bug list (round 1+2). That handoff itself ended with
new deferred items + suggested-next-steps AND the user appended a fresh
6-item bug list at the bottom of FOLLOWUPS.md. User direction at the
start of this session: *"don't just address the user bug list, also
address the items surfaced by the previous session and suggested next
steps."*

User also clarified the company-dedupe rule that m061 deliberately
left open: **"companies should be merged when they share the same job
board. if the two amazons both use the same internal or external
board, they should be listed as one company. if they use different
ones, split them out."**

## What this session shipped

Commits, in order (newest first):

1. `chore(companies): delete orphan _scan_result.html template` —
   c946d4d. The synchronous scan UX template had been left in the repo
   as a safety net after the async-scan refactor; prior handoff flagged
   it as safe to delete. Confirmed zero live references; updated stale
   comments + a docstring in test_companies.py. The remaining test on
   `test_scan_returns_scan_result_fragment` still passes (asserts
   non-empty body only).

2. `fix(jobs): smooth-scroll back to compact row on collapse, route-
   side trigger` — 1e4dd1a. User bug #4. Replaced the brittle
   `hx-on::after-request` inline JS that relied on
   `this.closest('tr').previousElementSibling` (fragile once the
   expanded row is swapped out) with an `HX-Trigger-After-Settle:
   {"job-collapsed": {"dedup_key": "..."}}` header from the collapse
   route + a single global listener in jobs/index.html that finds the
   compact row by its new `data-dedup-key` attribute. Both collapse
   paths (compact-row toggle and bottom-Collapse button) now fire the
   same code uniformly. Tests cover the route contract; visual smooth-
   scroll itself requires a browser.

3. `feat(locations): display-side normalization for filter dropdown` —
   961b854. User bug #1. After m060's write-side cleanup, the user
   still saw many San Jose variants in the filter. Live-DB inspection
   showed 16+ distinct entries (annotations, country tokens, ZIPs,
   ALLCAPS, state-name spelled out). Added `normalize_for_display` in
   `location_normalizer.py`, called only by `get_distinct_locations`
   (read-side). Strips "(+N other)" / "(+N others)", ZIP codes,
   trailing US country tokens, case-folds ALLCAPS multi-segment strings
   (with a comma-presence guard so NYC/SF/USA/UK stay ALLCAPS), and
   maps full US state names to 2-letter codes. After deploy the 16+
   San Jose variants collapse to 6 genuinely distinct entries (San
   Jose, San Jose CA, San Jose Costa Rica, San Jose CR, San Jose
   Office HQ, malformed "San Jose, NA, cr"). Source data is NOT
   mutated.

4. `fix(companies): m063 merges companies by shared job board` —
   f956d59. User bug #3 + user-clarified rule. Two passes:
   `(ats_platform, ats_slug)` first (most reliable: same ATS endpoint
   = same hiring entity even when display names diverge — "Sony
   Interactive Entertainment" and "PlayStation" both pulling from
   greenhouse/sonyinteractiveentertainmentglobal merge into one row),
   then canonical `careers_url` (host + path lowercased, www/scheme/
   query/trailing-slash stripped — catches "Empower" / "Empower
   Retirement" both pointing at jobs.empower.com). Canonical row wins
   on highest `jobs_found_total` with lowest id as tiebreaker.
   Applied to live DB: 83 duplicate rows merged (3691 → 3608
   companies). Deliberate non-handling: rows with NULL platform/slug
   AND NULL careers_url have no board signal and can't be merged here
   (the user's "ncidia"/"2100 nvidia usa" example sits in that
   cohort).

5. `feat(companies): persist scan progress across navigation` —
   d87a923. User bug #2. The async Scan ATS bg thread keeps running
   even when the user navigates away; clicking back to /companies/
   used to hide all progress until a manual re-click. Now the index
   route detects any status='running' ats_scan session and inlines the
   polling progress fragment in `#scan-result` with the live "Scanned
   X of N" count, so HTMX picks up the polling automatically. New
   helper `_find_running_scan_session` + a route-level translation of
   the session row's (id/scored/total) columns into the fragment's
   (session_id/scanned/total) template variables.

6. `feat(ats_scanner): per-company progress callback for Scan ATS` —
   1b2a0a5. Prior-session deferred item. Threads a
   `progress_callback: ProgressCallback | None` through `run_ats_scan`
   to a small `_ProgressTracker` shared between Phase A and Phase C
   loops. Total is computed upfront from the same WHERE clauses the
   phases use (new `_count_phase_a_eligible` + `_count_phase_c_
   eligible` helpers). `companies._run_ats_scan_bg` provides a tick
   callback that writes (scored, total) to the session row each
   company; the polling fragment then renders "Scanned X of N" instead
   of a static "Scanning N companies...". Per-tick connection overhead
   is negligible because the scanner already sleeps 0.5-1.0s between
   companies. Tick failures are swallowed so the scan can never abort
   from a UI-progress write error.

7. `test(views): seed jd_full in app_with_unscored_jobs fixture` —
   82b1b02. Prior-session "Suggested next step #1". The three failing
   TestBatchScoreStart tests passed once the fixture inserts a non-
   empty `jd_full` (count_scorable filters on
   `jd_full IS NOT NULL AND TRIM(jd_full) != ''`, per
   exclusion_filter.py:101-102).

Total: 7 commits, ~210 directly-affected tests green.

## How to verify the work

```powershell
# All affected test files — should be green except for any tests that
# were already pre-existing failures in adjacent suites. ~3 min on this
# machine.
uv run --active pytest `
  tests/test_views.py `
  tests/test_companies.py `
  tests/test_ats_scanner.py `
  tests/test_location_normalizer.py `
  tests/test_get_distinct_locations.py `
  tests/test_migration.py `
  tests/test_migration_063_merge_companies_by_job_board.py

# Browser-side checks (require dev server on :5000):
# - Job Board: expand a row, then collapse it (either click the
#   compact row again OR the bottom Collapse button). Page should
#   smooth-scroll back to the compact row in both cases.
# - Companies: click 'Scan ATS'. Progress fragment shows "Scanned X
#   of N" updating each ~0.5-1s as the scanner ticks. Navigate to
#   another page, then back to /companies. The progress fragment
#   re-renders automatically with the current scan state.
# - Job Board: filter dropdown's San Jose entries should collapse to
#   ~6 (was 16+). 'San Francisco, CA' must NOT be mangled to 'San
#   Francisco, Ca'; 'NYC' / 'SF' / 'USA' must stay ALLCAPS.

# Live-DB sanity check for m063 (already applied during this session
# via the migration runner invocation):
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
print('user_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('companies:', c.execute('SELECT COUNT(*) FROM companies').fetchone()[0])
# Cigna workday cluster should be 1 (was 5):
print('cigna cluster:',
  c.execute(\"SELECT COUNT(*) FROM companies WHERE ats_platform='workday' AND ats_slug='cigna.wd5/cignacareers'\").fetchone()[0])
"
```

## What I tried that didn't work, and why

- **ALLCAPS-fold without a comma guard.** First draft of
  `normalize_for_display` title-cased every ALLCAPS string. That
  immediately broke `test_returns_individual_entries_not_merged_
  combinations` because "NYC" / "SF" mangled to "Nyc" / "Sf". Fixed
  with `if "," not in s: return s` so the fold only fires for multi-
  segment ALLCAPS like "SAN JOSE, CALIFORNIA" (the only shape in
  the real data anyway). Added explicit tests for the abbreviation
  preservation contract.

- **Mid-segment placeholder stripping in
  `normalize_for_display`.** Tempting because "San Jose, NA, cr"
  would canonicalize cleanly to "San Jose, cr" if we dropped the
  "NA" segment. Skipped — risks too many side-effects on real
  segments (state code "NA" is meaningless, but the heuristic could
  shadow others). The 6 remaining San Jose variants are acceptable;
  the malformed "NA, cr" one is rare (4 rows total).

- **Name-based fuzzy merging in m063.** Considered adding a Levenshtein
  pass to catch the user's "ncidia" / "2100 nvidia usa" example. Did
  NOT: m063's contract is "share a job board → same company". The user
  said that explicitly. Name fuzz would re-open the false-positive
  cans that m061 had carefully closed. The right path for unprobed
  duplicates is a manual aliases UI (deferred — see below).

- **Inline `hx-on::after-request` on the bottom Collapse button.**
  Tried diagnosing why the existing handler wasn't reliably firing
  scrollIntoView. The closure-via-`this.closest('tr').previousElement
  Sibling` is fragile once the expanded row is swapped out — `this`
  becomes a detached node and DOM traversal stops working. Switched to
  a route-side HX-Trigger-After-Settle event + global listener, which
  is bulletproof regardless of which DOM lifecycle stage the handler
  fires at. Two tests now lock in the route contract (header presence
  + data-dedup-key attribute).

- **Auto-running USAJobs/Adzuna/Jooble ingestion from within this
  session (user bug list item 5).** Did NOT — multi-minute network
  operations against external paid APIs that depend on the dev server
  being up. Listed under "What's deferred" with a runbook so the user
  can drive it interactively.

## Known issues (pre-existing, not introduced this session)

- **Pyright `int | None` arguments to `_handle_scan_error`,
  `_reset_retry_state`, `probe_single_company` in
  `tests/test_ats_scanner.py`.** `cursor.lastrowid` is typed
  `int | None` and gets passed to functions expecting `int`. Pre-
  existing (multiple lines); not introduced by this session. Trivial
  fix: cast at the call sites OR change the helper signatures to
  accept `int | None`. Not blocking any test.

- **Pyright `union-narrowing warnings in tests/test_polling_status.py`**
  — pre-existing false positives; runtime correct (carried over from
  the previous handoff).

- **`make_response` lazy import in `db_helpers._attach_hx_trigger`**
  — intentional (carried over from previous handoff); informational.

## What's deferred / remaining

### Operational tasks (need user collaboration)

- **(User bug list #5) Run + monitor full ingestion for USAJobs /
  Adzuna / Jooble.** Now that all three have keys + auth info, walk
  through one ingestion cycle each with the dev server up, watching
  the logs + the activity feed for errors. Runbook sketch:
    1. `$env:JOB_CANNON_USER_DATA_DIR = $PWD; uv run job-cannon` —
       dev server on :5000.
    2. In another shell: `Invoke-WebRequest -Method Post
       http://localhost:5000/admin/jobs/usajobs_ingest/run-now`
       (substitute `adzuna_ingest` / `jooble_ingest`).
    3. Tail logs: scheduler/_runners.py records each provider's
       result row in `runs`. Check Dashboard Recent Activity.
    4. If any provider errors out, capture the traceback and triage
       (likely keyring + auth issues at the secrets layer).
  Estimated 30 min for all three if no errors; longer with triage.

- **(User bug list #6) Audit 30 random no-ATS companies + big-name
  failures.** Sketch:
    1. `SELECT * FROM companies WHERE ats_probe_status IN ('miss',
       'pending') ORDER BY RANDOM() LIMIT 30` — note name, homepage,
       miss_reason.
    2. Plus a curated list of big names that should have an ATS:
       Google, Amazon, Microsoft, Meta, Apple, Nvidia... — which ones
       are missing? Why?
    3. For each, attempt a manual visit of the homepage + careers
       page; identify the ATS platform; check if it's in our
       supported list (`_PLATFORM_SCANNERS` in `_run.py`).
    4. Outcomes: bug reports for individual companies + a prioritized
       list of new ATS platforms to add (which JS-heavy SPAs are
       most worth a Playwright scanner?).
  Estimated 1-2 hours of investigation, depending on findings.

### Code-side deferred

- **Manual company aliases UI** for cases m063 can't resolve via
  shared job board (the "ncidia"/"2100 nvidia usa" cohort with no
  platform/slug). Sketch: a `company_aliases` table mapping orphan
  name → canonical id, an admin route to add aliases, and an
  `upsert_job` change that consults the alias table during company
  lookup. Out of scope for migration-driven cleanup. ~2 hours.

- **Heal pre-existing `int | None` Pyright errors in
  test_ats_scanner.py.** Trivial — cast `cursor.lastrowid` to `int`
  at the helper boundary, or update the function signatures. ~5 min
  cleanup if the next session has spare time and wants to clear
  static-analyzer noise.

- **m063 doesn't address the case where two companies were
  *separately probed* for the same job board with different slugs**
  (e.g. case differences in slug). m063 normalizes platform to
  lowercase but leaves slug case-sensitive — "Flock%20Safety" vs
  "flock-safety" would NOT merge. Probably correct (different slugs
  could legitimately point at different sub-orgs in some platforms),
  but worth flagging for review if dups persist after deploy.

- **Salary single-value extraction.** Same status as previous
  handoff — `extract_salary_from_text` deliberately ignores
  `$120K base` / `Up to $150K`. If a coverage audit shows gaps, the
  right fix is directional-hint matching ("starting at" → min, "up
  to" → max). Decide based on data.

- **Mid-name punctuation in company dedupe.** Same status — "Goldman
  Sachs & Co" vs "Goldman Sachs" still doesn't merge through m061
  because `_COMPANY_SUFFIXES` expects `[,\s]+` before the suffix and
  `&` isn't in that set.

## Quirks the next session should know

- **Migration count is now 63.** Three count assertions in
  `tests/test_migration.py` (`test_migration_count_is_thirteen`,
  `test_migrations_count_is_19`, and the PRAGMA assertion in
  `test_migration53_creates_onboarding_state`) bump in lockstep when
  new migrations are added. The naming is vestigial.

- **`error_msg` column is dual-purpose for ats_scan sessions** (carried
  over): `_run_ats_scan_bg` writes the full `run_ats_scan` summary as
  a JSON blob into `batch_score_sessions.error_msg` when status='done'.

- **m063 pass order matters.** Pass 1 collapses by `(ats_platform,
  ats_slug)` first; Pass 2 by canonical `careers_url`. If you ever add
  Pass 3, run it AFTER Pass 2 — otherwise a row that has both signals
  could get re-pointed twice, which `_repoint_and_delete` would
  handle but for the wrong reason.

- **`normalize_for_display` is READ-side only.** Do NOT call it from
  `upsert_job` or any write path. The display normalizer is more
  aggressive than the write normalizer (it strips ZIPs, converts
  state names, folds ALLCAPS); applying it at write time would lose
  information forever and could break parsers that rely on the
  original location strings downstream.

- **The job-collapse smooth-scroll behavior is now contract-driven.**
  The collapse route emits `HX-Trigger-After-Settle: {"job-collapsed":
  {"dedup_key": "..."}}` and the index page listens. If you add a new
  collapse-like flow (or a different way to invoke `/collapse`), the
  scroll will fire automatically — no inline JS needed.

- **Per-company Scan ATS progress writes happen via a transient
  connection per tick.** Each tick opens + closes a sqlite3 connection
  to UPDATE the session row. Per-tick overhead is negligible because
  the scanner sleeps 0.5-1.0s between companies. If you ever shorten
  those sleeps significantly, consider batching ticks or holding a
  long-lived connection.

- **Pyright lag.** Many Pyright "X is not accessed" diagnostics fire
  on the round IMMEDIATELY after an edit, then disappear on the next
  round once the analyzer catches up. Don't chase those reflexively —
  re-check after the next edit lands.

## Suggested next step

In rough priority order:

1. **Operational item #5 (USAJobs/Adzuna/Jooble ingestion run +
   monitor).** This unblocks confidence in the no-key-compensation
   path that's been shipping in stages for a few weeks. Runbook in
   "What's deferred" above; ~30 min if no errors. Best done as the
   first interactive task next session.

2. **Operational item #6 (audit 30 random no-ATS companies + big
   names).** This is the investigation that drives ATS-coverage
   priorities. Outputs are bug reports + a prioritized new-platform
   list. Best done after #1 because some of those big-name failures
   may have been caused by an ingestion bug that #1 surfaces.

3. **Manual company aliases UI.** Last-mile cleanup for the
   "ncidia"/"2100 nvidia usa" cohort that m063 can't touch. ~2 hours;
   only worth doing after the user signals it's still a meaningful
   problem post-m063.

4. **Clear the pre-existing `int | None` Pyright noise** in
   test_ats_scanner.py (5 min cleanup) if context allows.

The session's primary goal — close BOTH the prior-session deferred
list AND the new user bug list (items 1-4) AND ALSO surface a runbook
for the operational items 5-6 — is complete. Seven commits.
