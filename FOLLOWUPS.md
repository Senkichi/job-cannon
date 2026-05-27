# FOLLOWUPS — 2026-05-27 user-bug-list + parser-audit cleanup

## What this session shipped

Job Cannon is a single-user, local-only Flask command center for job search
(see CLAUDE.md). This session worked through the full triage list left in the
prior FOLLOWUPS.md (F3–F7 polish carryover + 2026-05-27 parser-bug audit) and
the **User Generated Bug List** the user appended at the bottom. Every item
that wasn't explicitly marked "informational only" was addressed.

Commits, in order:

1. `refactor(blueprints): drop unreachable _render_scoring_done helper` — deleted
   the dead 18-line function in `batch_scoring.py:29`.
2. `fix(parsers): reject 'Confidential' aggregator placeholder` — hard-coded
   reject in `classify_company_name` so aggregator-withheld rows don't pollute
   the companies UI or burn scoring spend. Tests in
   `tests/test_ats_company.py` (new file).
3. `fix(blueprints): guard load_job_context re-fetch points in jobs.py` — six
   pyright `reportOptionalSubscript` errors cleared by adding `if ctx is None:
   return "", 404` guards at the re-fetch sites (paste_jd, rescore, save_jd).
4. `fix(scoring): stop counting skipped envelopes as scored; require jd_full
   for scorable` — the marquee bug from the user list. Two compounding
   problems: `_run_batch_bg` was treating any non-None `ScoringResult`
   envelope as a "scored" row (skipped/error envelopes are non-None too, and
   don't write classification), and `count_scorable` was advertising
   pre-enrichment rows the v3 scorer can't process. Both fixed; regression
   tests in `tests/test_batch_scoring.py::test_skipped_envelopes_do_not_count_as_scored`
   and `tests/test_exclusion_filter.py::TestCountScorable`.
5. `fix(detections): OOB-swap pipeline-review badge on confirm/dismiss` —
   extracted the section header into `_pipeline_review_header.html`, return
   it as `hx-swap-oob="true"` from the confirm/dismiss routes so the badge
   count decrements with the same request that fades the card. Test updated
   in `test_detections_blueprint.py::test_dismiss_response_carries_oob_header_only`.
6. `fix(companies): use 'intersect once' trigger so infinite scroll fires
   inside the scrolling main-content container` — root cause was
   `base.html:42` wrapping content in `overflow-y-auto`, and HTMX's
   `revealed` trigger doesn't fire reliably inside CSS-overflow scroll
   containers. Switched both `_table.html` and `_rows_partial.html` to
   `intersect once`.
7. `feat(companies): visible progress card + button-disable for Scan ATS` —
   added a `#scan-progress` indicator card with scannable count + JS
   countdown (4 s/company heuristic), `hx-indicator=".scan-busy"` to make
   it visible during the request, `hx-disabled-elt="this"` on the button
   to block double-click. Route is still synchronous (see "Next step" below).
8. `fix(settings): show '(saved)' placeholder for Adzuna/Jooble/USAJobs after
   keyring move` — display-only bug. `settings.index()` builds a `secret_set`
   dict but the dict was missing the five `sources.portal_search.*` canonical
   names. Template placeholders now use `secret_set` like JSearch already
   does. **The ingestion path was never broken** — `_inject_portal_search_creds`
   in `ingestion_runner.py` correctly calls `get_secret()` for all three
   portals.
9. `fix(companies): m058 consolidates duplicate company rows` — one-shot
   data heal. Numeric-prefix orphans (`100 Salesforce`, `001_ bcbsa`, etc.)
   re-pointed and merged into their canonical row; exact-name duplicates
   collapsed to the lowest id. FK re-points covered: `jobs.company_id`,
   `company_scan_log.company_id`, `company_research.company_id` (latter
   table-exists-guarded). Migration is idempotent on re-run, no-op on
   clean DBs. Tests in `tests/test_migration_058_consolidate_duplicate_companies.py`.
10. `fix(careers_crawler): reject metadata-blob titles before they enter the
    pipeline` — added `_is_metadata_blob()` predicate in `_title_filters.py`
    that catches titles >140 chars, titles containing "Posted ", "Apply by",
    "Agency", "Post level", req-ID-pipe patterns, or dollar signs. Wired
    into `_extract_jobs_from_soup` so blob rows are dropped before
    persistence. **Existing polluted rows are unaffected** — see "Next step".
11. `fix(careers_crawler): guard _CITY_SUFFIX_RE against short-ALLCAPS
    overstrip` — `MSI - Marvell Semiconductor` was collapsing to `MSI`.
    Guarded the regex with a check that the prefix has ≥5 chars and
    contains at least one lowercase letter; ALLCAPS abbreviations now
    survive intact. Tradeoff captured in commit message: bare-city suffixes
    after short titles will leak through, but those titles are still
    informative.

## How to verify the work

```powershell
# All affected test files pass (350 tests; takes ~2 min on this machine)
uv run --active pytest tests/test_ats_company.py tests/test_exclusion_filter.py `
  tests/test_batch_scoring.py tests/test_detections_blueprint.py `
  tests/test_companies.py tests/test_settings.py tests/test_migration.py `
  tests/test_migration_058_consolidate_duplicate_companies.py `
  tests/test_careers_crawler.py tests/test_polling_status.py `
  tests/test_v3_rescore.py tests/test_scoring_orchestrator.py

# Pyright clean on jobs.py
uv run --active pyright job_finder/web/blueprints/jobs.py

# Browser-side checks (require the dev server running on :5000):
# - Dashboard: click "Score N unscored jobs". Once jobs that need enrichment
#   are excluded, the button should now show 0 or fewer scorable.
# - Pipeline Review: confirm/dismiss any detection. The badge in the section
#   header should decrement in the same response (no full reload needed).
# - Companies: scroll to bottom of a >50-row company list. "Loading more..."
#   should fire the next page automatically.
# - Companies: click "Scan ATS". The card under the button should show
#   "Scanning N companies..." with a live countdown.
# - Settings: enter an Adzuna app_id, save, reload. The input should show
#   "(saved — type to replace)" placeholder, not "(not set)".
```

## What's deferred / remaining

### From the original FOLLOWUPS (carried forward, informational)

- **Pyright union-narrowing warnings in `tests/test_polling_status.py`** —
  `render_polling_status` returns `str | Response`; tests trigger pyright
  false positives. No fix needed; runtime is correct.
- **`make_response` lazy import in `db_helpers._attach_hx_trigger`** —
  intentional to keep the early-startup import graph slim. Noted only as a
  hint for future contributors.

### New deferred items surfaced this session

- **Scan ATS is still synchronous.** Commit 7 adds visible progress feedback
  but the route blocks the request until the scan finishes. The proper fix
  mirrors `batch_scoring`: background thread + session row + polling
  endpoint + done-fragment. Estimated 1–2 hours of work. The progress card
  already exists, so wiring polling on top of it is mostly mechanical.
- **`dashboard-refresh` HX-Trigger has no listener.** `_quick_actions.html`
  and `_stats_cards.html` carry comments claiming "auto-refreshed via
  dashboard-refresh event", but no element in any template has
  `hx-trigger="dashboard-refresh from:body"`. Either the listening wrapper
  was removed during a refactor and the comment never caught up, or this
  was always aspirational. The pipeline-review-header OOB pattern from
  commit 5 sidesteps this for that one element, but stats cards / quick
  actions silently rely on full-page reloads instead of the documented
  event-driven refresh. Worth a focused audit + restore.
- **Existing `careers_crawl` rows with title bleed are not cleaned up.**
  Commit 10's guard only stops new bleed. A one-shot heal migration could
  null-out or re-derive titles for the ~30+ existing rows; query is
  `SELECT title FROM jobs WHERE LENGTH(title) > 140 AND sources LIKE
  '%careers_crawl%'`. Decide whether to drop the row, blank the title (and
  re-fetch), or accept the existing pollution.
- **`_CITY_SUFFIX_RE` is still imperfect.** The new guard fixes the worst
  failure mode (short ALLCAPS prefixes), but bare multi-word cities without
  a state code (e.g. `Senior Engineer - New York`) still won't be stripped.
  The structural shape is identical to a brand name; a curated
  location-name allowlist is the only fully-correct fix. Lower priority
  than the cleanup migration above.

## Quirks the next session should know

- **Full `uv run --active pytest tests/` takes >2 min to produce any
  output**, then 2–3 min to finish. A targeted suite that hits only the
  affected files (above) runs in ~140 s and exercises every behaviour this
  session changed. If you need to dial in faster: `pytest -x` stops on the
  first failure, `-k name` filters by test-id substring.
- **`_insert_unscored_job` in `test_batch_scoring.py` now seeds `jd_full`
  by default.** Pre-this-session it inserted rows without a JD, which
  matched `count_scorable`'s old (incorrect) predicate. Several existing
  tests that called the helper without thinking about JD were silently
  relying on that. Tests that need an empty `jd_full` should `UPDATE` the
  row after insert.
- **`hx-indicator` accepts a CSS selector that can match multiple
  elements.** Commit 7 uses `hx-indicator=".scan-busy"` and puts the class
  on both the in-button spinner and the prominent progress card — both
  receive the `.htmx-request` class together and become visible in sync.
- **Migration files are auto-discovered** by `migrations/__init__.py::_discover`
  via filename pattern `m{NNN}_*.py`. m058 is wired automatically; no
  registry update needed. When adding the next migration, mirror the m057
  / m058 pattern (single `MIGRATION = Migration(...)` constant).
- **`classify_company_name` runs `normalize_company()` first**, which strips
  legal suffixes (Inc, LLC, Holdings, etc.). The new "Confidential"
  hardcoded reject catches `Confidential, LLC` and `Confidential Holdings`
  alongside the bare form — useful when an aggregator adds a legal suffix.
- **`get_secret(canonical_name)`** is the only correct way to read a
  keyring-migrated secret. Plain `config.get(...)` returns empty string
  after `_move_secret_to_keyring` ran. Pre-session bug 8 was caused by the
  Settings-page template forgetting this; ingestion was never affected
  because `_inject_portal_search_creds` already routes through `get_secret`.

## Suggested next step

Pick from the "deferred items" list above. Ordered by user-visible impact:

1. **Make Scan ATS async with polling.** Highest-leverage UX fix —
   eliminates the multi-minute synchronous wait. Mirror `batch_scoring`
   exactly (session row, background thread, polling endpoint, terminal
   done-fragment). The visible-progress card from commit 7 already exists;
   wire `hx-trigger="every Xs"` polling to a status endpoint that returns
   the same card with updated counts.
2. **Heal existing careers_crawl bleed.** Either delete rows where
   `LENGTH(title) > 140 AND sources LIKE '%careers_crawl%'`, or null the
   title and let enrichment re-fill from source. Probably wrap in an m059
   migration for atomicity + audit trail.
3. **Audit dashboard-refresh wiring.** Find the lost listener (or confirm
   it never existed) and either restore the auto-refresh pattern or
   replace the misleading comments with documentation of what actually
   triggers the partials' refresh.

The session goal — turn over the entire FOLLOWUPS triage list — is
complete. The remaining items here are net-new findings or genuinely
deferred work, not unfinished work from the original list.
