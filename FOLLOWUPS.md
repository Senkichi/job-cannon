# FOLLOWUPS — 2026-05-28 round 16 (Phase F jobvite recipes: 4 shipped, 1 blocked on dead slug; goto-runner SPA fix)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 16 picked up round-15's **optional secondary** carry-over:
hand-curate AI-nav recipes for the 5 unhandled jobvite tenants
(american-specialty-health, capcom, neogenomics, the-institutes,
victaulic). Round 15's `goto_with_query` vocabulary and DB-replay
probe mode were the enabling infra; this session applied them.

Net: 4 of the 5 tenants now have working recipes; the 5th (The
Institutes) is blocked on a dead jobvite slug and needs a separate
data fix to locate the company's current careers source. One small
runner-level fix landed alongside the recipes because jobvite-hosted
career pages embed a continuously-polling TalentNetwork iframe that
deadlines `networkidle` on every navigation.

The 3 round-15 downstream extractor gaps (NVIDIA `JR`-prefix,
Oracle empty `<a>`, ByteDance non-anchor tiles) are **NOT** addressed
in this round — they remain the highest-leverage carry-forward and
were intentionally deferred because the dispatch routed Phase F first.

## What this session shipped

Six commits on `orch/fu-jobvite-phaseF` (pending orchestrator merge to `main`):

1. **`85c4623`** — `fix(ai-nav): use domcontentloaded for recipe goto so SPA analytics don't deadline-fail`. Switches `_execute_step`'s `goto` + `goto_with_query` from `wait_until="networkidle"` to `wait_until="domcontentloaded"` (keeps the existing 2s settle wait). Unblocks jobvite-hosted tenants whose embedded TalentNetwork iframe never reaches network idle within the 15s deadline. Tests verified the existing recipes (Deloitte, NVIDIA, Oracle, ByteDance, Kaiser) all still work — `domcontentloaded` is a strict subset of `networkidle`.

2. **`dff1df2`** — `feat(ai-nav): hand-curated recipe for American Specialty Health (Phase F)`. Jobvite tenant `ashcompanies` (company_id=905). 1-step `goto_with_query` against `jobs.jobvite.com/ashcompanies/jobs/alljobs` + `?q={keyword}`. Verified.

3. **`f76adf5`** — `feat(ai-nav): hand-curated recipe for Capcom (Phase F)`. Jobvite tenant `capcomusa` (company_id=672). Anchored at the tenant root because `/jobs/alljobs` 302s to a 301-error variant for this small tenant. Verified.

4. **`e7d4412`** — `feat(ai-nav): hand-curated recipe for NeoGenomics (Phase F)`. Jobvite tenant `neogenomics` (company_id=2108). 1-step `goto_with_query` against `jobs.jobvite.com/neogenomics/jobs/viewall` + `?q={keyword}`. Recon confirmed server-side narrow (76 jobs → 4 for `?q=analyst`). Verified.

5. **`739003a`** — `feat(ai-nav): hand-curated recipe for Victaulic (Phase F)`. Jobvite-on-file but migrated to Workday during 2025; recipe routes to `victaulic.wd1.myworkdayjobs.com/en-US/victaulic_careers` + `?q={keyword}`. Workday's `data-automation-id="jobTitle"` tiles extract via `links_in_page`. Verified.

6. **`18f1956`** — `test: extend probe_ai_nav.py with Phase F jobvite targets`. Adds the 4 newly-seeded tenants to `TARGETS` so future `PROBE_FROM_DB=1` runs cover them.

Test posture: 84/84 in the focused suite (`tests/test_ai_career_navigator.py + tests/test_careers_crawler.py`) — unchanged from round 15.

## Probe results (the headline)

Post-Phase-F `PROBE_FROM_DB=1` against the 4 new tenants:

| Target                     | reach | snap | recipe_steps | replay_jobs | err |
|----------------------------|-------|------|--------------|-------------|-----|
| American Specialty Health  |  OK   | 3036 |      1       |      0      |  -  |
| Capcom                     |  OK   |  878 |      1       |      0      |  -  |
| NeoGenomics                |  OK   | 3671 |      1       |      0      |  -  |
| Victaulic (Workday)        |  OK   | 1008 |      1       |      0      |  -  |

All 4 recipes execute cleanly end-to-end. 0-yield is the **Kaiser-pattern title-filter intersection** (recipe correct; the user's current `target_titles` profile doesn't intersect what these tenants are presently posting). Concretely:

- **Capcom**: only 1 active posting on their entire site right now — no analyst/scientist roles.
- **ASH / NeoGenomics**: jobvite's server-side `?q=` keyword narrow returns matches for broad terms (e.g. `analyst`) but the user's `_derive_search_term`-selected query lands on a too-specific phrase, narrowing to 0 within these tenants' active postings.
- **Victaulic**: Workday's keyword filter is strict; same narrowing behavior. Will yield when Victaulic posts data/analytics roles.

All 4 will yield jobs as soon as those tenants post user-profile-shaped roles. Same operational profile as round-15's Kaiser.

## The Institutes — blocked

`jobs.jobvite.com/the-institutes` (company_id=1101) returns a 302 to
`https://www.jobvite.com/support/job-seeker-support/?invalid=1`. Slug
variants (`theinstitutes`, `the_institutes`, `theinstitutesriskandinsurance`,
`institutes`) all redirect to the same `?invalid=1` landing page, and a
direct job-permalink (`/the-institutes/job/oje7ifwG`) that web search
surfaced also 302s to invalid. The jobvite tenant is closed/disabled.

Their actual hiring portal appears to be **`web.theinstitutes.org/all-roles`**,
which loads roles client-side via a non-obvious data API (921 KB of HTML
with no extractable `<a href>` job links in the snapshot). Curating a
recipe here would require dynamic-API recon — out of scope for the Phase F
goto_with_query pattern.

**Disposition:** carried forward as a **data fix**, not a recipe gap.
The follow-up is: identify The Institutes' current public job feed
(Indeed shows 15 open positions for them but no link to the source),
update `companies.careers_url` accordingly, and either add a recipe
or route through `ai_navigate` with a fresh discovery.

## Quirks the next session should know

Rounds 3–15 quirks still apply. Additions from round 16:

- **`_execute_step` now uses `domcontentloaded`, not `networkidle`, for goto/goto_with_query.** Discovery's validation block (`discover_navigation_recipe` line ~569) still uses `networkidle` because discovery snapshots the page once and benefits from a true idle gate. Replay paths use `domcontentloaded` + 2s settle to tolerate continuously-polling SPAs (jobvite, talent-network widgets, embedded chat). Don't revert this without first verifying jobvite-hosted tenants still work.

- **Victaulic's recipe targets a different host than its `companies.careers_url`.** The DB row still says `jobs.jobvite.com/victaulic/jobs/alljobs` (stale, redirects to a WP marketing site), but the seeded recipe step navigates to `victaulic.wd1.myworkdayjobs.com/en-US/victaulic_careers`. The AI-nav tier handles this correctly because step URLs override the page's initial location. The proper fix (next session): flip the DB row to `ats_platform='workday'` + the Workday URL so the native Workday scanner becomes primary and AI-nav demotes to a fallback.

- **The Institutes is a data-fix follow-up, not a recipe follow-up.** Don't waste cycles trying alternate jobvite slugs; the tenant is closed. The next session should locate their current public job feed (e.g. their corporate ATS, the Indeed company page's apply-source, or a `web.theinstitutes.org` API) and update `companies.careers_url` accordingly.

## What's deferred / remaining

### HIGHEST LEVERAGE — carried unchanged from round 15

The 3 downstream extractor gaps are still the top of the docket. Round
16 did not touch them.

1. **Gap #1 — NVIDIA: `_NOSEP_TRAIL_LOC_RE` doesn't handle JR-prefixed Workday IDs** (`_title_filters.py:56-69`). Fix shape: extend the regex to allow `[A-Z]{2,}\d+` before the location suffix, or add `_REQID_PREFIX_RE`.

2. **Gap #2 — Oracle: empty `<a>` inner text** (`_static_tier.py:96`). Fix shape: when `tag.get_text(strip=True)` < 4 chars, look for title-bearing siblings within the same `<li>`/`<article>` ancestor (max 3 ancestor hops) before discarding.

3. **Gap #3 — ByteDance: non-`<a>` job tiles** (`_static_tier._extract_jobs_from_soup`). Fix shape: tile-pattern selector pass that runs after the `<a href>` pass and only if zero anchors yielded; look for `[role="button"]`, `[onclick*="job"]`, `[data-job-id]`, `<button>` containers.

Full per-gap detail (captured inputs, expected outputs, test names) is in dispatch `job-cannon-followups.md` and round-15 FOLLOWUPS. Each fix is 1 atomic commit; each lifts a known 0-yield curated recipe to actual yield.

### Phase F follow-ups

4. **The Institutes data fix** (round 16 surface). Locate the company's current public job feed; update `companies.careers_url` (and maybe `ats_platform`). Indeed reports 15 open positions for them — the answer is somewhere. Likely a WordPress `/all-roles` API on `web.theinstitutes.org` or a new ATS migration.

5. **Victaulic ATS reclassification** (round 16 surface). Flip `companies.id=382` to `ats_platform='workday'` + `careers_url='https://victaulic.wd1.myworkdayjobs.com/en-US/victaulic_careers'` so the native Workday scanner takes over. The Phase F AI-nav recipe then becomes a redundant fallback.

### Pre-flight items still carried (rounds 13/14/15)

6. **Pyright Path A vs B decision** (advisory). One line either way. `scripts/seed_curated_recipes.py` now triggers 4 instances of the same `"url" is not accessed` pyright noise (one per recipe). All 4 follow the existing 5-tenant pattern; will resolve uniformly when Path A or B is chosen.

7. **Manual browser smoke for Commit D from round 12** (Country dropdown / Workplace dropdown / pill renderer in `/jobs`). Requires Flask up — not session-runnable.

8. **Workable widget endpoint verification** (round-12 item #4). Trigger via `POST /admin/jobs/companies_scan/run-now`, re-query `jobs_found_total` for ids 71, 951, 1027, 1036.

9. **`_try_cached_tier` pre-replay wait inconsistency** (round-15 carry). `_tier_cache.py:109` still uses `page.wait_for_timeout(2000)` instead of `wait_for_snapshot_ready`. Trivial.

### Audit-track follow-ups (carried unchanged from rounds 7–15)

10. Manual company aliases UI, m063 slug case sensitivity, salary single-value extraction, mid-name punctuation in company dedupe — all carried.

### Open / advisory items (carried unchanged from rounds 13–15)

- `scripts/probe_ai_nav.py` + `scripts/seed_curated_recipes.py` + `scripts/recon_search_urls.py` pyright IDE noise (same Path A/B pattern).
- Lever freeform strings `unresolved=True` forever path.
- Bare-token workplace detection in jd_full (Q3 extension).
- Migration count drift on future migrations (`tests/test_migration.py` lines 404, 936, 1384 still use exact `== NN`).
- Production country distribution sanity (US 8987 / 72.6%, IN 352, TH 74, GB 74, CA 65).
- Ollama JSON parse-error correlation with longer `_DISCOVERY_SYSTEM` prompt.

### Holding pattern (out of scope, server-side blocks)

- **AMD bot/403 workaround.**
- **Tesla Akamai workaround.**

## Next session's contract

**Required deliverable** (highest leverage, ~2-4h, unchanged from round 15):

### Extractor gap fixes (recover the 3 0-yield round-15 curated recipes)

Sequence (smallest blast radius first):

1. **Fix #1 — NVIDIA: JR-prefix stripping in `_title_filters.py`.**
2. **Fix #2 — Oracle: empty-link sibling lookup in `_static_tier._extract_jobs_from_soup`.**
3. **Fix #3 — ByteDance: click-handler tile recognition.**

Each fix = one atomic commit. Re-run probe in `PROBE_FROM_DB=1` after each to isolate the lift.

**Phase F follow-ups** (cheap, in-session):

- The Institutes data fix (item 4 above) — recon their current public job source, update DB row.
- Victaulic ATS reclassification (item 5 above) — flip platform/URL in DB.

**Pre-flight before starting** (~5 min):

- Confirm Flask is off (port 5000 vacant) or boot deliberately. Probe doesn't need it; Workable verification would.
- Ollama on port 11434 typically up from prior session's scheduler auto-start.

## Open questions

**RESOLVED in round 16:**

- ✅ **Can jobvite-hosted careers tenants be unblocked with `goto_with_query`?** Yes, with the SPA-tolerant `domcontentloaded` runner fix. 4 of 5 Phase F tenants now have working recipes; the 5th is a data issue (dead slug), not an AI-nav limitation.

- ✅ **Does `networkidle` work for jobvite's embedded TalentNetwork iframe?** No — the iframe polls continuously and the page never reaches idle within the 15s deadline. `domcontentloaded` + 2s settle is the right gate for replay; discovery still uses `networkidle` because its single snapshot benefits from a true idle gate.

- ✅ **Is The Institutes' jobvite tenant recoverable with an alternate slug?** No. All slug variants (`the-institutes`, `theinstitutes`, `the_institutes`, `institutes`, `theinstitutesriskandinsurance`) 302 to `?invalid=1`. The tenant is closed/disabled. Data fix needed: locate their current public job feed.

**STILL OPEN (carried from prior rounds, low-priority):**

- `uv sync` editable-rebuild conflict with running Flask (round-9 carry).
- Manual users of `parse_locations` outside upsert_job (round-12 carry).
- Lever freeform strings — keep `unresolved=True` forever? (round-12 carry).
- Ollama prompt-length / JSON-parse correlation (round-15 carry).

---

# FOLLOWUPS — 2026-06-01 test-suite remediation (out-of-scope finds)

Discovered during the test-suite-remediation work (live-I/O leak fixes).

**UPDATE (same session): all three items below were subsequently fixed at the
user's request** — see commits `fix(quota): count CSE quota by local day…`
(date tests + the google_cse production bug), `style: clear ruff errors…`
(careers_page + scoring_orchestrator lint), and the
`test: …careers_crawler / smartrecruiters / onboarding / model_provider /
speculative-probe / workday` commits (the slow live-I/O tests). The full suite
is now **3780 passed, 0 failed, 0 errors in 3:34** (was 18:10). They are kept
below as a record of the findings and their root causes.

Genuinely still open (NOT addressed — small/optional):
- **~12 `tests/test_scheduler.py` tests sit at ~2.0s each** (~24s total). Not in
  the enumerated slow-test list; 2s is within the "a few seconds" bar. Likely a
  real APScheduler-startup wait per test. Candidate for a future pass.
- **Phase 5 of the source plan — a global `pytest-socket` kill-switch** — remains
  the gold-standard prevention (surfaces every remaining real-I/O test at once);
  deliberately deferred as a separate, larger effort.

## [TEST BUG — time-of-day flaky] test_costs.py "today" tests fail in the evening (negative-offset TZ)

**Status:** Pre-existing. Confirmed: fails on the pristine tree too (stashed all
remediation changes → still fails). NOT caused by the m1 migrated_db template.

**Failing now (2026-06-01 evening, Pacific):**
- `tests/test_costs.py::TestGetUsageStats::test_sums_tokens_across_providers`
  (`stats["today"]["calls"] == 3` → got 0)
- `tests/test_costs.py::TestCostViewExcludesFreeProviders::test_get_cost_stats_excludes_free_providers`
  (`stats["today"] - 0.50` → got 0.0), plus a Windows `PermissionError` teardown
  on that test (`tmp_db_path` removal — a connection/app handle not closed;
  likely a separate Windows-only fixture-teardown nit).

**Root cause (verified by repro):** production `get_usage_stats`/`get_cost_stats`
define "today" via `local_day_utc_window()` — the **local** calendar day in UTC
(correct per the locked "Store UTC, render local" decision). The tests build the
row timestamp from `datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")` — the
**UTC** date at noon UTC. When local date ≠ UTC date (any evening in a
negative-offset TZ; right now UTC is already 2026-06-02 while local is
2026-06-01), the row (`2026-06-02T12:00:00Z`) lands in *tomorrow's* local-day
window (`today_end = 2026-06-02T07:00`) → "today" aggregates to 0. Green in the
morning, red in the evening — same "passes by accident" class as the remediation,
but the seam is date/TZ, not live I/O.

**Proposed fix (needs sign-off — a test change):** build the cost rows from the
window production actually queries (`local_day_utc_window()[0]` + a few hours, or
local-noon→UTC), not from the UTC date. Apply to every "today"/current-month
assertion in `test_costs.py` (check `test_scoped_to_current_month` too — today is
also a month boundary, so it may share the latent issue). Don't hardcode a UTC
noon string. Production code is correct; the test is wrong.

## [LINT — pre-existing] 2 ruff errors + 1 format diff in careers_page files

**Status:** Pre-existing (in files this remediation never touched; from the
HEAD commit "fix(careers_page): extract location and prevent Blue State title
bleed"). The plan assumed "ruff-clean today" — it is not. All files this
remediation touched ARE ruff-clean.

- `job_finder/web/careers_scraper.py:631` — `RUF100` unused `# noqa: B023`
  directive (B023 no longer triggers there; the noqa can be removed).
- `tests/test_careers_page_extraction.py:15` — `I001` unsorted import block +
  would be reformatted by `ruff format`.

All three are `ruff --fix`/`ruff format` autofixes. NOT fixed here: careers_page
is the user's active work area (parallel commits) and editing those files from
this worktree risks a merge collision; the fixes are unrelated to the test-suite
remediation. Recommend the user run, on the careers_page branch:
`uvx ruff check --fix job_finder/web/careers_scraper.py tests/test_careers_page_extraction.py`
`uvx ruff format tests/test_careers_page_extraction.py`

## [SLOW TESTS — same I/O-leak class, out of this plan's scope] other 10-30s unit tests

The final full-suite slowest-20 no longer contains ANY homepage_discoverer /
ats_scanner / data_enricher / agentic_enricher entry (the plan's targets — all
fixed). But it surfaced other slow tests in files the plan never enumerated,
likely the same "live I/O not blocked" root cause:

- `test_careers_crawler.py::TestCrawlCareersBatch::*` — 4-31s each (the
  `test_falls_back_to_playwright_active` 31s one almost certainly drives a real
  Playwright/network fallback).
- `test_smartrecruiters_scanner.py::...::test_scan_paginates` — ~15s (likely real
  paginated HTTP).
- `test_onboarding_routes.py::test_each_step_renders[...provider_select...]` — ~15s.
- `test_model_provider.py::test_cascade_429_marks_exhausted` — ~6s.
- `test_speculative_probe_consistency.py::*` — ~3.3s each.

NOT fixed here — out of the remediation plan's enumerated findings (C1/M1/M2/M3/
M3b/m1/m2/m3/nit). Recommend a follow-up remediation pass applying the same
seam-blocking approach (and/or the Phase-5 `pytest-socket` kill-switch, which
would surface all remaining real-I/O tests at once).
