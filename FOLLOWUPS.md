# FOLLOWUPS — 2026-05-27 round 12 (location SPEC closed end-to-end: Q3 + Q1 + D + E)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Rounds 9-11 shipped the location-parsing SPEC's Commits A
(parser + canonical shape), B (m066 schema), and C (Layer-1 scanners
+ upsert_job kwarg). Round 12 (this session) **closed the SPEC** —
both gated parser refinements (Q3 jd_full body hashtag fallback, Q1
country-anchored Springfield disambiguation), the read-side surface
(Commit D dropdowns + Jinja filter + pill renderer), and the m067
backfill (Commit E) that populates 11,908 of 12,383 historic rows
with structured location data.

## What this session shipped

Four commits, in order:

- **`a4c8198`** — `feat(location): parse_locations jd_full body hashtag fallback (SPEC Q3)`
- **`189942a`** — `feat(location): country-anchored Springfield disambiguation (SPEC Q1)`
- **`bd3df3f`** — `feat(jobs): country + workplace_type dropdowns + canonical pill renderer (Commit D)`
- **`d260a70`** — `feat(location): m067 backfill locations_structured for legacy rows (Commit E)`

### Commit Q3 (`a4c8198`) — JD body LinkedIn hashtag fallback

- `parse_locations(raw, *, jd_full=None)` — new keyword-only param.
- `_LI_REMOTE_BODY_RE` / `_LI_HYBRID_BODY_RE` / `_LI_ONSITE_BODY_RE`
  match `#LI-Remote` / `#LI-Hybrid` / `#LI-Onsite` case-insensitive,
  word-boundary. **Bare `remote` / `hybrid` / `onsite` in body prose
  are deliberately NOT matched** — false-positive-prone ("remote
  possibility", "hybrid model").
- Precedence: per-segment token in raw > trailing-slash promotion >
  body hashtag. The body tag promotes ONLY UNSPECIFIED entries.
- Empty/None `raw` returns `[]` even when jd_full has a hashtag — the
  body tag is a workplace fallback for *known* locations, not a
  location source on its own.
- `upsert_job` passes `job.description` as `jd_full` (the
  pre-enrichment JD).
- 11 new tests in `test_location_parser.py`.

### Commit Q1 (`189942a`) — Country-anchored Springfield disambiguation

- `_lookup_city` no longer bails with `city=None` when there are
  multiple gazetteer matches without a region anchor. Instead:
  1. Country anchor in input → highest population in that country.
     `"Springfield, USA"` → Springfield, MO (pop ~167k).
  2. No country anchor + ambiguous → default to US (corpus-dominant
     locale). `"Springfield"` alone → Springfield, MO.
  3. No country anchor + ambiguous + zero US matches → global
     population tiebreak (rare).
- **Trade-off documented in commit:** `"Paris"` alone → Paris, TX
  (pop 24k), not Paris, FR (pop 2.1M). Callers needing non-US Paris
  must supply a country anchor (`"Paris, France"` works correctly).
- Springfield test updated: previously asserted `city=None`, now
  asserts `Springfield, MO`. 3 new tests cover bare-Springfield
  default, Paris US-default trade-off, and Paris-with-country anchor.

### Commit D (`bd3df3f`) — Read-side surface for m066 columns

- `get_distinct_country_codes(conn)` + `get_distinct_workplace_types(conn)`
  in `job_finder/db/_queries.py`. NULLs excluded, ORDER BY ASC.
- `get_filtered_jobs(conn, ..., country=None, workplace_type=None)`
  — value-bound via `?` placeholders; sanity-checked at the boundary
  (country = 2-char alphabetic uppercase; workplace_type ∈ the 4-value
  enum). Garbage input is ignored (returns full set, not zero, not a
  SQL error).
- New Jinja filter `format_canonical_location(value, max_entries=3)`
  in `job_finder/web/__init__.py`. Accepts JSON string from the
  column / JobLocation / list of either / list of dicts. Renders
  `City, Region · Country · Workplace`, comma-joined, capped with
  `+N more` suffix. Falsy/unparseable → empty (caller falls back to
  the legacy `location` string).
- `blueprints/jobs.py`: `_get_filter_kwargs` reads `?country=…` +
  `?workplace_type=…`; `index()` fetches the two distinct lists and
  passes them to the template.
- `templates/jobs/index.html`: two new `<select>`s alongside the
  Location dropdown. The standalone hide_stale checkbox's hx-include
  list was updated so the new params propagate through HTMX swaps.
- `templates/jobs/_row.html`: location cell uses the new filter when
  populated, falls back to `job.location`. `title=` shows raw on hover.
- `templates/jobs/_row_detail.html`: expanded panel renders one
  bg-indigo pill per location entry from `locations_structured`
  (JSON-parsed via the existing `from_json` filter).
- 19 smoke tests in `tests/test_jobs_location_filters.py`.

### Commit E (`d260a70`) — m067 backfill

- `job_finder/web/migrations/m067_backfill_locations_structured.py`.
- Pure py-hook migration. Reads `(dedup_key, locations_raw, jd_full,
  location)`, feeds through `parse_locations(raw, jd_full=jd_full)`,
  writes `(locations_structured, workplace_type, primary_country_code)`.
- Falls back to the `location` column when `locations_raw` is missing
  (legacy rows pre-locations_raw).
- Idempotent: parser is a fixed point on its own output.
- **Applied to live `jobs.db`** during this session — see below.

## How to verify (this session's work)

```powershell
# Round-12's combined test surface (110+ tests, ~3s):
.venv/Scripts/python.exe -m pytest `
  tests/test_location_parser.py `
  tests/test_location_parser_scanner_integration.py `
  tests/test_upsert_job_locations_structured.py `
  tests/test_jobs_location_filters.py `
  tests/test_migration_067_backfill_locations_structured.py `
  tests/test_migration_066_add_locations_structured.py `
  tests/test_migration_invariants.py `
  tests/test_migration.py -v

# Live-DB verification (Flask should be OFF or freshly booted):
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('jobs.db', timeout=5)
print('user_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('jobs with locations_structured:',
      c.execute('SELECT COUNT(*) FROM jobs WHERE locations_structured IS NOT NULL').fetchone()[0])
print('total jobs:', c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
print('country dist:', dict(c.execute(
  \"SELECT primary_country_code, COUNT(*) FROM jobs WHERE primary_country_code IS NOT NULL GROUP BY primary_country_code ORDER BY 2 DESC LIMIT 10\"
).fetchall()))
print('workplace dist:', dict(c.execute(
  \"SELECT workplace_type, COUNT(*) FROM jobs WHERE workplace_type IS NOT NULL GROUP BY workplace_type\"
).fetchall()))
"
# Live DB after this session (verified): user_version=67, 11908/12383 rows
# backfilled, US 8987 + IN 352 + TH/GB 74 + CA 65 ...; REMOTE 1375 / HYBRID 243 /
# ONSITE 45 / UNSPECIFIED 10245.

# UI smoke (Flask must be running):
# 1. Visit http://localhost:5000/jobs — Country dropdown lists US/IN/GB/CA/...
# 2. Filter by country=US → row count drops to US-only.
# 3. Filter by workplace_type=REMOTE → only REMOTE jobs visible.
# 4. Expand a job row → location pill row in the detail panel shows
#    "San Francisco, CA · US · Remote" (or similar) in bg-indigo.

# Pyright clean on Round-12-touched files:
.venv/Scripts/python.exe -m pyright `
  job_finder/web/location_parser.py `
  job_finder/web/__init__.py `
  job_finder/web/blueprints/jobs.py `
  job_finder/db/_queries.py `
  job_finder/db/_jobs.py `
  job_finder/db/__init__.py `
  job_finder/web/migrations/m067_backfill_locations_structured.py
# Expected: 0 errors, 0 warnings.
```

### Phase A (Flask restart from round-11's handoff)

The handoff anticipated needing to restart a running Flask (PID 40796)
to pick up Commit C. **Flask was already off at session start** — the
process is gone, so Phase A collapsed to "next Flask boot picks up
everything automatically". User can start with `uv run job-cannon`
whenever convenient; Commit C is already live in the on-disk code, and
m067 has already backfilled 96% of historic data.

## What I tried / considered but didn't do

- **Lever Option B redux** (call `parse_locations` inside Lever's
  `_to_canonical` and override workplace_type from the structured
  field). Still rejected — SPEC §Layer-1 says "trust structured ATS
  data verbatim (no parsing)". Lever's structured signal is workplace_type;
  freeform location strings stay `unresolved=True` and now go through
  m067's parser path on backfill. Quality is fine in practice.

- **Render the pill renderer on the compact row** as well, not just
  the detail panel. Considered, rejected — compact rows are already
  narrow (15% column width); putting pills there would force truncation
  or layout reflow. The row uses the `format_canonical_location` filter
  for a clean text rendering with `title=` for raw on hover. Pills are
  reserved for the expanded panel where there's room.

- **Bare-token workplace detection** (`#LI-*` plus bare `remote` /
  `hybrid` / `onsite` near top of body). The handoff parenthetical
  suggested this; I deliberately did NOT do it. Rationale documented
  in Q3 commit + parser code: "remote possibility", "hybrid model",
  "primarily on-site" all appear in JD prose as non-workplace tokens.
  The `#LI-*` forms are LinkedIn-specific and the false-positive surface
  is essentially zero. If user requests bare-token coverage later, it
  can be added as a position-bounded scan (first N chars).

- **Run the full 12.5-min test suite.** Ran the focused location +
  migrations + views + ingestion suites (~700 tests total across this
  session) and verified pyright clean on all touched files. The full
  suite would re-validate the unchanged tests; expensive vs. signal.
  If a regression surfaces it'll show up in a focused sweep too.

- **Browser-verify Commit D in Playwright.** Could not — Flask was off
  at session start and starting it would have created a background job
  spinning up schedulers / Ollama / the description reformat backfill.
  CLAUDE.md says UI work benefits from in-browser verification; this is
  carried forward as a manual user-action item. The smoke tests verify
  the routes return 200 with the right markup; visual rendering hasn't
  been eyeballed.

- **Update the m060 / earlier-migration test files** to convert their
  brittle `== NN` assertions to forward-compat `>= NN`. Only m066 needed
  the rename for this session — its 4 assertions broke when m067 landed.
  Earlier migrations had already been converted or had different test
  patterns. The blanket conversion is sound work but out of scope; can
  be a future-session sweep.

- **Run an ATS scan via `POST /companies/scan` to verify Commit C is
  live in-memory**. Same Flask-is-off issue. The next time Flask boots
  + runs an ingestion cycle, Layer-1 scanners will start writing
  `locations_structured` on fresh rows — the m067 backfill already
  covered the historic 96%.

## What's deferred / remaining

### CARRY FORWARD (priority order)

1. **Manual browser smoke** for Commit D when Flask next boots. Quick
   checklist:
   - Visit `/jobs`; Country dropdown shows US / IN / GB / CA / ... and
     Workplace shows REMOTE / HYBRID / ONSITE / UNSPECIFIED.
   - Filter by country=US → result count narrows to US rows.
   - Filter by workplace_type=REMOTE → ~1,375 rows visible.
   - Expand any job → pill row in detail shows "City, Region · CC · WT".
   - HTMX swap on filter change preserves dropdown state.
   - Pre-existing `_row.html` location column still readable
     (canonical or fallback to raw).

2. **Phase E cleanup bundle from round-11 handoff:**
   - **#4 Workable widget endpoint shape verification** — check the 4
     careers_url-tagged Workable companies; if all return 0 jobs, switch
     endpoint to `apply.workable.com/api/v3/...`. Small targeted commit;
     touches `_platforms_workable.py` only.
   - **#11 Pyright unused-args cleanup bundle** — rename
     `path`/`tmp_db_path`/`_ctx`/`mock_score`/`_i` params to `_path` etc.
     across `tests/test_migration.py` (~20 lines),
     `tests/test_careers_crawler.py` (10 lines), `tests/test_ats_scanner.py`,
     `tests/test_scheduler.py`. Single mechanical pass. Round-12 added
     a tiny amount of new noise via the m066/m067 tests touching
     `tmp_db_path`/`_ctx` — bundle with the existing pre-existing noise.
   - **#12 Fix `test_paste_jd_budget_cap_logs_at_info`** — find the
     offending `logger.warning("paste-jd: budget cap reached ...")` in
     `blueprints/jobs.py`, change to `logger.info`. Un-deselect the test.

3. **Phase F Jobvite per-tenant fix (Item #10 from round 11):**
   Add per-tenant `careers_nav_recipe` overrides or a dedicated
   jv-job-list scraper for the 5 unhandled jobvite tenants:
   american-specialty-health, capcom, neogenomics, the-institutes,
   victaulic. Tier-4 escalation. Likely the biggest single commit;
   start with `capcom` and `neogenomics` (both have active listings on
   public sites — failing parse is the bug). Verify with
   `POST /admin/jobs/careers_crawl/run-now` after each per-tenant change.

### Audit-track follow-ups (carried unchanged from rounds 7-11)

4. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Tier-4 crawler. Likely own session.

5. **Manual company aliases UI** (round-3 deferred).

6. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried.

7. **Round-8 carry: does the-institutes slug need manual cleanup?**
   Their careers_url 302s to `?invalid=1`. Data issue. Flag in the
   companies UI; this also affects #3 above.

### Open / advisory items

- **Lever freeform strings — keep `unresolved=True` forever?** As of
  Commit E, every Lever row's location strings now have at least the
  m067-backfill parser output written to `locations_structured`
  alongside the structured workplace_type from the scanner. So Lever
  is effectively fine end-to-end. The original "carried" Lever
  follow-up from round 11 (call parse_locations inside `_to_canonical`)
  is now lower-value — m067 covers the same ground. **De-prioritize.**

- **Bare-token workplace detection in jd_full** (Q3 extension). The
  handoff suggested this; I held off. Worth a brief check after a
  few days of seeing what % of UNSPECIFIED jobs would benefit. If
  >5% of UNSPECIFIED jobs have detectable workplace in body prose
  (above the FP floor), worth re-evaluating with a more conservative
  pattern (e.g., only "Remote" / "Hybrid" / "On-Site" with a leading
  `^` or post-heading anchor).

- **Migration count drift on future migrations.** Round-11/12 converted
  m065 + m066 to `_at_least_NN` style. The 3 generic sites in
  `tests/test_migration.py` (lines 404, 936, 1384) still use exact
  `== NN`. The next migration will need to bump all three again. Could
  be opportunistically converted to `>= NN` next session to stop the
  treadmill. Bundle with #11 (pyright cleanup).

- **Production country distribution sanity.** Top countries after
  m067 backfill: US 8987 (72.6%), IN 352, TH 74, GB 74, CA 65. The
  IN / TH numbers are higher than expected for a US-focused job
  search — worth a one-shot spot check that those rows are real
  India/Thailand postings and not parser mis-anchoring. Not blocking;
  curiosity-grade.

## Quirks the next session should know

Rounds 3-11 quirks still apply. Additions from round 12:

- **m067 reads `jd_full` from the row at backfill time.** That's the
  enriched JD body when present (post-enrichment), or NULL otherwise.
  Some rows that were pre-enrichment when m067 first ran will have
  NULL jd_full → no body-tag promotion happens for them. If those rows
  get enriched LATER and someone wants the body-tag signal applied,
  they'd need to re-run m067 (PRAGMA user_version=66 → run_migrations).
  Not automated — call out if user wants periodic re-backfills.

- **The `format_canonical_location` filter does a lazy import of
  `JobLocation` inside the filter body.** This is intentional — avoids
  a cold-path circular when the filter is registered before
  `location_canonical`'s dataclass binds. Don't hoist it to module
  scope.

- **`get_filtered_jobs(..., country=…, workplace_type=…)` silently
  ignores malformed input.** A bogus `?country=USA` (3-letter, not
  alpha-2) or `?workplace_type=foo` doesn't reduce the result set or
  raise — it's treated as "no filter applied". This is by design (vs.
  SQL injection guard via the sort_by allowlist pattern). If users
  ever want explicit "you gave me garbage" feedback in the UI, the
  blueprint can pre-validate and `abort(400)`.

- **`_lookup_city` now backfills country from a single global match
  AS WELL AS picking a US default for ambiguous bare-token input.**
  These two paths interact: e.g. "Paris" → 2 candidates → US default
  → Paris, TX. But "Tokyo" → 1 candidate (Tokyo, JP) → backfill
  country=JP. Single-match cities bypass the US default entirely. If
  this asymmetry surprises someone, the docstring at line 313 of
  `location_parser.py` covers it.

- **m066's "rows stay NULL" guarantee no longer holds end-to-end.**
  m066 itself still only adds nullable columns, but the next
  migration in line (m067) immediately backfills them. The
  test_migration_066 test that asserted NULL after migration was
  updated to assert post-m067 backfill values. The old "transitional
  state between m066 and m067 shipping" is no longer reachable through
  normal `run_migrations()`.

- **Population-weighted disambiguation is a property of the gazetteer
  bundle, not the parser.** If geonamescache pins a new version with
  different population data, Springfield, MO might be supplanted by
  Springfield, MA or vice versa. The Springfield tests assert
  `region_code='MO'` against current geonamescache 2.x. Add a pinned
  version constraint in `pyproject.toml` if this becomes a flake
  surface.

## Next session's contract

Minimum: pick up the cleanup bundle (#2 above — three small commits:
Workable endpoint switch, pyright unused-args sweep, paste-jd log level
fix). Together ~150 LOC, ~1 hour.

Stretch: Phase F Jobvite per-tenant work (#3). Likely the biggest
single commit of any future session — start with capcom and
neogenomics, verify with `POST /admin/jobs/careers_crawl/run-now`.

Aspirational: tackle either the AI-nav recipes (#4) or the manual
company aliases UI (#5).

**Before doing any of the above:** open `/jobs` in a browser, click
the new Country + Workplace dropdowns, expand a job to see the pill
row. Confirm Commit D actually renders correctly. The smoke tests
cover routing and markup, but visual rendering hasn't been eyeballed.

## Open questions

**RESOLVED in round 12 (this session):**

- ✅ **SPEC Q1 (Springfield):** Implemented and tested. Country-anchored
  with US default for bare-token input. Documented trade-off (Paris
  alone → Paris, TX) in the commit message.
- ✅ **SPEC Q3 (JD body keyword fallback):** Implemented and tested.
  Only `#LI-*` hashtags matched (bare-token detection deferred —
  explicit decision, not omission).
- ✅ **Commit D + E gates:** Both shipped after Q3/Q1 — backfill
  benefits from the refined parser. Live DB verified at 96% backfill
  coverage.

**STILL OPEN (carried from prior rounds, low-priority):**

- **`uv sync` editable-rebuild conflict with running Flask** (round-9
  carry). No new deps this round; pyproject.toml unchanged.

- **Bare-token JD body workplace detection** — see "Open / advisory
  items" #2 above. Hold off until empirical signal justifies the FP
  risk.

- **Manual users of `parse_locations` outside upsert_job** — anything
  that calls it directly today passes positional args only (`raw`).
  The new `jd_full` is keyword-only by design so positional callers
  don't accidentally pass a description string into the wrong slot.
  No known existing callers, but worth grep-checking before any
  future signature change.
