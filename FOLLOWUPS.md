# FOLLOWUPS — 2026-05-28 round 14 (AI-nav recipe work: empirical probe + one real fix; 5 distinct failure modes documented)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 14 picked up the round-13 next-session contract —
"AI-nav recipes for the 10 in-house custom ATS companies" — but on
verification reframed the work substantially:

- The 10-target list **shrinks to 9** (Citi already produces 41
  `careers_crawl`-sourced jobs via the playwright tier — confirmed by
  direct query on `jobs WHERE sources LIKE '%careers_crawl%'`. Drop it).
- "Writing recipes" isn't the work shape. The system **auto-discovers**
  recipes via the quick-tier model (Ollama qwen2.5:14b by default) when
  AI-nav is invoked. The real questions are: (a) is AI-nav being invoked
  at all for these companies, and (b) when it is, why doesn't discovery
  yield a usable recipe?
- An empirical probe answered (b): **0 of 9 targets produce a usable
  recipe today**. The 5 distinct failure modes the probe surfaced are
  documented below.

## What this session shipped

Two commits, pushed to `origin/main`:

- **`9c41d3c`** — `test: scripts/probe_ai_nav.py — AI-nav recipe
  discovery probe for 9 custom-ATS targets`. New standalone Playwright
  probe (no Flask, no DB writes). Per target reports: page reachable?
  accessibility-snapshot length + preview? raw Ollama recipe output?
  validation extraction count? Monkey-patches `_take_snapshot` and
  `call_model` to capture diagnostic state.

- **`7e783c6`** — `fix(ai-nav): align discovery validation keyword
  with replay's _derive_search_term`. One-line fix in
  `ai_career_navigator.py:484` — discovery's validation block was
  filling `{keyword}` placeholders with `target_titles[0]` (the user's
  most specific title like "Lead Product Analyst") while the replay
  path used the broad `_derive_search_term(target_titles)` ("analyst").
  Same recipe, two keyword strategies, opposite outcomes. Tests:
  22/22 in `test_ai_career_navigator.py` pass; 73/73 in the broader
  ai_career_navigator + careers_crawler suites pass.

## What the round-13 handoff got wrong, and what was actually true

Round 13 carried this prescription for the next session:

> Primary focus: AI-nav recipes for in-house custom ATS — Apple,
> Tesla, Oracle Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte,
> Genentech, Citi, Kaiser Permanente — all of which run custom
> (non-standard-ATS) careers sites and currently fall through the
> static/playwright tiers to the AI-navigator tier without a working
> recipe.

Reality at session start:

- **10 → 9 targets.** Citi already produces 41 `careers_crawl`-sourced
  jobs via the playwright tier (jobs.citi.com is well-structured for
  the playwright extractor). It does NOT need AI-nav. Verified via
  `SELECT COUNT(*) FROM jobs WHERE company_id = 1890 AND sources LIKE '%careers_crawl%'`.
- **9/10 other targets have ZERO careers_crawl-sourced jobs.** Their
  `jobs_found_total` values on the companies table (Apple 93, Tesla 22,
  Deloitte 84, Genentech 35, Kaiser 27, NVIDIA 29, Oracle 8, AMD 4,
  ByteDance 1) are **misleading** — that column is set in
  `_persistence.py:74-76` as
  `jobs_found_total = (SELECT COUNT(*) FROM jobs WHERE company_id = ?)`,
  i.e. total jobs across all sources, NOT jobs produced by careers_crawl.
  The DB-visible jobs for those 9 all came from Glassdoor / DataForSEO /
  LinkedIn / SerpAPI / Adzuna / portal_adzuna — not the careers crawler.
- **"They fall through tiers 1-3 to the AI-navigator tier" is
  unverifiable from the DB.** The orchestrator
  (`careers_crawler/__init__.py:498-513`) gates AI-nav with
  `if not jobs and ai_nav_enabled`. The `careers_crawl_tier` column
  defaults to `"static"` at line 379 and is only overwritten when a
  tier returns jobs — meaning `tier="static"` could mean (a) static
  succeeded, (b) all tiers failed including AI-nav, or (c) AI-nav
  never ran because an earlier tier returned non-empty.

## The empirical probe and what it found

`scripts/probe_ai_nav.py` runs `discover_navigation_recipe()` against
each target in isolation. Initial run (pre-fix): 0/9 produce a usable
recipe. Post-fix run: same 0/9 outcome — the bug fix is real but
addresses an orthogonal failure mode to what's blocking these 9.

**5 distinct failure modes** observed across the 9 targets:

| Failure mode | Targets | Diagnostic signal |
|---|---|---|
| Bot detection | Tesla | snapshot_len=202; content = "Access Denied / Reference #18.194e4317...". Akamai Edge blocks Playwright. |
| SPA didn't render in pre-discovery 2.5s wait | AMD | snapshot too short (< 50 chars), `discover_navigation_recipe` returns None before reaching Ollama. |
| Ollama hallucinates form selectors | Genentech, Oracle (when emitting keyword steps) | Recipe has e.g. `{"action":"type","role":"textbox","name":"Search Jobs"}` but no such accessible name exists on the destination page; `Locator.fill: Timeout 5000ms`. |
| goto-only recipes whose destination needs a search keyword | Apple → /search, ByteDance → /search, Kaiser → /clinical-careers, plausibly NVIDIA, Deloitte | Recipe is `[{"action":"goto","url":"..."}]` with no keyword step. Validation extraction returns 0 because the destination page lists jobs but none match `target_titles` exactly. |
| Validation timeout on `networkidle` | Oracle | `Page.goto: Timeout 15000ms exceeded` on the recipe's first goto. Oracle Recruiting Cloud is heavy. |

Notes:

- Ollama IS producing plausible recipes — they're just wrong in
  specific ways. The qwen2.5:14b output is not the bottleneck; the
  bottleneck is the recipe vocabulary, the validation harness, and
  per-site quirks.
- The validation-keyword bug fix (`7e783c6`) doesn't change yield for
  any of the 9 — none of them had a recipe whose `{keyword}` step
  successfully executed under the old code path. The fix matters for
  *future* companies whose recipe has a working type step.

## How to verify (this session's work)

```powershell
# 22/22 ai_career_navigator + 73/73 broader careers crawler tests pass:
.venv/Scripts/python.exe -m pytest tests/test_ai_career_navigator.py tests/test_careers_crawler.py
# Expected: 73 passed

# Confirm the fix is at the right line:
.venv/Scripts/python.exe -c "
import inspect
from job_finder.web import ai_career_navigator
src = inspect.getsource(ai_career_navigator.discover_navigation_recipe)
print([l for l in src.splitlines() if '_derive_search_term' in l])
"
# Expected: at least one line containing 'kw = _derive_search_term(target_titles)'

# Re-run the probe (slow — ~3-5 min, requires Ollama up):
.venv/Scripts/python.exe scripts/probe_ai_nav.py
# Expected: 0/9 succeed; summary table prints raw model outputs

# Only one target at a time:
$env:PROBE_ONLY="genentech"; .venv/Scripts/python.exe scripts/probe_ai_nav.py
```

## What I tried but didn't ship

- **Hand-curating recipes for any of the 9.** Considered after the
  probe revealed 0/9, but stopped to surface findings to the user
  before committing to a path that goes against the "discover once,
  replay forever" architecture. The user picked the validation-keyword
  fix first; hand-curation remains an option for next session.

- **Boot Flask and POST `/admin/jobs/careers_crawl/run-now`.** Flask
  was off; the batch endpoint iterates **all** 684 static-tier
  companies, not just the 9 targets. Too noisy for empirical probing.
  Standalone probe was the cleaner instrument.

- **Extend the recipe schema for URL-param search patterns.** This is
  the highest-leverage next step (would help Apple, Oracle, ByteDance,
  Kaiser, NVIDIA, Deloitte — possibly 6 of the 9), but it's a
  prompt + schema change, not a one-liner. Out of scope for this
  session after the validation-keyword fix took priority.

- **Run the full pytest suite (~12 min).** Ran focused subset of 73
  tests touching the fix. Risk of cross-cutting regression is low for
  a one-line keyword-resolution change.

- **Address pre-flight item #1 from round 13 (pyright Path A vs B)**
  and #2 (Commit D browser smoke + Workable scan). Neither is on the
  critical path for AI-nav recipe work. Carried forward.

## What's deferred / remaining

### CARRY FORWARD (priority order)

1. **AI-nav recipe work: continue from probe findings. ALL THREE of
   1a, 1b, 1c are required for the next session, in sequence.** 1d
   stays out — heavy investment for single-company yield.

   - **1a. Extend recipe vocabulary to include URL-param search**
     (~6 of 9 targets benefit: Apple, Oracle, ByteDance, Kaiser,
     NVIDIA, Deloitte). New step type
     `{"action":"goto_with_query","url":"...","query_param":"search","value":"{keyword}"}`
     or simpler: extend `goto` to accept `{"url":"...","query":{"search":"{keyword}"}}`.
     Then amend `_DISCOVERY_SYSTEM` prompt to suggest URL-param
     patterns when a search box is detected. Highest leverage. ~1 day.
   - **1b. Longer SPA pre-discovery wait + retry on snapshot < 50**
     (AMD). Change the `wait_until="domcontentloaded"` + 2s wait to a
     loop that polls accessibility-tree size for up to 8s. ~2 hours.
     Orthogonal to 1a — different code surface (`_take_snapshot` /
     pre-discovery wait vs. recipe vocabulary / schema). Ship in
     same session as 1a; the probe's per-target output isolates
     which lift came from which fix.
   - **1c. Hand-curate recipes for the cleanest residual targets**
     (the ones 1a doesn't lift). After 1a's probe re-run, the
     residual zero-yield set will plausibly be 2-4 of: Genentech,
     Oracle (form-selector hallucination), and any others whose
     destination pages don't match user titles even with URL-param
     search. Manually inject `careers_nav_recipe` JSON into the DB,
     force `careers_crawl_tier = 'ai_replay'` so `_try_cached_tier`
     runs the recipe first. Per-target time: 15-30 min. Bypasses
     auto-discovery for known-hard cases. **Sequence-critical:** do
     this AFTER 1a — otherwise you hand-curate recipes for cases
     1a would have auto-discovered, wasting effort.
   - **1d. Bot-detection workaround for Tesla.** Investigate
     `playwright-stealth` or similar. Heavy investment for one
     company. **Out of scope.**

   Required sequence: **1a + 1b parallel (same commit-isolated work),
   then 1c on the residual set.** Each working recipe = one atomic
   commit. Re-run `scripts/probe_ai_nav.py` after 1a+1b to identify
   the residual targets before starting 1c — don't blindly hand-
   curate all 9.

2. **Pre-flight from round 13 still outstanding:**
   - **Pyright Path A vs B decision** (advisory item). One line either
     way. See "Quirks the next session should know" below.
   - **Manual browser smoke for Commit D from round 12** (Country
     dropdown / Workplace dropdown / pill renderer in /jobs). Quick
     visual check, requires Flask up.
   - **Workable widget endpoint verification** (round-12 item #4).
     Trigger via `POST /admin/jobs/companies_scan/run-now`, then
     re-query `jobs_found_total` for ids 71, 951, 1027, 1036. If all
     return 0 after a fresh scan, switch endpoint in
     `_platforms_workable.py` to `apply.workable.com/api/v3/...`.

3. **Phase F Jobvite per-tenant fix** (Item #10 from round 11): add
   per-tenant `careers_nav_recipe` overrides for the 5 unhandled
   jobvite tenants (american-specialty-health, capcom, neogenomics,
   the-institutes, victaulic). If 1a (URL-param schema extension)
   ships, this becomes parallel work in the same vocabulary.

### Audit-track follow-ups (carried unchanged from rounds 7-13)

4. **Manual company aliases UI** (round-3 deferred).

5. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried.

6. **the-institutes slug needs manual cleanup?** Their careers_url
   302s to `?invalid=1`. Data issue.

### Open / advisory items

- **#11 (Pyright unused-args) — still advisory** (carried unchanged
  from round 13). Project `[tool.pyright]` excludes `**/tests`, so
  CLI `pyright` is silent on test files. Path A = rename test params
  to leading-underscore (~50 LOC); Path B = config tweak (1 line).
  Pick before any sweep.

- **`scripts/probe_ai_nav.py` triggers pyright IDE noise** (added by
  this session, same Path A/B pattern). Specifically: line 49 (the
  `_orig_call_model` re-assignment in a closure where pyright can't
  prove non-None). Same disposition as the test-file noise — not on
  the critical path; ignore or fix per chosen Path.

- **Lever freeform strings** — `unresolved=True` forever path. Carried
  unchanged.

- **Bare-token workplace detection in jd_full** (Q3 extension, carried
  from rounds 11-13).

- **Migration count drift on future migrations** (carried). The 3
  generic sites in `tests/test_migration.py` (lines 404, 936, 1384)
  still use exact `== NN`. Next migration will require bumping all
  three.

- **Production country distribution sanity** (carried). Top countries
  after m067: US 8987 (72.6%), IN 352, TH 74, GB 74, CA 65. IN / TH
  higher than expected — worth a one-shot spot check.

## Quirks the next session should know

Rounds 3-13 quirks still apply. Additions from round 14:

- **`companies.jobs_found_total` is misleadingly named.** Despite
  living in the careers-crawler-owned persistence module, it's
  `SELECT COUNT(*) FROM jobs WHERE company_id = ?` — total across
  every source (aggregators included), not careers_crawl-specific.
  Future you, if reasoning about "which companies has the careers
  crawler succeeded at", query
  `jobs WHERE company_id = X AND sources LIKE '%careers_crawl%'`,
  not `companies.jobs_found_total`.

- **`careers_crawl_tier='static'` ambiguity.** The orchestrator
  (`__init__.py:379`) initializes `tier_used = "static"` and only
  overwrites it when a non-static tier returns jobs. When all tiers
  fail (including AI-nav), the column stays `"static"`. So
  `tier='static'` could mean "static won", "AI-nav was attempted and
  failed", or "AI-nav was never reached because an earlier tier
  returned a non-empty list". Don't infer tier history from the column
  alone.

- **AI-nav is gated behind earlier-tier success.** Per
  `__init__.py:503` — `if not jobs and ai_nav_enabled`. If the static
  or playwright tier returns any non-empty list, AI-nav doesn't run
  that crawl cycle. The `_extract_jobs_from_soup` function already
  applies title-filtering inline (so static won't return random
  noise), but if a single JSON-LD JobPosting or one link-text match
  exists on the careers landing page, AI-nav is skipped — even if
  the discovered jobs aren't title-matched at the user's level.

- **The probe script's call_model monkey-patch tracks "last URL"**
  with a simple dict ordering trick. Single-threaded; safe for
  sequential probing. Don't use for parallel discovery.

- **Tesla is bot-blocked at the network layer** — no recipe will help
  until that's resolved. Akamai serves Access Denied to vanilla
  Playwright. Deprioritize unless someone wants to invest in stealth.

- **The line-scoped `logger.<level>` pattern in `test_log_levels.py`**
  (round 13 quirk) still applies.

## Next session's contract

**Required deliverables (all three):**

### 1a. URL-param search recipe vocabulary

Highest leverage — addresses ~6 of 9 targets.

1. Read `_DISCOVERY_SYSTEM` in `ai_career_navigator.py:285-307`. The
   prompt today only lists `goto / type / click / press / wait`
   actions. Add a step type that handles "navigate to URL with a
   query-string parameter set to the keyword."
2. Extend the schema validator (lines 374-401) to accept the new
   action shape.
3. Extend `_execute_step` (lines 197-247) to handle it.
4. Re-run `scripts/probe_ai_nav.py`. Expect Apple → /search?search=analyst,
   Oracle → /sites/jobsearch/jobs?keyword=analyst, etc., to start
   yielding non-empty validation extractions.
5. Each working recipe = one atomic commit; cache the recipe on the
   company row when the probe confirms yield > 0.

### 1b. Longer SPA pre-discovery wait

Cheap, orthogonal to 1a — different code surface. Helps AMD primarily
but may incidentally help others whose SPAs don't render in 2.5s.

1. Locate the pre-discovery wait in `_try_ai_navigation` (the
   `page.goto(careers_url, ...) + page.wait_for_timeout(2000)` call).
2. Replace with a loop that polls `_take_snapshot(page)` length until
   it exceeds the 50-char guard, capped at ~8s total.
3. Re-run the probe; AMD should now produce a non-trivial snapshot
   and either yield a recipe or fail for a different reason.

### 1c. Hand-curate recipes for the residual targets

**Sequence-critical: do this AFTER 1a+1b have shipped and the probe
has been re-run.** The residual zero-yield set will be smaller —
plausibly 2-4 targets, not all 9.

1. For each residual target, hand-write a `careers_nav_recipe` JSON
   blob: identify the actual search-URL pattern, form selector, or
   API endpoint via browser inspection.
2. Inject via direct SQL: `UPDATE companies SET careers_nav_recipe = ?, careers_crawl_tier = 'ai_replay' WHERE id = ?`.
3. Verify with `POST /admin/jobs/careers_crawl/run-now` (single-run
   doesn't exist — full batch — but stalest-first ordering should
   pick up the residual targets quickly; the recipe will execute
   via `_try_cached_tier`'s `ai_replay` shortcut).
4. Verify `jobs WHERE company_id = X AND sources LIKE '%careers_crawl%'`
   count goes from 0 to > 0 for each.

### Carried forward from round 13 (still on the docket)

- **Pyright Path A vs B decision** (advisory). One line either way.
  See "Quirks the next session should know" below. Item #2 in the
  carry-forward list.
- **Manual browser smoke for Commit D from round 12** (Country
  dropdown / Workplace dropdown / pill renderer in /jobs). Quick
  visual check, requires Flask up.
- **Workable widget endpoint verification** (round-12 item #4).
  Trigger via `POST /admin/jobs/companies_scan/run-now`, then
  re-query `jobs_found_total` for ids 71, 951, 1027, 1036. If all
  return 0 after a fresh scan, switch endpoint in
  `_platforms_workable.py` to `apply.workable.com/api/v3/...`.
- **Phase F Jobvite per-tenant fix** (Item #10 from round 11) —
  5 jobvite tenants. If 1a's URL-param vocabulary ships, this
  becomes parallel work in the same vocabulary; otherwise it
  remains a separate recipe-injection task.

Pre-flight before starting (~5 min total):

- Confirm Flask is off (port 5000 vacant) or boot it deliberately —
  the probe doesn't need it, but `POST /admin/jobs/careers_crawl/run-now`
  later will. Ollama is typically up (port 11434) from the prior
  session's scheduler auto-start.

Holding pattern (explicitly out of scope for next session):

- **1d (Tesla bot workaround)** — heavy investment for one
  company. Skip until/unless there's stranger-facing pressure.
- **Audit-track items 4-8** below (manual company aliases UI,
  m063 slug case sensitivity, salary single-value extraction,
  punctuation in company dedupe, the-institutes slug cleanup) —
  pre-existing low-priority backlog; not on the next-session
  critical path unless explicitly elevated.

**Scope reality check:** the v5.0 milestone (Phases 43-45 +
Phase 40 canary completion) is the actual release-blocker, not
AI-nav recipes. The next session's contract above is operational
quality work, not milestone work. If shipping v5.0 publicly is
the goal, Phase 43 (Update Check + Legal + Strangerify Exit
Gate) should preempt this entire AI-nav docket. The 1a/1b/1c
deliverables are valuable but orthogonal to release.

## Open questions

**RESOLVED in round 14 (this session):**

- ✅ **Validation-keyword bug in `discover_navigation_recipe`:** Fixed.
  Discovery and replay now use the same `_derive_search_term`
  strategy. Doesn't help the 9 specific targets today but eliminates
  a class of false-negative recipe discards going forward.
- ✅ **The 9 targets — does the architecture even *try* AI-nav for
  them?** Empirically yes (the probe demonstrates discovery runs).
  Recipes are produced but discarded at validation. 5 distinct
  failure modes documented above.
- ✅ **`jobs_found_total` semantics:** Confirmed it's total jobs per
  company across all sources, not careers_crawl-specific. Misleadingly
  named but not a bug — just a documentation gap.

**RE-CLASSIFIED in round 14:**

- ⚠️ **"AI-nav recipes for 10 in-house custom ATS"** (was round-13
  next-session contract): The framing's intent is right, but the work
  shape is different. Auto-discovery is the canonical path; "writing
  recipes" really means improving the discovery vocabulary, the
  validation harness, or the pre-discovery setup. 1a (URL-param
  vocabulary) is the highest-leverage next step.

**STILL OPEN (carried from prior rounds, low-priority):**

- **`uv sync` editable-rebuild conflict with running Flask** (round-9
  carry). No new deps this round.
- **Manual users of `parse_locations` outside upsert_job** (round-12
  carry). No known existing callers.
- **Lever freeform strings — keep `unresolved=True` forever?**
  (round-12 carry). De-prioritized.
