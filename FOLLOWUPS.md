# FOLLOWUPS — 2026-05-27 round 10 (location parsing Commit B shipped)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 9 shipped Commit A of the 5-commit location parsing SPEC
(parser + JobLocation dataclass + 52 tests). Round 10 (this session)
shipped Commit B — migration **m066** adding three nullable canonical-
location columns to `jobs`. The remaining round-8 verification items
(Flask restart for m065/m066, jobvite Playwright pickup, heartbeat
end-to-end) are still CARRIED FORWARD — they require user action this
session couldn't drive.

## What this session shipped

One commit: **`ca679e3`** — `feat(location): add migration m066 with
canonical location columns (Commit B)`. +263 LOC across 5 files.

New code:
- `job_finder/web/migrations/m066_add_locations_structured.py` (~45 LOC)
  — three ALTER TABLE statements: `locations_structured TEXT`,
  `workplace_type TEXT`, `primary_country_code TEXT`. All nullable, all
  NULL on existing rows. Picked up by auto-discovery in
  `migrations/__init__.py`; no manual registration needed.
- `tests/test_migration_066_add_locations_structured.py` (~190 LOC) —
  **11 tests, all green**. Shape (column types, no py-hook, exact SQL),
  behavior (fresh-DB column add, legacy columns untouched, NULL on
  existing rows, smoke-write/read, idempotent re-run), and registry
  invariants (MIGRATIONS len=66, max version=66, monotonic).

Invariant bumps (65 → 66):
- `tests/test_migration_invariants.py:27`  — `EXPECTED_MIGRATION_COUNT`
- `tests/test_migration.py:404`            — `assert len(MIGRATIONS) == 66`
- `tests/test_migration.py:935`            — `assert len(MIGRATIONS) == 66`
- `tests/test_migration.py:1384`           — `assert version == 66`

Drive-by fix:
- `tests/test_migration_065_add_polling_session_heartbeat.py` — renamed
  `test_user_version_after_run_is_65` → `test_user_version_after_run_is_at_least_65`
  and switched `== 65` to `>= 65`. The original was an over-specific
  assertion that broke as soon as m066 shipped. New pattern is future-
  proof: invariant is "m065 ran," not "no migration ever ships after
  m065." (Note: my own m066 test still uses `== 66` matching the local
  convention — same pattern will need bumping when m067 lands.)

## How to verify (this session's work)

```powershell
# Round-10's tests (m066-specific):
.venv/Scripts/python.exe -m pytest `
  tests/test_migration_066_add_locations_structured.py -v

# Full migration regression (m065 + m066 + invariants + suite):
.venv/Scripts/python.exe -m pytest `
  tests/test_migration_066_add_locations_structured.py `
  tests/test_migration_065_add_polling_session_heartbeat.py `
  tests/test_migration_invariants.py `
  tests/test_migration.py `
  -q --tb=short

# Round-9 + round-10 combined sweep (365 tests):
.venv/Scripts/python.exe -m pytest `
  tests/test_location_parser.py `
  tests/test_location_normalizer.py `
  tests/test_migration_066_add_locations_structured.py `
  tests/test_migration_065_add_polling_session_heartbeat.py `
  tests/test_polling_status.py `
  tests/test_round6_ats_scanners.py `
  tests/test_migration_invariants.py `
  tests/test_migration.py `
  -q --tb=line

# Pyright clean on new m066 files:
.venv/Scripts/python.exe -m pyright `
  job_finder/web/migrations/m066_add_locations_structured.py `
  tests/test_migration_066_add_locations_structured.py
# Expected: 0 errors, 0 warnings

# Auto-discovery sanity (m066 picked up by migrations/__init__.py):
.venv/Scripts/python.exe -c "
from job_finder.web.db_migrate import MIGRATIONS
print('len =', len(MIGRATIONS))
print('max =', max(m.version for m in MIGRATIONS))
m66 = [m for m in MIGRATIONS if m.version == 66][0]
print('m66.description =', m66.description)
print('m66.sql =', m66.sql)
"
```

## What I tried / considered but didn't do

- **Wait for the SPEC Q1 disambiguation confirmation.** Round 9 shipped
  the SPEC default for Springfield (`city=None` when region missing AND
  multiple gazetteer matches). The SPEC asked to "Confirm before
  implementation"; round 9 implemented the default rather than blocking.
  Still unconfirmed by user; once Commit E (backfill m067) runs, historic
  data follows the chosen behavior. Recommend explicit user confirmation
  before Commit E.

- **Restart Flask to apply m065 + m066.** Out of scope this session
  (user-driven dev process). Verified pre-state via the round-9
  PowerShell snippet: live DB still at `user_version=64`, no listener on
  port 5000 (the round-9 PID 34520 Flask process is gone, but never
  restarted). Migrations will both apply in order whenever the next
  Flask restart happens. m066 is purely additive and zero-risk on
  any populated DB.

- **Wire `upsert_job` (Commit C).** Out of scope. With the schema in
  place, Commit C is the natural next chunk — it turns the parser from
  dead code into live data. See section below.

- **Backfill (Commit E / m067) inline.** Out of scope. Per SPEC, the
  backfill ships AFTER Commits B-D have been in production for at least
  one ingestion cycle so the parser is trusted on fresh data first.

- **Run the full pytest suite.** Same blocker as round 9 — the `tests/e2e/`
  Playwright tests would collide on port 5000 if Flask were running. With
  no Flask process this session, the e2e tests would theoretically be
  runnable, but they were out of scope for verifying m066. Ran a 365-test
  targeted sweep instead — all green.

## What's deferred / remaining

### CARRY FORWARD from round 8 / round 9 (still highest priority)

1. **Restart Flask and verify m065 + m066 both apply.** Live DB still at
   `user_version=64`. After restart:
   ```powershell
   .venv/Scripts/python.exe -c "
   import sqlite3
   c = sqlite3.connect('jobs.db')
   print('user_version =', c.execute('PRAGMA user_version').fetchone()[0])
   bcols = [r[1] for r in c.execute('PRAGMA table_info(batch_score_sessions)').fetchall()]
   jcols = [r[1] for r in c.execute('PRAGMA table_info(jobs)').fetchall()]
   print('m065 last_tick_at present:', 'last_tick_at' in bcols)
   print('m066 locations_structured present:', 'locations_structured' in jcols)
   print('m066 workplace_type present:', 'workplace_type' in jcols)
   print('m066 primary_country_code present:', 'primary_country_code' in jcols)
   "
   # Expected post-restart: user_version=66 (was 64), all 4 columns present.
   ```

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
   round-8 architectural pivot worked. If all 7 return 0 jobs after
   multiple cycles, jv-job-list rendering needs per-tenant tuning
   (custom `careers_nav_recipe` or AI-nav).

3. **Verify heartbeat on a real long ATS scan.** Trigger via /companies
   UI button. With 908 hits + 303 pending, scan should take 30-60+ min.
   Verify: progress fragment does NOT flip to error past 30 min;
   `batch_score_sessions.last_tick_at` populates; on completion
   `error_msg` is NULL.

### Location parsing SPEC — next commits in order

4. **Commit C — Scanner Layer-1 wiring + `upsert_job` changes.**
   *Now unblocked by Commit B.*
   - Add `_to_canonical(item) -> list[JobLocation]` helpers next to
     `_extract_one_listing` in `_platforms_smartrecruiters.py`,
     `_platforms_ashby.py`, `_platforms_lever.py`,
     `_platforms_rippling.py`. Map vendor structured fields to
     `JobLocation` per SPEC Layer-1 table.
   - Wire `upsert_job` (in `job_finder/web/db/_jobs.py`) to accept
     `locations_structured: list[JobLocation] | None = None`. When
     `None`, derive via `parse_locations(locations_raw)` (Layer 2).
     Write the 3 new columns alongside existing `location` /
     `locations_raw` for backward compat. Per SPEC: also re-derive
     `location` / `locations_raw` from `[loc.raw for loc in locations_structured]`
     joined with `", "` so legacy reads stay coherent.
   - ~150 LOC + 4 scanner integration tests + 4-6 `upsert_job` tests.
   - **Risk:** First commit that actually writes the new columns. Worth
     a careful Phase-2 verify on `upsert_job`'s existing tests before
     and after — the function is the central write path for every
     ingestion source.

5. **Commit D — Read-side dropdowns + Jinja filter.** Country +
   workplace_type dropdowns in `blueprints/jobs.py` sourced from the
   new denormalized convenience columns. New
   `format_canonical_location` Jinja filter in `web/__init__.py`. Pill
   renderer in job detail/row templates. ~120 LOC + UI smoke tests.

6. **Commit E — Migration m067 backfill.** Re-parse every existing
   row's `locations_raw` through `parse_locations`, write the 3 new
   columns. Idempotent. Land AFTER Commits B-D have been in production
   for at least one ingestion cycle so the parser is trusted on fresh
   data first. ~120 LOC + tests. **Will need invariant bumps from 66
   → 67 at the same 4 sites m066 just bumped.**

### Audit-track follow-ups (carried unchanged from rounds 7-9)

7. **Workable widget endpoint shape verification.** After the next
   ATS scan cycle hits the 4 careers_url-tagged Workable companies,
   if all return 0 jobs, switch to `apply.workable.com/api/v3/...`
   endpoint (round-6 404'd but docs claim it's the v3 path).

8. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Tier-4 crawler.

9. **Manual company aliases UI** (round-3 deferred).

10. **Pyright `int | None` cleanup** in test_ats_scanner.py.

11. **`_make_app` helper bug** in test_scheduler.py.

12. **m063 slug-case-sensitivity edge case**, **salary single-value
    extraction**, **mid-name punctuation in company dedupe** — all
    carried.

13. **Real Jobvite scraper for tenants careers_crawler can't handle**
    (Victaulic redirects, the-institutes dead slug). Only after step #2
    verification shows specific gaps.

14. **NEW — Drive-by pyright noise.** `tests/test_migration.py` has
    ~20 pre-existing `"path" is not accessed (Pyright)` warnings on the
    older test fixtures (lines 463, 471, 479, 492, 520, 528, 540, 748,
    755, 760, 765, 771, 779, 801, 815, 823, 846, 866 + `tmp_db_path` at
    1100, `_ctx` at 1463). Not introduced this session; surfaced by the
    edits. Worth a single passing cleanup pass (rename unused params to
    `_path`) when convenient, ideally bundled with #10 + #11.

## Quirks the next session should know

All round-3 through round-9 quirks still apply. Additions from round 10:

- **Migration auto-discovery means just drop the file.** No registration
  list to edit. `migrations/__init__.py` uses `pkgutil.iter_modules` +
  filename regex `^m\d{3}_` to find every `m{NNN:03d}_*.py` file. The
  3-digit zero-pad is load-bearing (so `m066` sorts after `m010`). Source
  of truth is in `migrations/__init__.py:31-32`.

- **`jobs` has NO `applied_at` column.** Required cols for INSERT in a
  test fixture are `dedup_key, title, company, location, first_seen,
  last_seen` (PK is `dedup_key`). I assumed `applied_at` existed when
  writing the m066 tests, which is why the first run failed. Lookup
  command if you ever need to confirm:
  ```python
  c.execute("PRAGMA table_info(jobs)").fetchall()
  ```

- **`test_user_version_after_run_is_NN` is a brittle pattern.** When you
  add a new migration N+1, the test in N's file breaks. Round 10 fixed
  this for m065 (renamed + relaxed to `>= 65`). My own m066 test still
  has the brittle `== 66` form — match the project convention now, but
  consider relaxing both to `>= NN` going forward.

- **Live DB drift survives Flask shutdown.** PID 34520 (round-9 Flask)
  is gone but the DB is still at user_version=64. The migration only
  runs at Flask startup (via `create_app` → `run_migrations`), not at
  arbitrary process exit. Confirm with `Get-NetTCPConnection -LocalPort
  5000` (returns nothing if no listener — exit code 1 is expected).

- **PowerShell `Get-NetTCPConnection -LocalPort 5000 -State Listen`
  exits 1 when nothing's listening.** Wrap in `Get-NetTCPConnection
  ... -ErrorAction SilentlyContinue` + an `if ($conn) {} else {}` check
  to distinguish "no listener" from "command broken."

- **All round-9 quirks still apply:** `.planning/` is gitignored (SPEC +
  research files persist there); `pycountry.countries.get(alpha_2="UK")`
  returns Uganda (alias map in `_COUNTRY_ALIASES`); geonamescache 2.x
  `alternatenames` is a list not a string; `geonamescache.admin1code` is
  ISO 3166-2 only for US; region anchoring requires ≥2 remaining
  segments; trailing `/ Remote` promotes workplace, `or Remote` keeps
  entries distinct; `_parse_one` is private (external callers use
  `parse_locations`).

## Suggested next step (in priority order)

1. **Restart Flask + verify m065 + m066 + heartbeat fix.** 5-15 min.
   Run the verification PowerShell at the top of "How to verify" AFTER
   restarting Flask. If `user_version=66` + all 4 columns present, the
   round-8 (m065) AND round-10 (m066) schema changes are verified-applied.

2. **Watch the next 5:00 AM `careers_crawl` for jobvite tenants.** No
   Claude action needed — just a SQL check after the cron.

3. **Commit C (scanner wiring + `upsert_job`)** — the biggest value-add
   chunk in the SPEC. Turns the parser from dead code into live data.
   ~150 LOC + ~10 tests. Likely a full session by itself. Care needed
   on `upsert_job` since it's the central write path for every
   ingestion source.

If verification (#1 + #2) is clean, Commit C is the natural follow-on.

## Open questions

- **SPEC Q1 (Springfield ambiguity)** — Still unconfirmed by user;
  round 9 shipped the SPEC default (`city=None` when region missing +
  multiple matches). User should confirm or override before Commit E
  (backfill) goes live — once the backfill runs, historic data follows
  the chosen behavior.

- **SPEC Q3 (JD body keyword fallback)** — `parse_locations` does NOT
  yet accept a `jd_full` parameter. The internal token regex matches
  `#LI-Remote` etc., so if a caller passes JD text concatenated with
  location, it would detect. But there's no formal API for it yet.
  Should this go into Commit C (scanner integration), Commit D
  (read-side), or its own micro-commit?

- **`uv sync` editable-rebuild conflict with running Flask** (round 9
  carry) — Long-term, when adding deps to pyproject during active Flask
  sessions, is `uv pip install <pkg>` the canonical workaround, or
  should we document a "stop Flask before deps change" rule? No new deps
  in round 10 (m066 is schema-only).

- **Round-8 carry: does the-institutes slug need manual cleanup?**
  Their careers_url 302s to `?invalid=1`. Data issue, not code. Flag
  for cleanup in the companies UI when verification (#2) lands.
