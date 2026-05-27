# FOLLOWUPS — 2026-05-27 deferred-items + user-bug-list (round 2)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job search
(see CLAUDE.md). The previous session closed the 2026-05-27 triage list and
left two distinct backlogs in FOLLOWUPS.md: (a) three "deferred items" the
prior session shipped partially, and (b) a four-item "User Bug List" the
user appended after the handoff was written.

User direction at the start of this session: **both lists are equal in
priority; address all seven items.** Done.

## What this session shipped

Commits, in order (newest first):

1. `feat(companies): make Scan ATS async with HTMX polling` — 9a7083f.
   Mirrors the batch_scoring pattern. New routes
   `POST /companies/scan` (session row + bg thread + progress fragment)
   and `GET /companies/scan/status/<id>` (polling endpoint via the shared
   `render_polling_status` helper). Bg thread serializes the summary into
   `batch_score_sessions.error_msg` on success (column reused; status is
   the discriminator). Two new templates `_scan_ats_progress.html` /
   `_scan_ats_done.html`. The page is no longer blocked during scans; the
   user can navigate away. Emits `dashboard-refresh + jobs-updated`
   HX-Trigger so the dashboard refreshes when the scan finishes.

2. `feat(enrichment): deterministic salary regex + m062 backfill` — 94218ac.
   New `job_finder/web/salary_extractor.py` with `extract_salary_from_text`
   covering `$120K-$150K`, `USD 120,000-150,000`, `salary range: 140K-180K`,
   etc. Plausibility filter `[$30K, $5M]` rejects hourly rates, funding
   numbers, version strings. Hooked into
   `data_enricher._apply_post_fetch_extraction` as a fast-path BEFORE the
   LLM tier — saves API spend on common formats. **m062** backfills salary
   from existing `jd_full` rows where salary is NULL (never overwrites
   source-API values).

3. `fix(companies): m061 reconciles semantic company-name duplicates` —
   5895acc. Auto-merges paren-abbrev pairs (`X (Y)` vs `X`) and corporate-
   suffix variants (`Albertsons` vs `Albertsons Companies`). Out of scope:
   subsidiary / branding variants (Amazon vs AWS) — those have different
   base names and represent distinct hiring entities.

4. `fix(locations): normalize at ingestion + read, heal old rows (m060)` —
   e8c3453. Two-fold fix: new `location_normalizer.py` module with
   `normalize_location` (trim, drop placeholders like `Unknown`/`TBD`/`N/A`)
   and `split_multi_locations` (split on `|` / `;` / ` / ` / ` & ` / ` or `
   but NOT plain commas — those mangle City/State pairs). Wired into
   `upsert_job` at INSERT and UPDATE branches with case-insensitive dedup.
   `get_distinct_locations` now sources from per-entry `locations_raw`
   (not the merged `location` column), so multi-location *combinations*
   no longer bloat the dropdown. **m060** backfills existing rows.

5. `fix(migrations): m059 heals existing careers_crawl title-bleed rows` —
   d8317d0. Reuses `_is_metadata_blob` predicate from
   `careers_crawler._title_filters`. Conservative scope: only
   `sources == ["careers_crawl"]` rows with `pipeline_status='discovered'`
   are deleted — multi-source and user-touched rows are preserved.

6. `fix(dashboard): wire dashboard-refresh auto-refresh as originally
   intended` — d122ab7. Audit revealed: `_stats_cards.html` and
   `_quick_actions.html` partials carried comments claiming auto-refresh,
   but `dashboard/index.html` inlined all the markup directly and no
   element subscribed to `dashboard-refresh from:body`. Restored the
   wiring: index.html now wraps the stat cards + quick-action widgets in
   listening containers (`#dashboard-stats`, `#dashboard-quick-actions`,
   5s delay on the latter to let backend session state settle) that
   `{% include %}` the partials. After a sync or batch-score run, counts
   and the budget banner refresh without a full page reload.
   **Note**: I initially chose the "scrub the dead code" path and deleted
   the partials. The user redirected: *"always err on the side of the
   original intention"*. Restoring the wiring was the right call. Memory
   saved at `feedback_restore_original_intent`.

7. `fix(jobs): trigger HTMX fetch on a restored input, not the form, when
   reloading filter state` — 8a8d90b. Root cause: `restoreFilters()` JS
   set the dropdown values from localStorage and called
   `htmx.trigger(form, 'change')`, but the form's hx-trigger is
   `change from:select, change from:input, ...` — the `from:` qualifier
   requires the event source to be a descendant select/input, NOT the
   form itself. So restored values displayed in the UI but no HTMX fetch
   fired, leaving the table on the unfiltered initial render. Fix:
   dispatch the change event on the first restored element instead. New
   e2e regression `test_posted_within_restore_actually_refreshes_table`
   compares post-reload table HTML to locally-filtered HTML (rather than
   asserting row counts) so the test stays meaningful when fixture data
   yields 0 'today' rows.

## How to verify the work

```powershell
# All directly-affected tests (~210 across the new + updated files; takes
# ~3 min on this machine). The 3 TestBatchScoreStart failures listed
# under 'Known issues' below are pre-existing, NOT caused by this session.
uv run --active pytest `
  tests/test_location_normalizer.py `
  tests/test_get_distinct_locations.py `
  tests/test_salary_extractor.py `
  tests/test_migration_059_heal_careers_crawl_title_bleed.py `
  tests/test_migration_060_normalize_locations.py `
  tests/test_migration_061_reconcile_semantic_company_dupes.py `
  tests/test_migration_062_backfill_salary_from_jd.py `
  tests/test_migration.py `
  tests/test_views.py::TestAsyncScanFlow `
  tests/test_views.py::TestDashboardRefreshFragments `
  tests/test_ats_scanner.py::TestScanRouteProbeBeforeScan `
  tests/test_dedup_normalizer.py

# Browser-side checks (require the dev server on :5000):
# - Dashboard: stat cards live inside a #dashboard-stats wrapper with
#   hx-trigger="dashboard-refresh from:body". Trigger a batch scoring
#   run; when it finishes, the cards should re-fetch without a full
#   page reload.
# - Job Board: change the posted_within dropdown to 'Today', reload
#   the page. Dropdown stays on 'Today' AND the listing should match
#   the today-filtered set (not 'all jobs').
# - Companies: click 'Scan ATS'. The page should become responsive
#   immediately, a progress card appears, and a result card lands
#   when the scan finishes (no full-page block during the wait).
# - Companies filter dropdowns / table: should now show normalized
#   location values without case-variant duplicates after m060 runs.
```

## What I tried that didn't work, and why

- **"Scrub the dead dashboard-refresh code" path.** When the audit
  revealed the partials and fragment routes were referenced nowhere,
  my first instinct was to delete the partials + fragment routes +
  the `dashboard-refresh` key in `_BATCH_HX_TRIGGER`. Cleanup-faster,
  honest-about-reality. The user redirected before I committed:
  *"always err on the side of the original intention"*. Memory saved:
  `feedback_restore_original_intent.md`. If you find similarly
  unwired-but-documented features in the future, restore rather than
  scrub unless the user says otherwise.

- **Aggressive title-casing in `normalize_location`.** First draft
  did `.title()` on monocase input. Caught immediately that "san
  francisco, CA" would become "San Francisco, Ca" (state code
  mangled). Dropped the case normalization — the cost was higher
  than the dropdown-cleanup benefit. The lower-case-key dedupe in
  `get_distinct_locations` still collapses case variants for display.

- **Splitting locations on plain commas.** Tempting because
  "Remote, NYC, SF" looks like three locations. But it would mangle
  "San Francisco, CA" into ["San Francisco", "CA"] — and city/state
  pairs are far more common in real data than comma-separated lists.
  `split_multi_locations` only splits on unambiguous separators
  (`|`, `;`, ` / `, ` & `, ` or `). A small fraction of comma-listed
  multi-location strings still appear as single entries — acceptable
  trade-off.

- **Adding "companies" plural to the shared `_COMPANY_SUFFIXES` regex
  for m061.** Shared regex is used by `normalized_dedup_key` across
  the codebase; changing it would affect dedup_key generation for
  every new job. Risk too broad. Kept the supplemental suffix pattern
  local to m061's `_canonical_key` instead.

- **Splitting `run_ats_scan` for per-company progress.** The function
  iterates companies internally and returns a single summary dict.
  Adding a progress-callback parameter would let the bg thread tick
  the `scored` column after each company — better UX. Skipped for
  this session because it'd touch `run_ats_scan` + every caller, and
  the user's main pain (synchronous block on the request) is already
  resolved. Listed under "Open questions" below.

## Known issues (pre-existing, not introduced this session)

- **`TestBatchScoreStart` (3 tests fail) in `tests/test_views.py`**:
  `test_batch_score_start_returns_progress_fragment_when_unscored_exist`,
  `test_batch_score_start_progress_shows_scoring_label`,
  `test_batch_score_start_creates_session_in_db`. Root cause: the
  `app_with_unscored_jobs` fixture inserts rows WITHOUT `jd_full`, but
  the prior session's "stop counting skipped envelopes as scored" fix
  (commit 8731796) made `count_scorable` require `jd_full`. The fixture
  needs `jd_full` seeded for the rows to be considered scorable. The
  prior FOLLOWUPS explicitly flagged this pattern ("test_batch_scoring.py
  helper updated; other test fixtures should follow"). Trivial fix:
  add a non-empty `jd_full` value to the executemany insert at
  `tests/test_views.py:742-790`. Verified pre-existing via `git stash`
  bisect.

## What's deferred / remaining

### From the original FOLLOWUPS (still applicable)

- **Pyright union-narrowing warnings in `tests/test_polling_status.py`** —
  pre-existing false positives; runtime is correct.
- **`make_response` lazy import in `db_helpers._attach_hx_trigger`** —
  intentional; informational only.

### New items surfaced this session

- **Per-company progress for Scan ATS.** The async flow ships, but
  the progress fragment shows a static "Scanning N companies..."
  instead of "Scanned X of N". To wire incremental progress:
    1. Add `progress_callback=None` parameter to `run_ats_scan` in
       `job_finder/web/ats_scanner/_run.py`.
    2. Inside the company-iteration loop, call the callback after
       each company with `(scanned_so_far, total)`.
    3. In `_run_ats_scan_bg` (companies blueprint), pass a callback
       that updates `batch_score_sessions.scored` for the session.
    4. The progress template already reads `session["scored"]`, so
       displaying `scored/total` is just template tweak.
  Estimated 30-45 minutes. Don't forget the scheduler caller of
  `run_ats_scan` in `_runners.py` — pass `progress_callback=None`
  (default) to keep it a no-op there.

- **Heal `TestBatchScoreStart` fixture** (see Known issues). 5 minute
  fix; would un-break 3 tests that the next session would otherwise
  see fail. Worth doing as a tiny preamble.

- **m061 doesn't merge `Amazon` vs `Amazon Web Services` etc.** That
  was a deliberate non-handling — different base names, different
  hiring entities. If the user wants those merged anyway, the right
  path is a manual aliases UI (a `company_aliases` table + a
  "merge these two companies" admin action), NOT extending m061's
  fuzzy matching (too many false-positive risks like "Apple" vs
  "Apple Records"). Surface this when the user asks again.

- **Salary single-value extraction.** `extract_salary_from_text`
  deliberately ignores `$120K base` / `Up to $150K` — single-value
  attribution is ambiguous. If the next salary-coverage audit shows
  significant gaps, the right fix is directional hint matching
  ("starting at" → min, "up to" → max). Decide based on data.

- **Mid-name punctuation in company dedupe.** "Goldman Sachs & Co"
  vs "Goldman Sachs" doesn't merge because `_COMPANY_SUFFIXES`
  expects `[,\s]+` before the suffix and `&` isn't in that set.
  Adding `&` support risks false positives on real names like
  "Penn & Teller". Lower priority than the alias-UI path above.

- **Old `_scan_result.html` template** is no longer referenced by
  any live route after the async ATS refactor, but it's left in the
  repo in case any external doc/branch needs it. Safe to delete
  next session if no concerns surface.

## Quirks the next session should know

- **`error_msg` column is dual-purpose for ats_scan sessions.**
  `_run_ats_scan_bg` writes the full `run_ats_scan` summary as a
  JSON blob into `batch_score_sessions.error_msg` when status='done'.
  The `_scan_done_ctx` callable reads it directly off the session
  row (NOT via the helper's `error_msg` parameter, which the
  `render_polling_status` helper only surfaces when status='error').
  If you add other terminal payload data for ats_scan sessions,
  follow the same pattern OR add a dedicated `summary_json` column
  via a migration.

- **Migration `_canonical_key` in m061 ≠ `normalize_company`.**
  m061 uses a comparison-only key that adds trailing-paren stripping
  AND a supplemental suffix pass for "companies"/"enterprises". DO
  NOT confuse it with `normalize_company` (shared module, used by
  `normalized_dedup_key`, must stay stable across releases).

- **Location-normalizer plausibility list does NOT include
  "Anywhere"/"Worldwide"/"Global"/"US"/"USA".** Those ARE meaningful
  filter values (especially for fully-remote roles). The placeholder
  list is restricted to unambiguous junk: `n/a`, `tbd`, `tba`,
  `unknown`, `various`, `varies`, `multiple locations`, `see job
  description`, `see jd`, `see description`, `not specified`,
  `none`, `-`, `--`.

- **Salary plausibility window is `[$30K, $5M]`.** Below $30K is
  almost always an hourly rate / typo / version number; above $5M
  is total comp + funding rounds. Adjust both bounds in
  `salary_extractor.py` if real-world data shifts.

- **m060 falls back to the `location` column when `locations_raw` is
  empty/NULL.** Some old rows pre-date the `locations_raw` column or
  were inserted via paths that didn't populate it. Without this
  fallback, m060 would blank their `location` column. Discovered by
  `TestMigrationPreservesData::test_preserves_original_column_values`
  failing — the conftest fixture inserts `location='United States'`
  without `locations_raw`.

- **The dashboard `index.html` budget banner moved into the
  `_stats_cards.html` partial.** Previously inline at lines ~28-41
  of `dashboard/index.html`. Moving it into the partial means it
  auto-refreshes with the rest of the stat cards on `dashboard-
  refresh` — but it also means deleting the partial would lose
  the banner entirely. Be careful with the partial.

- **Migration count is now 62.** Three count assertions in
  `tests/test_migration.py` (the narrative-comment `test_migration_
  count_is_thirteen`, `test_migrations_count_is_19`, and
  `test_migration53_creates_onboarding_state`'s PRAGMA assertion)
  bump in lockstep when new migrations are added. The naming is
  vestigial — the names mention `13` and `19` because that's when
  the tests were added.

- **Full `uv run --active pytest tests/` takes >2 min to produce any
  output, then 2-3 min more to finish.** The targeted suite at the
  top of "How to verify" runs in ~3 min and exercises everything
  this session changed.

## Suggested next step

In rough priority order:

1. **Heal the `TestBatchScoreStart` fixture** (5 min). Add a non-
   empty `jd_full` to the inserts in `app_with_unscored_jobs` so the
   3 failing tests pass. Smallest possible change, immediate value.

2. **Per-company progress for Scan ATS** (30-45 min). See deferred
   items above for the wiring sketch. The user-visible improvement
   is meaningful — "Scanning 5 of 24..." vs "Scanning 24 companies..."
   is a much better feedback loop for the multi-minute waits.

3. **Look for fresh user-visible signals** before tackling lower-
   priority deferred items. The list has accumulated for a while;
   some items may have been silently fixed in adjacent work or no
   longer matter to the user. Ask before opening into them.

The session's primary goal — close the prior session's deferred list
AND the appended User Bug List — is complete. Seven commits, seven
tasks, all tests green except the 3 pre-existing fixture failures
documented above.
