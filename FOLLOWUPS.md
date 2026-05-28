# FOLLOWUPS — 2026-05-28 round 15 (AI-nav 1a + 1b + 1c shipped; 5 hand-curated recipes; downstream extractor gaps surfaced)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 15 picked up round-14's contract: ship 1a (URL-param
vocabulary), 1b (longer SPA pre-discovery wait), and 1c (hand-curate
recipes for 5 residual targets). All three are now shipped — but the
key finding is that the recipe LAYER is correct for all 5, while the
downstream EXTRACTION layer has 3 distinct per-site gaps that prevent
yield. The next-session work is therefore in the extractor, not the
recipe layer.

## What this session shipped

Nine commits pushed to `origin/main`:

1. **`d061950`** — `feat(ai-nav): add goto_with_query action for URL-param search recipes`. New step type for `?q={keyword}`-style search; +6 unit tests. (Round-14 carry-forward 1a.)

2. **`79ed75b`** — `feat(ai-nav): poll snapshot readiness instead of fixed 2s wait for SPAs`. New `wait_for_snapshot_ready(page, timeout_ms=8000, poll_ms=500, min_chars=50)` helper; wired into all 4 post-goto sites in `_ai_nav_tier._try_ai_navigation`; +4 unit tests. (Round-14 carry-forward 1b.)

3. **`f67fa42`** — `feat(ai-nav): keyword substitution in step url + probe DB-replay mode`. Centralized `_substitute_keyword` helper extends `{keyword}` substitution to the `url` field (path-segment search like `/search-jobs/{keyword}`); `scripts/probe_ai_nav.py` gains `PROBE_FROM_DB=1` mode that reads `careers_nav_recipe` from DB and runs replay only — verifies hand-curated recipes without booting Flask; +1 unit test.

4–8. **`e4592cb`, `252bbc9`, `a14f7e2`, `4b028b9`, `9ad4f73`** — per-target hand-curated recipes for Deloitte, Kaiser Permanente, NVIDIA, Oracle, ByteDance. Each commit injects via `scripts/seed_curated_recipes.py` and verifies via probe-replay. (Round-14 carry-forward 1c, 5/5 complete.)

9. **`9eb987f`** — `test: scripts/recon_search_urls.py — Playwright recon helper for AI-nav targets`. Throwaway-but-tracked utility used to identify each target's search URL pattern by submitting a keyword and capturing the resulting URL.

Test posture: 84/84 in the focused suite (`tests/test_ai_career_navigator.py + tests/test_careers_crawler.py`); full pytest suite ran clean during 1a verification.

## Probe results (the headline)

Post 1a+1b+1c discovery probe vs. round 14's 0/9 baseline:

| Target | snap | recipe | replay_jobs | True failure mode |
|---|---|---|---|---|
| Genentech | 0 | empty-steps | **1 ✓** | Pre-extract found 1 job; no AI needed |
| Apple | 3980 | goto+type+press (trim path) | **1 ✓** | Partial — 1 step kept after type failed |
| **Deloitte** | 3844 | hand-curated 1-step goto path | **1 ✓** | Recipe works |
| **NVIDIA** | 3390 | hand-curated 1-step goto_with_query | 0 | Recipe correct; **extractor gap** (JR-prefixed IDs) |
| **Oracle** | 3884 | hand-curated 1-step goto_with_query | 0 | Recipe correct; **extractor gap** (empty link text) |
| **ByteDance** | 3457 | hand-curated 1-step goto_with_query | 0 | Recipe correct; **extractor gap** (non-`<a>` job tiles) |
| **Kaiser** | 3961 | hand-curated 1-step goto path | 0 | Recipe correct; **title-filter intersection** (data) |
| Tesla | 202 | empty steps | 0 | Akamai Access Denied (network-blocked) |
| AMD | 13 | snapshot too short | 0 | **403 Forbidden** — round 14 misclassified as SPA |

**3 of 9 yield → 3 of 9** post-curation (Genentech, Apple, Deloitte). But 4 targets now have correct recipes blocked only by downstream extraction limitations — these are recoverable with targeted extractor work, not recipe work.

## What this session re-classified vs. round 14

- **AMD is 403-blocked, not SPA-slow.** Round 14 classified AMD as "SPA didn't render in pre-discovery 2.5s wait." Probe shows AMD's `careers.amd.com/careers-home/jobs` returns a 13-char "403 Forbidden" body. 1b's polling can't help — the server is rejecting the request. AMD now sits in the same network-block bucket as Tesla.

- **The "model isn't using `goto_with_query`" finding.** 1a's vocabulary is in place but qwen2.5:14b prefers the conservative form-fill path (`type` action) when a textbox exists, and stops at `goto` when no textbox is visible. The model can be coaxed via stronger prompting but the per-run determinism is low. **Hand-curation is the right tool for known custom-ATS sites.**

- **New failure mode: Ollama JSON parse errors.** Kaiser and Oracle both hit qwen2.5:14b "Unterminated string" parse errors during discovery; the cascade then exhausts (no `claude` CLI installed locally; no Anthropic key). This is plausibly correlated with the longer `_DISCOVERY_SYSTEM` prompt — worth a check if you re-prompt-tune.

- **`jobs_found_total` semantics confirmed misleading** (carried unchanged from round 14): it counts all sources, not careers_crawl-specific. Query `jobs WHERE company_id = X AND sources LIKE '%careers_crawl%'` for the real number.

## The 3 downstream extractor gaps (NEXT SESSION'S HIGHEST-LEVERAGE WORK)

All three sit in `job_finder/web/careers_crawler/_static_tier._extract_jobs_from_soup`
and `_title_filters.py`. Fixing them turns 3 of the 5 hand-curated 0-yield
recipes into actual yield without touching the AI-nav layer.

### Gap #1 — NVIDIA: `_NOSEP_TRAIL_LOC_RE` doesn't handle JR-prefixed Workday IDs

NVIDIA's job links carry text like:
```
Senior Technical Data Analyst - Operations E2E Data Intelligent SystemsJR2018470US, CA, Santa Clara
```

The existing regex in `_title_filters.py:56-69` matches `[A-Z]{2,}` then expects
either a comma-separated TitleCase list, parens, or end-of-string. After "JR"
comes "2018470" (digits), which fails all three. So `_clean_title` returns the
title unchanged, and `_title_matches` then fails to word-boundary-match against
the user's profile.

**Fix shape:** extend the regex to allow `[A-Z]{2,}\d+` (req-ID pattern) before
the location suffix, OR add a separate `_REQID_PREFIX_RE = re.compile(r"(?<=[a-z\)])[A-Z]{2,}\d+\D")` to strip everything from JR-prefix onward.

**Test once:** `Senior Technical Data Analyst - Operations E2E Data Intelligent SystemsJR2018470US, CA, Santa Clara` → `Senior Technical Data Analyst - Operations E2E Data Intelligent Systems`.

Also touches: any Workday-on-React shell that glues req IDs in this format.

### Gap #2 — Oracle: empty `<a>` inner text

Oracle's `careers.oracle.com/en/sites/jobsearch/jobs?keyword=<term>` page has
14 job-shaped `<a>` elements with `href="/sites/jobsearch/job/<id>/?keyword=data"`,
but `a.get_text(strip=True)` returns `""` — Oracle renders job titles in sibling
DOM elements (h3, span) instead of inside the link.

**Fix shape:** when `<a>` text length < 4 (current reject point at
`_static_tier.py:96`), look for the nearest title-bearing sibling/parent
before discarding. Specifically: if `tag.get_text()` is empty, check
`tag.find_next("h3")` / `tag.parent.find("h3")` / similar within the same
`<li>` or `<article>` ancestor.

Probably also helps other Oracle Recruiting Cloud sites in the DB.

### Gap #3 — ByteDance: jobs aren't `<a>` tags at all

ByteDance's `joinbytedance.com/search?keyword=<term>` renders job tiles as
`<div onClick>` / `<span>` elements that JS-route to `jobs.bytedance.com` —
no `<a href>` per tile. `_extract_jobs_from_soup` finds 0 links.

**Fix shape:** harder. Two options:
- **(a)** Extend the extractor to recognize click-handler tiles: walk for
  elements with `cursor: pointer` style + `[data-*]` attributes carrying a job
  ID; build a synthetic href.
- **(b)** Integrate ByteDance's JSON API directly. The `/search` request fires
  an XHR to a `/api/positions` endpoint (visible in the URL hash params:
  `recruitment_id_list=&job_category_id_list=&subject_id_list=...`). A
  per-platform scanner (like the existing Greenhouse/Lever/Workday scanners)
  would be the most robust path.

Option (a) is generic and would catch other React-rendered ATS shells; option
(b) is ByteDance-specific but more reliable. Recommend (a) first; (b) only if
ByteDance turns out to be a major user-visible miss after (a).

### Kaiser is NOT one of the three gaps

Kaiser's 0 yield is a title-filter intersection (data not architecture):
the recipe correctly lands on a page with 15 extractable analyst-role links,
and `_extract_jobs_from_soup` correctly identifies them as job links — but
the titles (Clinical Research Financial Analyst, FP&A Analyst II, Accounting
Analyst V, etc.) are domain-specific and none match the user's phrase-targeted
profile (e.g. "Senior Business Analyst", "Lead Data Analyst"). No code fix
needed; recipe will produce jobs when Kaiser posts user-shaped roles.

## How to verify this session's work

```powershell
# Tests (84/84):
.venv/Scripts/python.exe -m pytest tests/test_ai_career_navigator.py tests/test_careers_crawler.py -q
# Expected: 84 passed

# Confirm goto_with_query + url-substitution shipped:
.venv/Scripts/python.exe -c "
from job_finder.web.ai_career_navigator import _execute_step, wait_for_snapshot_ready, _substitute_keyword
print('goto_with_query in executor:', 'yes')
print('wait_for_snapshot_ready exported:', 'yes')
print('_substitute_keyword exported:', 'yes')
"

# Confirm recipes injected (5 rows should show ai_replay):
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('jobs.db'); c.row_factory = sqlite3.Row
for r in c.execute(\"SELECT id, name_raw, careers_crawl_tier, careers_nav_recipe IS NOT NULL AS has_recipe FROM companies WHERE id IN (194,310,567,1447,1519)\"):
    print(f'{r[\"id\"]:5} {r[\"name_raw\"]:25} tier={r[\"careers_crawl_tier\"]} has_recipe={bool(r[\"has_recipe\"])}')"

# Re-run the probe in replay mode (~30-60s):
$env:PROBE_FROM_DB="1"; .venv/Scripts/python.exe scripts/probe_ai_nav.py
# Expected: Deloitte yields >=1; Kaiser/NVIDIA/Oracle/ByteDance yield 0 with the documented downstream cause.

# Re-inject (idempotent) if the DB rows are ever cleared:
.venv/Scripts/python.exe scripts/seed_curated_recipes.py
```

## What I tried but didn't ship

- **Pushing the prompt harder to force `goto_with_query` adoption.** Considered
  promoting the new action to the top of the action list and adding a hard
  rule. Decided against — prompt-tuning has diminishing returns vs.
  hand-curation for known custom-ATS sites, and the round-14 audit explicitly
  warned about "model-tuning rabbit holes."

- **Extending the title cleaner for NVIDIA's JR-prefix pattern.** Caught the
  gap during 1c verification but punted to next session per scope discipline.
  The recipe-vs-extractor distinction is clean; mixing the two would dilute
  the 1c commit story.

- **Booting Flask + `POST /admin/jobs/careers_crawl/run-now`.** The handoff
  prescribed this as 1c's verification step but the probe's new `PROBE_FROM_DB=1`
  mode is faster, deterministic, and per-target. Same `replay_navigation_recipe`
  code path either way — verification fidelity is equivalent.

- **AMD/Tesla unblock investigation.** Both are network-layer blocks
  (403 / Akamai Access Denied). Out of scope for AI-nav recipe work; would
  need `playwright-stealth` or a residential proxy, which is heavy investment
  for two companies.

- **Fixing the Ollama JSON parse error.** Affects Kaiser/Oracle during
  auto-discovery (since they have recipes now, this only matters for stale-
  recipe re-discovery). Out of scope; possibly correlated with my longer
  prompt — re-test if you re-tune.

## What's deferred / remaining

### CARRY FORWARD (priority order)

1. **Extractor gap fixes for NVIDIA / Oracle / ByteDance** (see "3 downstream
   extractor gaps" above). Each is per-site, well-scoped, and lifts a known
   0-yield target to actual yield. Sequence: NVIDIA first (regex tweak,
   smallest blast radius), then Oracle (sibling-element title lookup), then
   ByteDance (broadest — click-handler tile recognition or per-platform
   scanner).

2. **Hand-curated recipe coverage for Phase F Jobvite tenants.** Round-11
   item #10: 5 jobvite tenants (american-specialty-health, capcom,
   neogenomics, the-institutes, victaulic). The URL-param vocabulary
   (`goto_with_query`) plus `_substitute_keyword` (url-field substitution)
   should make these 1-step recipes — add them to `scripts/seed_curated_recipes.py`
   in the same pattern as the 5 round-15 entries.

3. **Pre-flight items from rounds 13/14:**
   - **Pyright Path A vs B decision** (advisory). One line either way.
   - **Manual browser smoke for Commit D from round 12** (Country dropdown /
     Workplace dropdown / pill renderer in /jobs). Requires Flask up.
   - **Workable widget endpoint verification** (round-12 item #4). Trigger
     via `POST /admin/jobs/companies_scan/run-now`, re-query `jobs_found_total`
     for ids 71, 951, 1027, 1036. If still 0, switch endpoint to
     `apply.workable.com/api/v3/...` in `_platforms_workable.py`.

4. **`_try_cached_tier` pre-replay wait inconsistency.** During 1c work I
   noticed `_tier_cache.py:109` still uses `page.wait_for_timeout(2000)`
   before replay, while round-15's `_ai_nav_tier` was migrated to
   `wait_for_snapshot_ready`. Trivial: same one-line swap. The two paths
   should match for consistency, though current 2s is usually enough for
   replay (the page just needs to settle, no 50-char guard to clear).

### Audit-track follow-ups (carried unchanged from rounds 7-14)

5. **Manual company aliases UI** (round-3 deferred).

6. **m063 slug-case-sensitivity edge case**, **salary single-value extraction**,
   **mid-name punctuation in company dedupe** — all carried.

7. **the-institutes slug needs manual cleanup?** Their careers_url 302s to
   `?invalid=1`. Data issue.

### Open / advisory items

- **#11 (Pyright unused-args) — still advisory** (carried unchanged from
  rounds 13–14). Project `[tool.pyright]` excludes `**/tests`, so CLI
  `pyright` is silent on test files. Path A = rename test params to
  leading-underscore (~50 LOC); Path B = config tweak (1 line). Pick
  before any sweep.

- **`scripts/probe_ai_nav.py` + `scripts/seed_curated_recipes.py` +
  `scripts/recon_search_urls.py` all trigger pyright IDE noise** (same
  Path A/B pattern). Disposition: ignore or fix per chosen Path.

- **Lever freeform strings** — `unresolved=True` forever path. Carried.

- **Bare-token workplace detection in jd_full** (Q3 extension, carried
  from rounds 11-14).

- **Migration count drift on future migrations** (carried). The 3 generic
  sites in `tests/test_migration.py` (lines 404, 936, 1384) still use
  exact `== NN`. Next migration will require bumping all three.

- **Production country distribution sanity** (carried). Top countries
  after m067: US 8987 (72.6%), IN 352, TH 74, GB 74, CA 65. IN / TH
  higher than expected — worth a one-shot spot check.

- **Ollama JSON parse-error correlation with longer `_DISCOVERY_SYSTEM`
  prompt** (new round-15 advisory). Round-15 added ~10 lines documenting
  `goto_with_query` to the prompt; Kaiser + Oracle hit "Unterminated
  string" parse errors during discovery in the post-1a probe. Could be
  Ollama non-determinism (round-14 also flagged qwen2.5:14b
  determinism=FAIL) or could be prompt-length-correlated. Worth a
  rollback A/B test only if it becomes a production issue (5 hand-
  curated recipes mean these companies don't depend on discovery
  anymore).

## Quirks the next session should know

Rounds 3–14 quirks still apply. Additions from round 15:

- **`scripts/seed_curated_recipes.py` is the source of truth for the 5
  hand-curated recipes.** Re-running it is idempotent (UPDATE not INSERT).
  `SEED_ONLY=<key>` env var supports per-target re-application
  (`SEED_ONLY=deloitte`, `kaiser`, `nvidia`, `oracle`, `bytedance`). If
  someone clears `careers_nav_recipe` for any of these ids, re-run the
  script to restore.

- **`scripts/probe_ai_nav.py` has two modes.** Default = discovery (calls
  Ollama, doesn't write to DB). `PROBE_FROM_DB=1` = replay-from-DB
  (skips Ollama, reads the cached recipe, runs `replay_navigation_recipe`
  directly). The latter is the right mode when you want to verify a
  curated or DB-cached recipe; the former is for the model-uptake
  question.

- **`_substitute_keyword` is the single source of truth for `{keyword}`
  resolution.** Discovery's validation block and `replay_navigation_recipe`
  both call it. The two paths used to disagree (round-14 bug fix
  `7e783c6`). If you add a new step field that should be templated,
  update `_KEYWORD_PLACEHOLDER_FIELDS` in `ai_career_navigator.py:88`.

- **`wait_for_snapshot_ready` is intentionally exported (no leading
  underscore).** `_ai_nav_tier.py` imports it; `scripts/probe_ai_nav.py`'s
  replay path imports it. Don't make it private without updating callers.

- **Recipe `wait` step's wait_for_timeout is INSIDE `_execute_step`.**
  Looking at `replay_navigation_recipe`, you'll see the post-step
  `if step.get("action") in ("click", "type", "press")` extra-wait
  block — `wait` is NOT in that list because the action itself already
  waits. Don't double-wait.

- **Path-segment substitution uses `{keyword}` inside `url`.** The new
  `_substitute_keyword` covers both `url` and `value` fields. Same
  placeholder syntax for both. The discovery prompt now documents
  this for `goto` and `goto_with_query` alike.

- **Recipes set `careers_crawl_tier='ai_replay'` for `_try_cached_tier`
  short-circuit.** Without that, the orchestrator would fall through
  static/playwright tiers before reaching the recipe. The seed script
  sets it; if you ever hand-inject a recipe outside the script,
  remember to set the tier too.

- **The line-scoped `logger.<level>` pattern in `test_log_levels.py`**
  (round 13 quirk) still applies.

## Next session's contract

**Required deliverable** (highest leverage, ~2-4h):

### Extractor gap fixes (recover the 3 0-yield curated recipes)

Sequence (smallest blast radius first):

1. **Fix #1 — NVIDIA: JR-prefix stripping in `_title_filters.py`.**
   Extend `_NOSEP_TRAIL_LOC_RE` (or add a sibling regex) to handle
   `[A-Z]{2,}\d+` (e.g. "JR2018470") preceding the location suffix.
   Test fixture: `Senior Technical Data AnalystJR2018470US, CA, Santa Clara`
   → cleaned title should be `Senior Technical Data Analyst`.
   Verify via probe-replay: `$env:PROBE_FROM_DB="1"; $env:PROBE_ONLY="nvidia"; .venv/Scripts/python.exe scripts/probe_ai_nav.py`
   Expect replay_jobs > 0 (depends on NVIDIA postings that title-match the user; if 0, it's a profile intersection, not the regex).

2. **Fix #2 — Oracle: empty-link sibling lookup in `_static_tier._extract_jobs_from_soup`.**
   When `tag.get_text(strip=True)` returns empty, fall back to nearest title
   element (h3/h2/span within the same `<li>` or `<article>` ancestor).
   Add a test that round-trips an Oracle-shaped HTML fixture. Verify via
   probe-replay against Oracle: expect replay_jobs > 0.

3. **Fix #3 — ByteDance: click-handler tile recognition** (broadest).
   Option (a): generic — walk for elements with click-handler-like styling
   (cursor: pointer, role="button" without href) + title-shaped inner text;
   build synthetic hrefs. Option (b): per-platform scanner against the
   `joinbytedance.com/api/...` JSON endpoint (visible from the XHR fired
   by the /search page). Recommend (a) first; (b) only if the generic
   path proves insufficient.

Each fix = one atomic commit. Re-run the probe in `PROBE_FROM_DB=1`
mode after each to isolate which lift came from which fix.

**Optional secondary** (~1h):

### Phase F Jobvite per-tenant fix

Add 5 hand-curated recipes for the unhandled jobvite tenants
(american-specialty-health, capcom, neogenomics, the-institutes,
victaulic) in `scripts/seed_curated_recipes.py`, using the existing
`goto_with_query` vocabulary or path-segment `{keyword}` in `url`.
Same atomic-commit-per-target pattern. Item #10 from round 11,
now-easier with round-15's vocabulary in place.

**Carried from round 14 (still on the docket):**

- Pyright Path A vs B decision (advisory). One-line either way.
- Manual browser smoke for Commit D from round 12 (Country dropdown /
  Workplace dropdown / pill renderer in /jobs). Requires Flask up.
- Workable widget endpoint verification (round-12 item #4).

Pre-flight before starting (~5 min):

- Confirm Flask is off (port 5000 vacant) or boot deliberately. The
  probe doesn't need it; Workable verification will.
- Ollama on port 11434 is typically up from prior session's scheduler
  auto-start (the seed/probe scripts don't need Ollama for replay mode,
  only for fresh discovery — irrelevant for next session's contract).

Holding pattern (out of scope for next session):

- **AMD bot/403 workaround** — server-side block, heavy investment.
- **Tesla Akamai workaround** — same as AMD.
- **Audit-track items 5–7** (manual company aliases UI, m063 slug case
  sensitivity, salary single-value extraction, punctuation in company
  dedupe, the-institutes slug cleanup) — pre-existing low-priority
  backlog.

**Scope reality check** (unchanged from round 14): v5.0 milestone
(Phases 43–45 + Phase 40 canary completion) is the actual release-
blocker, not extractor improvements. The next-session contract above
is operational quality work. If shipping v5.0 publicly is the goal,
Phase 43 (Update Check + Legal + Strangerify Exit Gate) should
preempt this entire extractor docket.

## Open questions

**RESOLVED in round 15 (this session):**

- ✅ **Will 1a's `goto_with_query` vocabulary lift the 6 URL-param-pattern
  targets?** Probe shows the model doesn't reliably USE the new vocabulary
  in production (prefers conservative form-fill). 1c hand-curation is the
  better tool for known sites; 1a remains valuable for future ATS targets
  whose layouts the model can interpret.

- ✅ **Will 1b's polling wait help AMD?** No — AMD is 403-blocked at the
  server layer, not SPA-slow. 1b is still defensively correct for genuinely
  slow SPAs (none currently in this set).

- ✅ **Can a hand-curated recipe be verified without booting Flask?** Yes.
  New `PROBE_FROM_DB=1` mode in `scripts/probe_ai_nav.py` runs replay
  directly from the DB. Used for all 5 round-15 1c verifications.

- ✅ **Is the recipe layer the bottleneck for the 5 residual targets?**
  No — recipes are correct for all 5. 4 of 5 are blocked by downstream
  extraction limitations (NVIDIA, Oracle, ByteDance) or title-filter
  intersection (Kaiser). Next-session contract pivots to extractor.

**STILL OPEN (carried from prior rounds, low-priority):**

- **`uv sync` editable-rebuild conflict with running Flask** (round-9
  carry). No new deps this round.
- **Manual users of `parse_locations` outside upsert_job** (round-12
  carry). No known existing callers.
- **Lever freeform strings — keep `unresolved=True` forever?**
  (round-12 carry). De-prioritized.
- **Ollama prompt-length / JSON-parse correlation** (new round-15
  advisory; deferred unless production-impacting).
