# FOLLOWUPS — 2026-05-27 round 5 (drift fixes #2 + #3 shipped; Op #6 audit closed)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round-4 documented three handoff-runbook drift items and
closed operational #5. Round 5 (this session) shipped drift fixes
#2 and #3 with tests, then ran the round-3-promised Op #6 audit
(no-ATS company investigation) and produced the actionable findings
in `.planning/ATS-COVERAGE-AUDIT-2026-05-27.md`.

## What this session shipped

Commits, in order (newest first):

1. `docs(planning): ATS coverage audit (Op #6 deliverable)` — Op #6
   audit findings landed in `.planning/ATS-COVERAGE-AUDIT-2026-05-27.md`.
   Headline: 2568/3761 (68%) of companies are `ats_probe_status='miss'`
   with `miss_reason` empty for 99.8% of them. Two scanners would
   unlock immediate coverage (Pinpoint = 22 companies already tagged;
   Jobvite = 7+ companies via careers_url hostname). FAANG-class
   false positives confirmed (Microsoft=bamboohr, Amazon=personio,
   Meta=recruitee — all with NULL ats_evidence). Five concrete bug
   reports (B1-B5) + a prioritized 6-platform scanner roadmap.

2. `feat(scheduler): include portal_*_fetched keys in scheduled_sync
   metadata` — 0792dfb. Closed drift #3 from round-4. The
   cron+admin-triggered path previously dropped per-portal counts
   from `action='scheduled_sync'` activity metadata; now mirrors the
   dynamic-scoop pattern from the manual `action='sync'` path.
   Adding a new portal_search_source fetcher no longer requires
   touching the scheduler. Tests in TestScheduledSyncPortalMetadata
   cover both (a) per-portal keys propagate including explicit-zero
   values and (b) no-portals summary leaves metadata clean.

3. `feat(ingestion): log portal_search aggregate to runs table` —
   b93a63a. Closed drift #2. `pipeline_runner` now writes a
   `source='portal_search'` row in the runs table when
   `portal_search_fetched > 0` or `portal_search_errors`. Mirrors
   the gmail/imap/serpapi/dataforseo log_run pattern. One test in
   TestPerPortalBreakdown locks the contract.

4. `docs(followups): close Op #5 + document handoff runbook drift`
   — a00f246 (round-4 handoff itself).

Drift item #1 (no per-provider scheduler job IDs) deliberately not
"fixed" — that's a documentation bug, the single-ingestion-poll
architecture is correct. Round-4 handoff already documents the
correct invocation.

## How to verify

```powershell
# Both fixes' tests:
uv run --active pytest `
  tests/test_ingestion.py::TestPerPortalBreakdown::test_portal_search_logged_in_runs_table `
  tests/test_scheduler.py::TestScheduledSyncPortalMetadata -v

# After next scheduled or admin-triggered ingestion, the runs table
# will have a portal_search row + scheduled_sync activity row will
# carry per-portal keys:
uv run --active python -c "
import sqlite3, json
c = sqlite3.connect('jobs.db')
# Latest runs row for portal_search (will appear after next 0/8/16 cron
# tick OR next /admin/jobs/ingestion_poll/run-now trigger):
row = c.execute('SELECT * FROM runs WHERE source=\"portal_search\" ORDER BY id DESC LIMIT 1').fetchone()
print('latest portal_search runs row:', row)
# Latest scheduled_sync metadata (look for portal_*_fetched keys):
row = c.execute('SELECT metadata FROM user_activity WHERE action=\"scheduled_sync\" ORDER BY id DESC LIMIT 1').fetchone()
print('latest scheduled_sync portal keys:', {k: v for k, v in json.loads(row[0]).items() if k.startswith('portal_')})
"

# Audit deliverable:
cat .planning/ATS-COVERAGE-AUDIT-2026-05-27.md  # 6.5 KB, fully self-contained
```

## What I tried / considered but didn't do

- **"Fix" drift #1 by adding per-provider scheduler job IDs.** Round-4
  already recommended skipping. The architecture (one `ingestion_poll`
  bundling all sources) is intentional. Adding query-string filters
  or N scheduler jobs would just expand API surface for a single-user
  app. Skipped per the round-4 open-question default.

- **Use the pre-existing `_make_app` helper for the new scheduler
  tests.** Crashed on `app.config.get = lambda` — pre-existing bug in
  the helper that only fires with `testing=False`, and no other test
  exercises that path (every other test passes testing=True and exits
  early). Worked around with a `_minimal_app` method in the new test
  class. Did NOT fix `_make_app` because the bug is dormant (silent)
  and out of scope. Worth a separate cleanup PR.

- **Wait for the FOLLOWUPS-verification scoring run to finish before
  writing the round-5 handoff.** Scoring is still grinding through
  279 jobs at ~14s each (~50 min remaining at time of writing). The
  outcome is independent of the audit and the fixes already
  committed; no need to block.

- **Apply the B1 (FAANG FP) reset migration this session.** Tempted —
  it's a small migration — but it touches user data (resets
  ats_platform/ats_slug on Microsoft/Amazon/Meta). The user
  should see the audit first and confirm the reset criteria. Listed
  under "What's deferred."

## What's deferred / remaining

### Audit-derived (NEW this round — see `.planning/ATS-COVERAGE-AUDIT-2026-05-27.md`)

In priority order:

- **Pinpoint scanner** (`_platforms_pinpoint.py`) — unlocks 22
  companies already tagged: kpmg, medstar, thorne, techinsights,
  nuvitek. Pinpoint has a public job-board API; follows the
  Ashby skeleton. ~150 lines. Highest leverage of any single change.

- **Jobvite scanner** — 7 companies pointing at jobs.jobvite.com.
  Documented API; SMB-enterprise mix. ~150 lines.

- **Populate `miss_reason`** (audit B4). 99.8% of misses have an
  empty reason, blocking any further analysis. Categorize as
  `no_homepage` / `homepage_unreachable` / `homepage_no_ats_fingerprint`
  / `platform_slug_404` / `platform_slug_blocked` / `unknown_custom_ats`.
  Thread through the probe path. Makes every future ATS audit
  meaningfully cheaper.

- **B1 reset migration**: NULL out `ats_platform`/`ats_slug` for the
  FAANG FP cohort (rows where `jobs_found_total > 0` AND
  `ats_evidence_trigger IS NULL` AND `platform IN ('bamboohr',
  'personio', 'recruitee', 'breezy')`). Surgical migration —
  estimated 5-10 rows affected. Pair with cohort-bias gate so the
  next probe doesn't recreate them.

- **B2 reprobe pass**: 6 companies already point at
  `jobs.ashbyhq.com` / `careers.smartrecruiters.com` URLs but didn't
  get tagged. Add hostname-pattern fast-path to the probe and rerun
  on those 1036 no-platform-but-has-careers-url rows.

- **Workable + Breezy + Paylocity + Rippling scanners** (lower
  priority — small absolute counts each but cumulative coverage).

- **AI-nav recipes for in-house ATS** — Apple, Tesla, Oracle, AMD,
  ByteDance, TikTok, Deloitte, Genentech, Citi, Kaiser. Each is a
  one-off Tier-4 recipe. Cheaper than Playwright per-company.

### Code (carried forward)

- **Manual company aliases UI** (round-3 deferred). Still relevant —
  audit B3 confirmed 3 salesforce / 2 nvidia / 2 amazon duplicate
  cohorts that m063 can't resolve via shared job board.

- **Pyright `int | None` cleanup** in test_ats_scanner.py. ~5 min.

- **`_make_app` helper bug** in test_scheduler.py (the
  `app.config.get = lambda` assignment on a real dict). Dormant —
  every existing call passes `testing=True` and exits early. ~10
  min cleanup; would simplify writing more scheduler integration
  tests in the future.

- **m063 slug-case-sensitivity edge case**, **salary single-value
  extraction**, **mid-name punctuation in company dedupe** — all
  carried forward unchanged from round 4.

## Quirks the next session should know

All round-3 + round-4 quirks still apply. Adding:

- **The dynamic portal-scoop pattern is now in TWO places.** Both
  `blueprints/sync.py::_run_sync_bg` (action=`sync`, manual UI path)
  AND `scheduler/_jobs.py::register_ingestion::run_pipeline`
  (action=`scheduled_sync`, cron+admin path) loop over
  `summary.items()` to pick up `portal_<name>_fetched` keys. If you
  ever change the per-portal counter shape in
  `ingestion_runner._fetch_portal_search` line 488-492, both call
  sites need to stay in sync. `test_log_activity_scoops_dynamic_portal_keys`
  + `test_per_portal_keys_appear_in_scheduled_sync_metadata` cover
  the contract.

- **`portal_search` is now a runs source.** The runs table previously
  contained only {gmail, imap, serpapi, dataforseo, ats_scan,
  careers_crawl, *_parse_failure} as source values. After this
  round, `portal_search` is a new value. Anything that aggregates
  by source (analytics, dashboard charts) needs to handle it.

- **`ats_evidence_trigger IS NULL` is a strong false-positive
  signal.** Any row with `ats_probe_status='hit'` AND
  `ats_evidence_trigger IS NULL` is suspect — the probe assigned a
  platform without recording why. Useful as a corroboration gate
  in future probe changes.

- **`miss_reason` is mostly empty.** The handful of populated
  values are `blocked_brand` only. Don't trust the column for
  diagnostics until B4 is fixed.

## Suggested next step (in priority order)

1. **Pinpoint scanner.** Highest leverage of anything on the board —
   22 known companies unlocked, 150-line file mirroring Ashby. The
   audit doc spells out the approach.

2. **Populate `miss_reason`** (audit B4). Every future ATS audit
   pays this back in less guesswork.

3. **B1 reset migration + cohort-bias gate.** Surgical, makes the
   probe trustworthy on famous brands again.

4. **Jobvite scanner.** Second-largest new-platform unlock.

5. The rest of the audit roadmap (Workable / Breezy / Paylocity /
   Rippling / AI-nav recipes for big in-house systems).

## Open questions

- Is the user OK with the reset-migration approach for B1 (the
  FAANG FP cohort)? Alternative: leave the wrong platform tags in
  place and add a UI badge "needs reverify" — less destructive but
  more carrying-cost.

- Order of operations for the scanner roadmap: implement Pinpoint
  first (already-tagged 22 unlocks) or implement the hostname-pattern
  probe fast-path first (reclassifies ~6 already-mis-tagged Ashby/
  SmartRecruiters companies + lets B2 cleanup land before B1)?
  Default recommendation: Pinpoint first — it's a pure addition with
  no risk to existing data.
