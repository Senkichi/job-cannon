# FOLLOWUPS — 2026-05-27 round 8 (ATS scan timeout fix + location SPEC + jobvite pivot)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 7 shipped 4 new ATS platforms + 4 audit-driven probe fixes
(B1–B4) and deferred the two remaining user bugs (ATS scan 30-min
timeout, location parsing overhaul) explicitly per user instruction.
Round 8 (this session) tackled all three deferred items at once: the
timeout is fixed architecturally (heartbeat instead of wallclock), the
location parsing work is fully spec'd and ready for implementation, and
the "real Jobvite scraper" item pivoted to an architectural fix when
investigation revealed the original framing was wrong.

## What this session shipped

Commits, in order (newest first):

1. `185cb6e` — `fix(ats_probe): drop jobvite from URL fast-path
   (careers_crawler conflict)`. Removed `"jobvite"` from
   `_URL_FASTPATH_PLATFORMS` in `_probe.py`. The detection regex and the
   stub scanner stay as defensive no-ops. **Why:** investigation found
   that promoting a jobvite tenant to `ats_probe_status='hit'` excludes
   it from `careers_crawler/__init__.py:226` (`ats_probe_status !=
   'hit'`), removing the only Playwright-capable data path. With
   jobvite out of the fast-path, the 7 jobs.jobvite.com careers_url
   companies stay at `status='miss'` and become eligible for the
   careers_crawler Tier-3 Playwright crawl. +2 invariant tests
   (jobvite-excluded), -1 test split into 2.

2. `5140fa4` — `fix(polling): heartbeat-based staleness check, not
   wallclock elapsed`. `render_polling_status` in `db_helpers.py` now
   compares `(now - COALESCE(last_tick_at, started_at))` against
   `cfg.timeout_minutes`. Both tick sites
   (`companies.py:_run_ats_scan_bg._tick` and `batch_scoring.py`'s two
   progress flushes) write `last_tick_at` on every update. Error
   message changed `"Session timed out (>N min)"` →
   `"No progress in >N min"` to match the new semantics. +4 heartbeat
   behavior tests in `test_polling_status.py`.

3. `457ebe9` — `feat(migrations): m065 add last_tick_at heartbeat
   column`. Adds nullable `last_tick_at TEXT` to `batch_score_sessions`.
   COALESCE fallback preserves backward compatibility for pre-m065
   rows and just-started sessions that haven't ticked yet. +11 m065
   tests. Bumped `len(MIGRATIONS) == 65` at 3 sites
   (test_migration_invariants.py + test_migration.py:402 + :934 + the
   TestMigration52And53 assertion at :1383).

Plus uncommitted in `.planning/` (project convention — that tree is
gitignored; see "Quirks" below):

- `.planning/SPEC-location-parsing.md` — implementation-ready SPEC for
  user bug 2 with canonical `JobLocation` shape, 3-layer parser
  architecture (trust-structured-ATS / gazetteer / heuristic), m066
  + m067 migration plan, anchor test corpus, and 5-commit breakdown.
- `.planning/location-parsing-research.md` — research findings:
  libpostal/pypostal is a Windows install dead-end (MSVC build fails,
  2 GB model), `geonamescache` + `pycountry` is the pure-Python combo,
  Nominatim's 4 req/min bulk ceiling kills it for batch ingestion,
  LinkedIn workplaceType is `REMOTE`/`HYBRID`/`ONSITE`, ATS data
  quality ranking (SmartRecruiters → Ashby → Lever → Greenhouse →
  Workday), schema.org JobPosting field mapping.

Full test impact: **+17 new tests** across 3 files
(test_migration_065_*.py +11, test_polling_status.py +4 heartbeat,
test_round6_ats_scanners.py +2 jobvite-excluded). Test sweep
(413 tests across affected modules) all green.

## How to verify (this session's work)

```powershell
# All round-8 commits' tests:
uv run --active pytest `
  tests/test_migration_065_add_polling_session_heartbeat.py `
  tests/test_polling_status.py `
  tests/test_round6_ats_scanners.py `
  tests/test_migration_invariants.py `
  tests/test_speculative_probe_consistency.py `
  -v

# m065 applied to live DB:
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
print('user_version =', c.execute('PRAGMA user_version').fetchone()[0])
cols = [r[1] for r in c.execute('PRAGMA table_info(batch_score_sessions)').fetchall()]
print('last_tick_at present:', 'last_tick_at' in cols)
"
# Expected: user_version=65, last_tick_at present: True

# Jobvite excluded from fast-path:
uv run --active python -c "
from job_finder.web.ats_scanner._probe import _URL_FASTPATH_PLATFORMS
print('jobvite in fast-path:', 'jobvite' in _URL_FASTPATH_PLATFORMS)
print('fast-path platforms:', sorted(_URL_FASTPATH_PLATFORMS))
"
# Expected: jobvite in fast-path: False (15 platforms total)

# Heartbeat fix end-to-end (manual, requires UI):
# 1. Trigger an ATS scan via /companies UI button.
# 2. Watch the progress fragment; verify it does NOT flip to error
#    after 30 minutes if the bg thread is still ticking.
# 3. After completion, check batch_score_sessions row for the run:
#    SELECT status, last_tick_at, started_at FROM batch_score_sessions
#    ORDER BY id DESC LIMIT 1;
#    -- last_tick_at should be populated and recent.
```

The next ingestion + probe cycle will:
- **Re-verify the round-7 B2 fast-path** for workable / paylocity /
  rippling (4 + 4 + 3 careers_url companies tagged via URL evidence
  per round-7 expectation).
- **Leave the 7 jobs.jobvite.com companies at `status='miss'`** (round 8
  removed jobvite from the fast-path) so careers_crawler can attempt
  Playwright extraction.
- **Pick up heartbeat ticks** on the ATS scan + any batch scoring runs.
  Watch the live `batch_score_sessions.last_tick_at` column populate.

## What I tried / considered but didn't do

- **Increase ATS scan `timeout_minutes` to a bigger constant.** Tempting
  one-line fix, but doesn't fix the underlying invariant. The right
  signal is "no tick recently," not "elapsed wallclock since start."
  Bigger numbers just delay the same broken behavior. Went with the
  heartbeat-based architectural fix instead.

- **Build a real Jobvite scraper.** Original handoff scoping. Investigation
  found jobs.jobvite.com is a client-side JS app (no embedded JSON, no
  `__INITIAL_STATE__`, no public unauthenticated JSON API). All API
  endpoint guesses returned 302 or 404. Real scraping needs Playwright
  — which the careers_crawler Tier-3 already does for arbitrary JS
  sites. Building a dedicated scanner would duplicate infrastructure
  AND tag companies as `ats_platform='jobvite'` which removes them from
  careers_crawler eligibility. Architecturally wrong — pivoted to
  removing jobvite from the URL fast-path entirely (Option A in the
  user check-in). Some tenants also redirect to custom domains
  (Victaulic → careers.victaulic.com); the-institutes redirects to an
  `invalid=1` page (slug appears to no longer be live).

- **Add migration m066 to reset existing `ats_platform='jobvite'`
  rows.** Live DB has 0 such rows (the round-7 B2 fast-path hadn't run
  yet to tag them), so a reset migration would be cosmetic. Skipped to
  keep scope tight.

- **Delete the jobvite stub scanner + detection regex.** Considered for
  cleanup but kept as defensive: the regex still recognizes jobvite
  URLs (useful for stats / dashboards / future re-enablement) and the
  stub stays in `_PLATFORM_SCANNERS` so any pre-existing tagged row
  doesn't blow up with "unknown platform." Minimal-scope discipline.

- **Implement the location parsing SPEC inline.** It's a 5-commit /
  900-1100-LOC effort with new dependencies (`pycountry` +
  `geonamescache`) and 3 layers of parsing logic. Way over a single-
  session budget. Spec'd it instead and broke into 5 ordered commits
  with anchor test corpus.

- **Apply Pyright `int | None` cleanup in test_ats_scanner.py.** Still
  on the carry-forward list; not relevant to this session.

- **Apply `_make_app` helper fix in test_scheduler.py.** Still dormant.
  Round 7 noted it, round 8 still hasn't touched it.

## What's deferred / remaining

### NEW top priority (next session): verify the round-8 pivots in production

1. **Verify careers_crawler successfully extracts jobs from
   jobs.jobvite.com tenants via Playwright Tier-3.** After the next
   careers_crawl scheduled run (5:00 AM daily), inspect
   `company_scan_log` for the 7 jobvite-URL companies — did Playwright
   render the JS app and pick up jobs? Concrete check:
   ```sql
   SELECT c.name, c.careers_url, csl.jobs_matched, csl.created_at
   FROM company_scan_log csl
   JOIN companies c ON c.id = csl.company_id
   WHERE c.careers_url LIKE '%jobvite%'
   ORDER BY csl.created_at DESC LIMIT 20;
   ```
   If `jobs_matched > 0`, the round-8 architectural fix is complete.
   If `jobs_matched = 0` across all 7 after multiple crawl cycles,
   the jv-job-list rendering needs per-tenant tuning — either custom
   `careers_nav_recipe` entries or an AI-nav recipe per tenant. (See
   ai_career_navigator — round-5 retained.)

2. **Verify heartbeat-based timeout on a real long scan.** Trigger an
   ATS scan via the /companies UI. With 908 hits + 303 pending in the
   live DB, the scan should take ~30-60 min minimum (per round-7
   diagnostic). Watch the UI: progress fragment should keep showing
   "Scanned N of M" past 30 minutes without flipping to error. After
   completion, confirm `batch_score_sessions.last_tick_at` is populated
   and `error_msg` is NULL. (If still flipping to error, something in
   the COALESCE / column-add chain is wrong — bg thread isn't writing
   the tick.)

### NEW priority (separate session): implement the location parsing SPEC

3. **Implement the location parsing SPEC.** Fully scoped at
   `.planning/SPEC-location-parsing.md`. 5 ordered commits:
   - **Commit A** — Add deps (`pycountry`, `geonamescache`), write
     `location_canonical.py` + `location_parser.py` (Layer 1/2/3 logic).
   - **Commit B** — Migration m066 (column add: `locations_structured`,
     `workplace_type`, `primary_country_code`) + invariant bumps to 66.
   - **Commit C** — Wire `upsert_job` + 4 Layer-1 scanners (Ashby,
     Lever, SmartRecruiters, Rippling).
   - **Commit D** — Read-side: dropdown additions for country +
     workplace_type, Jinja filter `format_canonical_location`, template
     pill renderer.
   - **Commit E** — Migration m067 (backfill — runs AFTER one ingestion
     cycle of fresh writes have been verified clean).

   Budget: 1-2 sessions depending on Layer-2 edge cases. ~900-1100 LOC
   including tests. Anchor test corpus has 10 strings (`"San Francisco,
   CA / Remote"`, `"Multiple Locations"`, etc.) that MUST PASS.

### Audit-track follow-ups

4. **Workable widget endpoint shape verification.** From round 7:
   Datadog + Canonical fetches returned empty `jobs: []` from the
   widget endpoint. After the next ATS scan cycle hits the 4
   careers_url-tagged Workable companies, if all return 0 jobs, switch
   to a different endpoint (`apply.workable.com/api/v3/...` — 404'd in
   round-6 exploration but the docs claim it's the v3 path).

5. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Each is a per-company AI-nav recipe
   (Tier-4 crawler). Lower priority than the platform scanners.

### Code (carried forward unchanged from rounds 4-7)

6. **Manual company aliases UI** (round-3 deferred). m063 can merge
   by shared job board but not by name alone; salesforce/nvidia/amazon
   duplicate cohorts need manual aliasing.

7. **Pyright `int | None` cleanup** in test_ats_scanner.py. ~5 min.

8. **`_make_app` helper bug** in test_scheduler.py (the
   `app.config.get = lambda` assignment on a real dict). Dormant.

9. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried forward.

10. **Real Jobvite scraper for tenants the careers_crawler can't
    handle.** Only if step 1 (verification) shows careers_crawler
    misses some tenants. Likely candidates: Victaulic (redirects to
    careers.victaulic.com), the-institutes (slug appears dead). Each
    needs either a custom-domain scraper or a `careers_url` fix in
    the companies table.

## Quirks the next session should know

All round-3 through round-7 quirks still apply. Adding from round 8:

- **`.planning/` is gitignored.** The SPEC and research files for
  location parsing live there and won't show up in `git log`. Read
  them directly with `Read` on `.planning/SPEC-location-parsing.md`
  and `.planning/location-parsing-research.md`. The handoff
  (FOLLOWUPS.md) is committed; everything in `.planning/` is local.

- **Polling timeout is now tick-based, not wallclock-based.** The
  `cfg.timeout_minutes=30` default in `PollingSessionConfig` now
  means "no tick received in 30 min" (not "started 30 min ago").
  Any new polling route added via this helper should either tick
  via the `last_tick_at` UPDATE pattern (see
  `companies.py:_run_ats_scan_bg._tick` for the template) OR
  acknowledge that NULL `last_tick_at` falls back to `started_at`
  semantics (preserves the old behavior for routes that don't tick).

- **`_URL_FASTPATH_PLATFORMS` has a load-bearing exclusion for
  jobvite.** Defined in `job_finder/web/ats_scanner/_probe.py:91`.
  The companion test
  `TestDispatcherWiring.test_jobvite_excluded_from_fastpath_set`
  guards the invariant. Do NOT re-add jobvite without also: (a) doing
  something useful in the scanner (currently a stub) AND (b) handling
  the careers_crawler exclusion-by-status-hit problem
  (`careers_crawler/__init__.py:226`).

- **The detection regex `_JOBVITE_HUMAN_URL` in `ats_detection.py`
  still recognizes jobs.jobvite.com URLs.** `extract_ats_from_url_best`
  will return `("jobvite", slug, 5)` for matching URLs. The B2
  fast-path's gate (membership in `_URL_FASTPATH_PLATFORMS`) is what
  prevents promotion — not regex absence. This is intentional so
  future stats / dashboards can still attribute jobvite URLs without
  promoting them.

- **m065's `last_tick_at` column is NULL on every existing row in the
  live DB** (the column was just added, no tick writes have run yet).
  The COALESCE in `render_polling_status` falls back to `started_at`
  for these. Once any new scan ticks the column, the heartbeat path
  takes over for that row. Backward-compatible by construction.

- **3 hardcoded `len(MIGRATIONS) == N` assertions exist in the test
  suite**, all bumped to 65 this round
  (`test_migration_invariants.py:27`, `test_migration.py:402`,
  `test_migration.py:934`). Plus a fourth at
  `test_migration.py:1383` (`assert version == N` not
  `len(MIGRATIONS) == N`, but same maintenance burden). Bump all four
  whenever you add a migration.

## Suggested next step (in priority order)

User has cleared the original round-7 deferred items in 3 sequential
sessions. The two highest-value next things are pure verification work
(post-pivot reality check), then the location parsing implementation.

1. **Verify careers_crawler picks up jobvite-URL companies via
   Playwright** — a single SQL check after the next 5:00 AM crawl plus
   one manual ATS scan to confirm the heartbeat fix end-to-end. Could
   be a 15-minute session.
2. **Implement the location parsing SPEC** — 1-2 sessions of meatier
   work; the SPEC is implementation-ready so the planning lift is zero.

If both verifications come back clean and location parsing is in
flight, the next-highest-value cleanup is **#4 (Workable widget
endpoint verification)** — same shape as #1: wait for next scan, check
the 4 careers_url-tagged Workable companies for jobs_matched.

## Open questions

- **Does careers_crawler's Playwright Tier-3 actually extract jobs from
  jobs.jobvite.com tenants?** Honest unknown until next crawl cycle.
  The Tier-3 path handles arbitrary JS-rendered sites; the `jv-job-list`
  pattern should be extractable via the existing
  `_extract_jobs_from_soup` after JS settles. But if Jobvite uses
  client-side virtualization (only renders visible jobs) or requires
  search-form interaction to show jobs, we'd need to extend the
  interactive enricher or fall back to AI-nav. Sub-question: do any
  tenants completely redirect away (Victaulic does), and if so does
  the crawler follow the redirect and find jobs at the destination?

- **Should the heartbeat tick interval be tightened?** Currently the
  bg thread ticks every company (~8 s in worst case). A scan that
  legitimately stalls for 30 min on a single slow company would still
  trip the timeout. That's correct (the company IS hung), but maybe
  the per-company HTTP timeout should be lower than the global
  heartbeat threshold. Currently both happen to be in the same range
  (8 s probe vs 30 min stale). Not urgent — only matters if a real
  scan ever trips this.

- **the-institutes slug appears dead** (302s to `invalid=1`). Their
  careers_url should probably be updated or the company merged out.
  This is a data issue, not a code issue — flag for manual cleanup
  in the companies UI.
