# FOLLOWUPS — 2026-05-27 round 9 (location parsing Commit A shipped)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Rounds 7-8 closed the ATS-platform expansion + the heartbeat-
based polling timeout + the jobvite-careers_crawler pivot. Round 9
(this session) executed Commit A of the location parsing SPEC. The
remaining round-8 verification items (jobvite Playwright pickup +
heartbeat end-to-end) are CARRIED FORWARD unchanged — they need
post-Flask-restart observation that this session couldn't drive.

## What this session shipped

One commit: **`8ce7f14`** — `feat(location): add JobLocation canonical
shape + Layer 2/3 parser (Commit A)`. +1279 LOC across 5 files. New
deps `pycountry>=24.0,<25` + `geonamescache>=2.0,<3` in main pyproject
deps (NOT extras — required at app boot once Commit C wires
`upsert_job`).

New code (no call site reaches it yet — Commits B + C will):
- `job_finder/web/location_canonical.py` (158 LOC) — frozen `JobLocation`
  dataclass (schema.org PostalAddress + LinkedIn workplaceType enum
  casing), `dedupe_locations`, `to_json` / `from_json` with forward-
  compat unknown-field tolerance, `unresolved_from_raw` helper.
- `job_finder/web/location_parser.py` (644 LOC) — `parse_locations()`
  entry point with Layer 2 (gazetteer) + Layer 3 (heuristic). Country
  anchor via pycountry with `UK`→`GB` + `USA`→`US` alias map to dodge
  the pycountry alpha-2 "UK→Uganda" trap. Region via
  `pycountry.subdivisions` (input is source of truth; gazetteer
  admin1code is only ISO 3166-2 for US, FIPS-numeric elsewhere — used
  as a fallback only). City via `geonamescache` with population-
  weighted tiebreak + dedup-by-geonameid to fix the alternatenames
  triple-index bug. Trailing `/ Remote` promotes workplace onto the
  preceding row (one entry); `or Remote` keeps entries distinct
  (two entries). Region anchoring requires ≥2 remaining segments
  so `"Manchester, UK"` parses to city=Manchester rather than
  consuming Manchester as a GB metropolitan county.
- `tests/test_location_parser.py` (447 LOC) — **52 tests, all green**.
  SPEC anchor corpus byte-for-byte + ambiguity guards (Springfield
  US has 8 matches; with no state → `city=None`) + country aliasing +
  workplace detection + multi-loc separators + dedup invariants +
  frozen-dataclass invariants + JSON round-trip with forward-compat.

## How to verify (this session's work)

```powershell
# Round-9 commit's tests:
.venv/Scripts/python.exe -m pytest tests/test_location_parser.py -v

# Targeted regression sweep (location adjacent + round-8 anchors):
.venv/Scripts/python.exe -m pytest `
  tests/test_location_parser.py `
  tests/test_location_normalizer.py `
  tests/test_migration_065_add_polling_session_heartbeat.py `
  tests/test_polling_status.py `
  tests/test_round6_ats_scanners.py `
  tests/test_migration_invariants.py `
  tests/test_migration.py `
  -q --tb=short

# Pyright clean on the two new modules:
.venv/Scripts/python.exe -m pyright job_finder/web/location_parser.py `
                                    job_finder/web/location_canonical.py
# Expected: 0 errors, 0 warnings

# Quick anchor-corpus smoke test (without pytest):
.venv/Scripts/python.exe -c "
from job_finder.web.location_parser import parse_locations
for case in ['San Francisco, CA / Remote', 'Bengaluru, KA, India',
             'London, UK or Remote', 'Hybrid - Toronto, ON, Canada',
             'Multiple Locations', 'Springfield, USA']:
    print(repr(case), '->', parse_locations(case))
"
```

## What I tried / considered but didn't do

- **Verify careers_crawler picks up jobvite-URL companies via Playwright.**
  Round-8 step #1. Requires waiting for the next 5:00 AM `careers_crawl`
  cron to fire OR triggering the crawler manually. Out of scope this
  session — carry forward as priority #1 below.

- **Verify heartbeat end-to-end on a real long ATS scan.** Round-8
  step #2. **Drift finding from this session's Phase-2 verify:** the
  live DB is at `user_version=64`, NOT 65. The m065 source is in
  place + tests pass, but a Flask process from BEFORE the round-8
  commits is running on port 5000 (PID 34520 as observed during this
  session — restart of localhost:5000 may have changed the PID by
  the time you read this). Session 108 (`ats_scan`, total=994,
  scored=0, error_msg="Session timed out (>30 min)") confirms the
  running Flask was using PRE-heartbeat code (old error message).
  Migration won't fire until Flask restarts. Verification step #2
  requires that restart and then a fresh long-running scan.

- **Inline the Springfield disambiguation per SPEC Q1.** The SPEC's
  open Q1 default (`city=None` when region is missing AND multiple
  gazetteer matches exist) is what shipped — matches the SPEC's
  stated default. If the user wants to revisit later (Q1 says
  "Confirm before implementation"), the change point is
  `_lookup_city`'s "len(candidates) >= 2 and not region_code → return
  None" branch.

- **JD body fallback (SPEC Q3).** Parser doesn't yet scan JD bodies for
  `#LI-Remote` hashtags as a last-resort workplace signal. The regex
  is already in `_REMOTE_TOKEN_RE` so it'll match if a future caller
  passes JD excerpts through `parse_locations`, but there's no formal
  `jd_full` parameter yet. Adding the parameter is a small follow-up
  in either Commit C (scanner integration) or a separate enhancement
  commit.

- **Run the full pytest suite.** The e2e Playwright tests under
  `tests/e2e/` would hang on the running Flask process (browser
  collisions on port 5000). I ran a targeted 285-test regression
  sweep instead — all green. A clean post-restart full sweep should
  happen as a sanity check after Flask is restarted.

- **Implement Commit B (m066) inline.** Out of scope. The remaining
  SPEC commits B / C / D / E are still queued; see below.

## What's deferred / remaining

### CARRY FORWARD from round 8 (still highest priority)

1. **Restart Flask and verify m065 actually applies.** The live DB is
   stuck at `user_version=64` because Flask hasn't been bounced since
   the round-8 commits. Restart, then verify:
   ```powershell
   .venv/Scripts/python.exe -c "
   import sqlite3
   c = sqlite3.connect('jobs.db')
   print('user_version =', c.execute('PRAGMA user_version').fetchone()[0])
   cols = [r[1] for r in c.execute('PRAGMA table_info(batch_score_sessions)').fetchall()]
   print('last_tick_at present:', 'last_tick_at' in cols)
   "
   # Expected post-restart: user_version=65, last_tick_at present: True
   ```
   Note: if PID 34520 (the Flask process I found) is gone before the next
   session starts, the verification probably already happened (a restart
   would have done it). Run the check anyway.

2. **Verify careers_crawler picks up jobs.jobvite.com via Playwright Tier-3.**
   After Flask restart + next 5:00 AM `careers_crawl` cron, run:
   ```sql
   SELECT c.name, c.careers_url, csl.jobs_matched, csl.created_at
   FROM company_scan_log csl
   JOIN companies c ON c.id = csl.company_id
   WHERE c.careers_url LIKE '%jobvite%'
   ORDER BY csl.created_at DESC LIMIT 20;
   ```
   The 7 jobvite-URL companies should show `jobs_matched > 0` if the
   round-8 architectural pivot (drop jobvite from URL fast-path) worked.
   If all 7 return 0 jobs after multiple cycles, jv-job-list rendering
   needs per-tenant tuning (custom `careers_nav_recipe` or AI-nav).

3. **Verify heartbeat on a real long ATS scan.** Trigger via /companies
   UI button. With 908 hits + 303 pending, scan should take 30-60+ min.
   Verify: progress fragment does NOT flip to error past 30 min;
   `batch_score_sessions.last_tick_at` populates; on completion
   `error_msg` is NULL.

### Location parsing SPEC — next commits in order

4. **Commit B — Migration m066** (column add: `locations_structured`,
   `workplace_type`, `primary_country_code` on jobs). All NULL on
   existing rows. Plus invariant bumps in 4 sites
   (`test_migration_invariants.py:27`, `test_migration.py:402`,
   `test_migration.py:934`, `test_migration.py:1383`) from 65 → 66.
   ~80 LOC + ~11 m066 tests. Zero risk (nullable additions).

5. **Commit C — Scanner Layer-1 wiring + upsert_job changes.**
   - Add `_to_canonical(item) -> list[JobLocation]` helpers next to
     `_extract_one_listing` in `_platforms_smartrecruiters.py`,
     `_platforms_ashby.py`, `_platforms_lever.py`,
     `_platforms_rippling.py`. Map vendor structured fields to
     `JobLocation` per SPEC Layer-1 table.
   - Wire `upsert_job` (in `job_finder/web/db/_jobs.py`) to accept
     `locations_structured: list[JobLocation] | None = None`. When
     `None`, derive via `parse_locations(locations_raw)` (Layer 2).
     Write the 3 new columns alongside existing `location` /
     `locations_raw` for backward compat.
   - ~150 LOC + 4 scanner integration tests.

6. **Commit D — Read-side dropdowns + Jinja filter.** Country +
   workplace_type dropdowns in `blueprints/jobs.py` sourced from the
   new denormalized convenience columns. New
   `format_canonical_location` Jinja filter in `web/__init__.py`. Pill
   renderer in job detail/row templates. ~120 LOC + UI smoke tests.

7. **Commit E — Migration m067 backfill.** Re-parse every existing
   row's `locations_raw` through `parse_locations`, write the 3 new
   columns. Idempotent. Land AFTER Commits B-D have been in production
   for at least one ingestion cycle so the parser is trusted on fresh
   data first. ~120 LOC + tests.

### Audit-track follow-ups (carried unchanged from rounds 7-8)

8. **Workable widget endpoint shape verification.** After the next
   ATS scan cycle hits the 4 careers_url-tagged Workable companies,
   if all return 0 jobs, switch to `apply.workable.com/api/v3/...`
   endpoint (round-6 404'd but docs claim it's the v3 path).

9. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Tier-4 crawler.

10. **Manual company aliases UI** (round-3 deferred).

11. **Pyright `int | None` cleanup** in test_ats_scanner.py.

12. **`_make_app` helper bug** in test_scheduler.py.

13. **m063 slug-case-sensitivity edge case**, **salary single-value
    extraction**, **mid-name punctuation in company dedupe** — all
    carried.

14. **Real Jobvite scraper for tenants careers_crawler can't handle**
    (Victaulic redirects, the-institutes dead slug). Only after step
    #2 verification shows specific gaps.

## Quirks the next session should know

All round-3 through round-8 quirks still apply. Additions from round 9:

- **Location parser deps were installed via `uv pip install`, not
  `uv sync`.** The full `uv sync` failed during this session because
  the running Flask process (PID 34520 at the time) held
  `Scripts/job-cannon.exe` open for write. `uv pip install` skipped
  the editable-rebuild step and got `pycountry` / `geonamescache`
  into the venv successfully. `pyproject.toml` + `uv.lock` are
  consistent and a future `uv sync` (after Flask restart) will be
  a no-op for these two packages.

- **`pycountry.countries.get(alpha_2="UK")` returns Uganda.** The
  parser has a small alias map (`_COUNTRY_ALIASES`) to remap
  `UK`/`U.K.`/`USA`/`U.S.A.` before pycountry sees them. Anyone
  extending the parser to other ambiguous codes should add them
  there, not via `search_fuzzy` (too easy to hit a wrong match on
  3-letter strings).

- **geonamescache 2.x `alternatenames` is a `list`, NOT a comma-
  separated string.** Older versions used a string. The parser's
  `_cities_by_name` index handles both forms defensively. If
  geonamescache flips again, only that one site needs updating.

- **`geonamescache.admin1code` is ISO 3166-2 ONLY for US.** For CA
  (Canada) it's numeric ('08' = Ontario), for IN (India) it's FIPS
  ('19' = Karnataka), for GB it's a string ('ENG'). The parser
  therefore only uses `admin1code` for region matching when
  `country_code == "US"`; everywhere else, region_code is sourced
  from the EXPLICIT input segment validated against
  `pycountry.subdivisions`.

- **Region anchoring requires ≥2 remaining segments.** With only 1
  segment after country anchor, the parser treats it as a city, not
  a region. This handles Manchester (city) vs Manchester (GB
  metropolitan county), and similar cases in IN / GB / CA. If you
  add new logic to consume regions, preserve this guard or you'll
  regress `"Manchester, UK"`.

- **Trailing `/ Remote` is the workplace-promotion signal.** The
  `or Remote` separator does NOT promote — it produces two distinct
  entries. The distinction comes from `_TRAILING_SLASH_WORKPLACE_RE`,
  which only fires when the separator is a slash. If the SPEC's
  decision is ever revisited, that regex is the single change point.

- **The `_parse_one` helper is private.** External callers go through
  `parse_locations` (the public entry point). Test code reaches
  `_parse_one` only via parametrized tests in
  `test_location_parser.py` for ambiguity scenarios.

- **All round-8 quirks still apply:** `.planning/` is gitignored
  (SPEC + research files persist there); polling timeout is
  tick-based (not wallclock); jobvite excluded from `_URL_FASTPATH_PLATFORMS`;
  4 migration-count assertions to bump when adding migrations
  (now at 65, B will move them to 66).

## Suggested next step (in priority order)

1. **Restart Flask + verify m065 + heartbeat fix.** 5-15 min session.
   Run the verification PowerShell at the top of "How to verify"
   AFTER restarting Flask. If `user_version=65` + `last_tick_at`
   present, the round-8 architectural fix is verified-applied.

2. **Watch the next 5:00 AM `careers_crawl` for jobvite tenants.**
   No Claude action needed — just a SQL check after the cron.

3. **Commit B (m066)** — easiest next location parsing chunk. ~80
   LOC + ~11 tests, zero schema risk. After this lands, Commit C
   (scanner wiring + upsert_job) is unblocked and is the biggest
   value-add chunk (turns the parser from dead code into live data).

If verification (#1 + #2) and Commit B are all clean in one session,
Commit C (the scanner wiring) is the natural follow-on and probably
spans 1 session by itself.

## Open questions

- **SPEC Q1 (Springfield ambiguity)** — shipped as SPEC default
  (`city=None` when region missing + multiple matches). The SPEC
  asked to "Confirm before implementation"; I implemented the
  default. User should confirm or override before Commit E
  (backfill) goes live — once the backfill runs, historic data
  follows the chosen behavior.

- **SPEC Q3 (JD body keyword fallback)** — `parse_locations` does
  NOT yet accept a `jd_full` parameter. The internal token regex
  matches `#LI-Remote` etc., so if a caller passes JD text
  concatenated with location, it would detect. But there's no
  formal API for it yet. Should this go into Commit C (scanner
  integration), Commit D (read-side), or its own micro-commit?

- **`uv sync` editable-rebuild conflict with running Flask.** Long-
  term, when adding deps to pyproject during active Flask sessions,
  is `uv pip install <pkg>` the canonical workaround, or should we
  document a "stop Flask before deps change" rule? Round 9 used the
  workaround successfully.

- **Round-8 carry: does the-institutes slug need manual cleanup?**
  Their careers_url 302s to `?invalid=1`. Data issue, not code.
  Flag for cleanup in the companies UI when verification (#2) lands.
