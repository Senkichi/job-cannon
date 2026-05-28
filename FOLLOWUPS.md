# FOLLOWUPS — 2026-05-27 round 11 (location parsing Commit C shipped + items 1-3 cleared)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Rounds 9-10 shipped the location-parsing SPEC Commits A (parser
+ canonical shape) and B (m066 schema). Round 11 (this session)
**verified the round-8 heartbeat fix end-to-end on a real long ATS
scan**, **verified the jobvite tenant pivot**, and **shipped Commit C**
— the 4 Layer-1 scanner mappings + `upsert_job` `locations_structured`
kwarg + denormalized column writes (workplace_type, primary_country_code).

## What this session shipped

Two commits:

- **`31bd40f`** — `feat(location): wire 4 Layer-1 scanners + upsert_job
  locations_structured (Commit C)` (+728 LOC across 11 files).
- One docs commit (this file, after Commit C).

### Code (Commit C, in shipping order)

- **`job_finder/web/location_canonical.py`** — added
  `normalize_workplace_type(value: str | None) -> WorkplaceType` that
  unifies per-vendor enum casings (Ashby PascalCase / Lever kebab-case /
  SmartRecruiters bool / Rippling whatever) into REMOTE/HYBRID/ONSITE/
  UNSPECIFIED.

- **4 scanner files in `ats_platforms_internal/`** — each got a
  `_to_canonical(item) -> list[JobLocation]` helper next to
  `_posting_to_job`, and each `_posting_to_job` dict now carries a
  `"locations_structured"` key:
  - `_platforms_smartrecruiters.py` — `location.{city, region,
    regionCode, country, countryCode, remote}` → single resolved entry;
    `remote: true` → REMOTE
  - `_platforms_ashby.py` — `address.postalAddress.*` (primary) +
    `secondaryLocations[].address.postalAddress.*` + `workplaceType`
    enum + `isRemote` fallback; multi-entry list, posting-level
    workplace_type propagated to all entries
  - `_platforms_lever.py` — `workplaceType` (kebab-case) +
    `categories.{location, allLocations}` → list of `unresolved=True`
    entries carrying structured workplace_type (Lever's location strings
    are freeform — m067 backfill will Layer-2-resolve them later).
    Uses raw-string dedup (`dict.fromkeys`) instead of
    `dedupe_locations` because every unresolved entry has identical
    canonical-tuple keys and would collapse to one.
  - `_platforms_rippling.py` — `locations[].{name, city, state,
    country, workplaceType}` → un-flattened, per-entry workplace_type;
    2-letter state/country compresses to region_code/country_code

- **`job_finder/db/_jobs.py`** — `upsert_job` gains kwarg-only
  `locations_structured: list[JobLocation] | None = None`. When None,
  `parse_locations(job.location)` auto-derives via Layer 2. Both INSERT
  and UPDATE branches write the 3 m066 cols (locations_structured JSON,
  workplace_type, primary_country_code), denormalized from
  `locations[0].*` per SPEC §Schema. Legacy `location` / `locations_raw`
  merge logic untouched per SPEC ("keep existing string columns intact
  for back-compat").

- **`job_finder/web/ats_scanner/_run.py:503`** — the ONE call site
  (`_upsert_one_ats_api_job`) that threads
  `locations_structured=job_dict.get("locations_structured")` into
  `upsert_job`. Every other caller (ingestion_runner, blueprints/jobs,
  careers_crawler, _run_html) keeps the default None and gets Layer-2
  auto-derivation. Verified by `grep -rn 'upsert_job(' job_finder/`.

### Tests (20 new, all green)

- **`tests/test_location_parser_scanner_integration.py`** (NEW, 12
  tests) — one fixture per `_to_canonical` shape per platform:
  full-structured passthrough, multi-location, workplace_type
  normalization edge cases, missing-input fallbacks.

- **`tests/test_upsert_job_locations_structured.py`** (NEW, 8 tests) —
  Layer-1 kwarg-provided writes; Layer-2 auto-derive when kwarg=None;
  empty/placeholder strings → 3 cols NULL; denormalized cols from
  `locations[0]`; UPDATE-branch overwrite (last-seen wins for
  structured); legacy `location`/`locations_raw` preserved.

### Drive-by fixes (Commit C touched these too)

- **`tests/test_careers_crawler.py:103`** — added 3 m066 cols to the
  hand-rolled `CREATE TABLE jobs` fixture. Same fixture-vs-real-schema
  divergence pattern as
  `feedback_test_fixture_vs_real_config_divergence` memory.
- **`tests/test_migration_064_reset_fp_prone_speculative_hits.py:326`**
  — same brittle `== 64` pattern as round-10's m065 fix. Renamed
  `test_run_migrations_brings_db_to_version_64` →
  `_at_least_64` and switched assertion to `>= 64`.

### Items 1-3 (verification carried over from rounds 8-10) — ALL CLEARED

1. ✅ **Flask restart + m065 + m066 applied.** No orphan Flask was
   actually holding port 5000 — the "resistant" python processes the
   user saw were `pyright-langserver --stdio` (LSP for the IDE), not
   Flask. Killed nothing; just `uv run python -m job_finder` →
   `user_version=66`, last_tick_at + 3 location cols all present.

2. ✅ **Jobvite tenant pivot verified.** Of 7 jobvite-careers_url
   companies, **2 return `jobs_found=1`** (pulsepoint, havas media
   network). The other 5 (american-specialty-health, capcom,
   neogenomics, the-institutes, victaulic) consistently return 0.
   Round-8 pivot was partially successful — the round-9 escalation
   trigger ("if all 7 return 0") did NOT fire, but the per-tenant
   tuning gap remains. See #13 in deferred list.

3. ✅ **Heartbeat fix verified on a long-running scan.** Triggered
   ats_scan via `POST /companies/scan` (creates the
   `batch_score_sessions` row that `/admin/jobs/ats_scan/run-now`
   does NOT). 3 concurrent ats_scan sessions (109/110/112) plus 1
   scoring session (111) ran. Session 111 completed clean (scored=19/19).
   Sessions 109/110/112 ran 75+ min without hitting the wallclock kill
   that murdered round-9's session 108 at 30 min — all 3 tick every
   second-ish, scored ≥889/1569 at last check, error_msg=NULL. Round-8
   heartbeat fix is **rock solid**.

## How to verify (this session's work)

```powershell
# Round-11's new tests (20 tests, ~2s):
.venv/Scripts/python.exe -m pytest `
  tests/test_location_parser_scanner_integration.py `
  tests/test_upsert_job_locations_structured.py -v

# Full suite (12.5 min — 3522 passed, 6 skipped, 2 deselected, 3 xfailed):
.venv/Scripts/python.exe -m pytest tests/ `
  --ignore=tests/e2e `
  --deselect tests/test_log_levels.py::TestJobsBlueprintLogLevels::test_paste_jd_budget_cap_logs_at_info `
  -q --tb=line --no-header

# Pyright clean on Commit-C-touched files:
.venv/Scripts/python.exe -m pyright `
  job_finder/web/location_canonical.py `
  job_finder/web/ats_platforms_internal/_platforms_smartrecruiters.py `
  job_finder/web/ats_platforms_internal/_platforms_ashby.py `
  job_finder/web/ats_platforms_internal/_platforms_lever.py `
  job_finder/web/ats_platforms_internal/_platforms_rippling.py `
  job_finder/db/_jobs.py `
  job_finder/web/ats_scanner/_run.py
# Expected: 0 errors, 0 warnings

# Live-DB heartbeat tick check (Flask should be running):
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('jobs.db', timeout=5); c.row_factory = sqlite3.Row
for r in c.execute(\"SELECT id, session_type, status, started_at, last_tick_at, scored, total FROM batch_score_sessions WHERE status='running'\"):
    print(dict(r))
"
# Expected: any running session has last_tick_at populated within last 5 min.

# Smoke for Commit-C ingestion (requires Flask AND a future ingest cycle):
.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('jobs.db', timeout=5)
print('jobs with locations_structured populated:',
      c.execute('SELECT COUNT(*) FROM jobs WHERE locations_structured IS NOT NULL').fetchone()[0])
print('country distribution:',
      dict(c.execute(\"SELECT primary_country_code, COUNT(*) FROM jobs WHERE primary_country_code IS NOT NULL GROUP BY primary_country_code ORDER BY 2 DESC LIMIT 10\").fetchall()))
"
# Expected pre-restart: 0 / {}. After Flask restart + one ingest cycle:
# non-zero counts for Layer-1 scanner sources (SmartRecruiters, Ashby,
# Lever, Rippling) — and for Layer-2 sources too (Greenhouse, Workday,
# Gmail parsers, SerpAPI, ...) since upsert_job auto-derives via
# parse_locations.
```

## What I tried / considered but didn't do

- **Wait for SPEC Q1 user confirmation** (Springfield disambiguation,
  city=None default). Still unconfirmed. Recommend explicit confirmation
  before Commit E ships — once m067 backfill runs, historic data follows
  the chosen behavior. Carried.

- **Commit D (read-side dropdowns + Jinja filter)** — deliberately
  deferred. Multi-file, multi-template, and per `CLAUDE.md` UI work
  benefits from in-browser verification. Better as a focused next
  session where the user can drive Playwright or just look at the
  result. See "Next session's contract" below.

- **Commit E (m067 backfill)** — deferred. Per SPEC, ships AFTER B-D
  have been in production for at least one ingestion cycle so the
  parser is trusted on fresh data first. Plus gated on SPEC Q1.

- **Restart Flask after committing Commit C.** Left the existing
  Flask (PID 40796) running with old upsert_job in memory. The user
  will pick up Commit C on their next manual restart. This is intentional
  — preserves the active ats_scan + careers_crawl runs and avoids
  killing the 75-min heartbeat verification mid-flight.

- **Lever Option B (call parse_locations inside `_to_canonical` and
  override workplace_type)** — considered; rejected for SPEC fidelity.
  SPEC §Layer-1 says "trust structured ATS data verbatim (no parsing)";
  Lever's only structured signal is workplaceType, so the freeform
  location strings stay unresolved at the scanner boundary. m067
  backfill or a future scanner-side parse step closes this gap.

- **Update all 13 other hand-rolled `CREATE TABLE jobs` test fixtures**
  with the 3 m066 cols — deferred. Only `test_careers_crawler.py`
  actually triggers upsert_job through a fixture path that needs the
  schema. Other files don't write through `upsert_job` (or use the
  migrated-DB fixture pattern instead). When future migrations land,
  the same opportunistic fix on whichever fixture breaks will work.

- **Stop the running Flask before Commit C** — explicitly chose NOT to.
  Heartbeat verification needed the live scan; killing it would have
  forfeited round-8's acceptance test. The trade-off: Commit C is on
  disk but not in the live process — meaning all jobs ingested in the
  current session window have NULL for the 3 m066 cols. Acceptable
  because (a) m067 backfill will catch them; (b) the heartbeat
  verification was the higher-value signal.

## What's deferred / remaining

### CARRY FORWARD (priority order)

1. **Restart Flask to pick up Commit C.** Currently running Flask
   (PID 40796 at session end) has Commit C on disk but not in memory.
   ```powershell
   # Kill old Flask (Ctrl+C in its terminal, or):
   $p = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
   if ($p) { Stop-Process -Id $p.OwningProcess -Force }
   # Restart:
   uv run job-cannon
   # Then watch a new ingest cycle and verify m066 cols populating:
   .venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('jobs.db'); print(c.execute('SELECT COUNT(*) FROM jobs WHERE locations_structured IS NOT NULL').fetchone()[0])"
   ```

### Location parsing SPEC — next commits in order

2. **Commit D — Read-side dropdowns + Jinja filter.** ~120 LOC + UI
   smoke tests. Concrete pieces:
   - `job_finder/web/__init__.py` — register Jinja filter
     `format_canonical_location(loc_or_list, *, max_entries=3) -> str`
     that handles both single JobLocation and list[JobLocation]. Use
     for pill rendering and tooltip body.
   - `job_finder/web/blueprints/jobs.py` — add two filter dropdowns:
     - Country: `SELECT DISTINCT primary_country_code FROM jobs WHERE
       primary_country_code IS NOT NULL ORDER BY primary_country_code`
     - Workplace_type: `SELECT DISTINCT workplace_type FROM jobs WHERE
       workplace_type IS NOT NULL ORDER BY workplace_type`. Both feed
       into the existing `get_filtered_jobs` SQL via new optional
       filter params (per the established `sort_by` allowlist pattern
       — country/workplace_type must be in a Python-side allowlist
       before SQL interpolation).
   - Templates: `templates/jobs/_list.html` and
     `templates/jobs/_detail.html` get a pill renderer (small bg-indigo
     pill per location entry; tooltip shows full raw on hover).
   - Tests: smoke routes confirm dropdowns render + filter SQL works.
     For visual, manual browser check per CLAUDE.md.
   - **Browser test plan:** the dropdowns will be sparse until #1
     above (Flask restart) lands AND one ingest cycle runs — only then
     do the m066 cols populate on fresh data. m067 backfill (#3 below)
     fills historic data.

3. **Commit E — Migration m067 backfill.** Re-parse every existing
   row's `locations_raw` through `parse_locations`, write the 3 new
   columns. Idempotent. ~120 LOC + tests. Invariant bumps from 66 → 67
   at the same 4 sites m066 bumped + the new pattern site (the
   `test_migration_064` rename this session established for future
   refits).
   - **GATE A (SPEC Q1):** User-side confirm Springfield ambiguity
     behavior before backfill freezes historic data.
   - **GATE B (SPEC ordering):** Per SPEC, ships AFTER Commit D has
     been live for ≥1 ingest cycle.

### Audit-track follow-ups (carried unchanged from rounds 7-10)

4. **Workable widget endpoint shape verification.** Check the 4
   careers_url-tagged Workable companies; if all return 0 jobs, switch
   the endpoint to `apply.workable.com/api/v3/...`. Small targeted
   commit; touches `_platforms_workable.py` only.

5. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech, Citi,
   Kaiser Permanente). Tier-4 crawler. Likely own session.

6. **Manual company aliases UI** (round-3 deferred).

7. **Pyright `int | None` cleanup** in test_ats_scanner.py.

8. **`_make_app` helper bug** in test_scheduler.py.

9. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried.

10. **Real Jobvite scraper for 5 unhandled tenants** (american-specialty-
    health, capcom, neogenomics, the-institutes, victaulic). The
    round-8 pivot worked for 2/7 (pulsepoint, havas) but the other 5
    consistently return 0. Tier-4 escalation: per-tenant
    `careers_nav_recipe` overrides OR a Jobvite-specific scraper.

11. **Drive-by pyright noise.** Pre-existing warnings in
    `tests/test_migration.py` (~20 unused `path`/`tmp_db_path`/`_ctx`
    args). New noise from C-touched tests in `test_careers_crawler.py`
    (10 `mock_score is not accessed` / `result is not accessed` /
    `_i is not accessed`) — also pre-existing, just surfaced by my
    edits. Bundle with #7 + #8 when convenient.

12. **Pre-existing test failure:**
    `tests/test_log_levels.py::TestJobsBlueprintLogLevels::test_paste_jd_budget_cap_logs_at_info`.
    Static source check that
    `blueprints/jobs.py` "paste-jd: budget cap reached" line uses
    `logger.info`, not `logger.warning`. Unrelated to Commit C —
    confirmed by `git log -- blueprints/jobs.py` (no recent changes
    to that line). Deselect or fix in a follow-up commit.

13. **Round-8 carry: does the-institutes slug need manual cleanup?**
    Their careers_url 302s to `?invalid=1`. Data issue. Flag in the
    companies UI; this also affects #10 above.

## Quirks the next session should know

Round-3-through-round-10 quirks still apply. Additions from round 11:

- **`/admin/jobs/ats_scan/run-now` does NOT create a
  `batch_score_sessions` row.** That's the scheduled background job
  path. To exercise the heartbeat machinery (and the
  `_run_ats_scan_bg` thread that ticks `last_tick_at`), POST to
  `/companies/scan` instead. The admin run-now path triggers the
  scheduled job which writes to `runs` table, not `batch_score_sessions`.

- **`/companies/scan` can take 30-60s under load to return the initial
  202-equivalent response** (the inserting-eligible-companies SELECT is
  expensive when 3782 companies + an active ats_scan + careers_crawl
  are competing for the writer lock). Use `-m 60` with curl, or expect
  a `TimedOut` exception with default 15s PowerShell `Invoke-WebRequest`.

- **Multiple concurrent `ats_scan` sessions** is the expected state
  after enough manual triggers. The round-8 heartbeat fix + the
  writer-lock starvation fix from earlier rounds let them progress in
  parallel without deadlocking. The `_job_currently_running` check in
  the admin run-now route only blocks scheduler-job-launched concurrency,
  NOT cross-source concurrency (scheduler + /companies/scan can both
  run).

- **`pyright-langserver --stdio` python processes look like Flask
  orphans.** When `Get-Process python` shows long-running .venv pythons,
  check cmdline before killing — `(Get-CimInstance Win32_Process -Filter
  'ProcessId=NNN').CommandLine`. LSP servers persist across IDE
  sessions and SHOULD NOT be killed.

- **`dedupe_locations` collapses all `unresolved=True` entries to one.**
  The canonical dedup key `(country_code, region_code, city,
  workplace_type)` is identical (`(None, None, None, wt)`) for every
  unresolved entry. Lever's `_to_canonical` uses `dict.fromkeys(raw)`
  for raw-string dedup instead. Anyone adding a new freeform-string
  Layer-1 mapper hits this trap.

- **`@dataclass(frozen=True, slots=True)` JobLocations require
  `dataclasses.replace()` to "change" workplace_type.** Direct
  attribute assignment raises. The Lever path avoids replace() by
  building unresolved entries directly with the structured wt at
  construction time.

- **Layer-1 ↔ Layer-2 boundary lives inside `upsert_job`.** Scanner
  dict carries `"locations_structured": list[JobLocation]` (Layer-1
  scanners populate it; Layer-2 sources don't). `upsert_job(...,
  locations_structured=None)` triggers Layer-2 auto-derivation. No
  caller needs to know which layer it's in — that decision lives at the
  upsert boundary.

- **Live DB drift survives Flask shutdown** (round-10 quirk
  reaffirmed). Migration only runs at Flask startup. Always check
  `PRAGMA user_version` if uncertain about the live DB schema state.

## Next session's contract — Commit D (+ pull #1 first)

Scope: Item #1 (Flask restart to pick up Commit C) + Item #2 (Commit D)
+ Items #4, #11, #12 if time permits.

### Verification before any code

1. **Flask restart + verify m066 cols populate on fresh ingests.** See
   the snippet under CARRY FORWARD #1. Without this, Commit D's
   dropdowns will be empty since the cols are NULL on every existing
   row + every row ingested under the round-10 Flask boot.

### The work

2. **Commit D — read-side dropdowns + `format_canonical_location` Jinja
   filter + pill renderer.** See "What's deferred" #2 for the concrete
   piece list. ~120 LOC + UI smoke tests.

   - **Visual verification per CLAUDE.md.** After committing, open
     `http://localhost:5000/jobs` in a browser. Check:
     - Country dropdown shows distinct values
     - Workplace dropdown shows distinct values
     - Pills render on job rows + job detail
     - Filtering by either dropdown narrows the list correctly
     - HTMX swaps don't break the dropdown state
   - If browser-test is impossible (no manual driver), use the Flask
     test client to confirm route returns expected fragments.

3. **(Bonus) Item #4 — Workable widget endpoint shape verification.**
   Small targeted commit. Run the next ATS scan, check the 4 Workable
   careers_url-tagged companies. If all 0, switch endpoint to v3 path.

4. **(Bonus) Item #11 — Bundle pyright noise cleanup.** A single
   passing commit renaming unused params to `_path` etc. across
   test_migration.py + test_careers_crawler.py + test_ats_scanner.py +
   test_scheduler.py.

5. **(Bonus) Item #12 — Fix `test_paste_jd_budget_cap_logs_at_info`.**
   Find the offending `logger.warning("paste-jd: budget cap reached
   ...")` in `blueprints/jobs.py` and change to `logger.info`. Then
   un-deselect the test.

### Session-end success criterion

Minimum: items #1 + #2 (Flask restart + Commit D shipped + browser-
verified). Stretch: + #3 or #4 or #5. Commit E is explicitly NOT in
this session's scope — it stays gated by SPEC Q1 + the SPEC's "one
ingest cycle after D" rule.

## Open questions

- **SPEC Q1 (Springfield ambiguity)** — still unconfirmed. Round 9
  shipped `city=None when region missing + multiple matches`. Confirm
  before Commit E.

- **SPEC Q3 (JD body keyword fallback)** — `parse_locations` does not
  yet accept a `jd_full` parameter. Should it? If yes, which commit
  (D for read-side, E for backfill, or its own micro-commit)?

- **Lever freeform strings — keep `unresolved=True` forever?** Today
  Lever entries depend on m067 backfill to ever resolve. A follow-up
  could call `parse_locations` inside Lever's `_to_canonical` and
  override workplace_type from the structured field — gives quality
  data from day 1 but bends the SPEC §Layer-1 "bypass parser" rule.
  Worth a brief user check.

- **Round-8 carry: the-institutes slug** still 302s to `?invalid=1`.
  Data cleanup or scrape-aware fallback?

- **`uv sync` editable-rebuild conflict with running Flask** (round-9
  carry). No new deps in round 11.
