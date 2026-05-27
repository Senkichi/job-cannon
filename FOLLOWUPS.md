# FOLLOWUPS — 2026-05-27 round 7 (B1–B4 + 4 new ATS platforms shipped)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 6 corrected a stale audit and surfaced the real
remaining work. Round 7 (this session) shipped all 7 of the user-
approved items from that corrected list in 5 commits:
B1a (cohort gate) + B1b (FP reset migration) + B2 (URL fast-path) +
B4 (categorical miss_reason) + 4 new ATS platforms (Workable, Jobvite,
Paylocity, Rippling). Two user-reported bugs (ATS scan 30-min timeout,
location parsing architectural overhaul) remain explicitly deferred
per round-6 user instruction.

## What this session shipped

Commits, in order (newest first):

1. `f50c98b` — `feat(ats): add Workable / Jobvite / Paylocity /
   Rippling platforms (items 5-8)`. 16 ATS platforms now registered.
   Workable / Paylocity / Rippling: real scanners with public JSON
   APIs. Jobvite: stub returning [] (no public unauthenticated API;
   probe-only). +4 URL detection patterns. +4 probes. +31 tests.
   ATS_EXTRACTOR_VERSION: m049-v3 → m049-v4.

2. `4f00ba8` — `feat(ats_probe): categorical miss_reason on probe
   failures (B4)`. Speculative probe writes `speculative_exhausted`
   or `speculative_rejected`; explicit-platform probe writes
   `platform_slug_404` / `platform_slug_blocked`; reconcile/blocked
   paths unchanged. Future audits become meaningfully cheaper.
   Legacy 2563 NULL rows NOT backfilled (no signal to backfill with).

3. `c514e7f` — `feat(ats_probe): careers_url hostname fast-path for
   unambiguous ATS URLs (B2)`. Step 0 of `probe_ats_slugs`:
   extract_ats_from_url_best on careers_url → if known platform AND
   live probe verifies → write hit with
   `ats_evidence_trigger='careers_url:...'`. Runs BEFORE brand
   blocklist (URL evidence > collision risk). Allows FP-prone
   platform assignment when URL evidence is unambiguous (overrides
   B1a's speculative-ladder exclusion). +6 tests.

4. `7dff596` — `feat(migrations): m064 reset speculative-probe FPs
   for FP-prone platforms (B1b)`. Reset migration: NULL
   platform/slug + status='pending' for 40 rows matching `hit AND
   platform IN (bamboohr/personio/recruitee/breezy) AND
   ats_evidence_trigger IS NULL`. Live-DB: 40 → 0 (pending 233 →
   273). +11 m064 tests. Bumped len(MIGRATIONS) assertions to 64.

5. `c683125` — `feat(ats_probe): exclude FP-prone platforms from
   speculative ladder (B1a)`. Removed bamboohr/personio/recruitee/
   breezy from `_PROBES` in `_probe.py`. Module-level assert locks
   the invariant. `_verify_live` in `ats_identity_reconcile.py`
   extended to cover all 7 stage-4 platforms so the evidence-based
   reconcile path can still promote them. +4 tests.

(0. `a05bdc4` — round-6 FOLLOWUPS correction + audit v2 rewrite.
   Listed for context; not new code this session.)

Full test impact: **+57 new tests** across 4 test files
(test_speculative_probe_consistency.py +13, test_round6_ats_scanners.py
+31, test_migration_064_*.py +11, test_stage4_ats_scanners.py +1 +
modified). All ATS/probe/migration suites pass: 510 tests green.

## How to verify (this session's work)

```powershell
# All B1–B4 + platforms commits' tests:
uv run --active pytest `
  tests/test_speculative_probe_consistency.py `
  tests/test_round6_ats_scanners.py `
  tests/test_migration_064_reset_fp_prone_speculative_hits.py `
  tests/test_migration_invariants.py `
  tests/test_stage4_ats_scanners.py `
  -v

# B1b migration applied to live DB:
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
print('user_version =', c.execute('PRAGMA user_version').fetchone()[0])
fp = c.execute('''
SELECT COUNT(*) FROM companies
WHERE ats_probe_status=\"hit\"
  AND ats_platform IN (\"bamboohr\",\"personio\",\"recruitee\",\"breezy\")
  AND ats_evidence_trigger IS NULL
''').fetchone()[0]
print(f'B1 FP cohort remaining: {fp} (expected 0)')
pending = c.execute('SELECT COUNT(*) FROM companies WHERE ats_probe_status=\"pending\"').fetchone()[0]
print(f'Pending: {pending}')
"
# Expected: user_version=64, B1 FP cohort=0, pending around 273

# 16 platforms registered:
uv run --active python -c "
from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS
print('Platforms:', len(_PLATFORM_SCANNERS))
for p in sorted(_PLATFORM_SCANNERS.keys()):
    print(f'  - {p}')
"
# Expected: 16 platforms incl. workable, jobvite, paylocity, rippling
```

The next ingestion + probe cycle will:
- Re-probe the 40 reset-by-m064 rows (FAANG cohort) — with the
  FP-prone-excluded ladder, almost all should land as miss with
  `speculative_exhausted` reason. The 3 Microsoft/Amazon/Meta rows
  in particular will not be re-tagged with bamboohr/personio/recruitee.
- Tag the 7 jobvite + 4 workable + 4 paylocity + 3 rippling
  careers_url companies via the B2 fast-path with
  `ats_evidence_trigger='careers_url:...'`.
- Write categorical `miss_reason` on every new miss.

## What I tried / considered but didn't do

- **Backfill `miss_reason` for the 2563 legacy NULL miss rows.** We
  don't know what specifically failed for them at the time the
  speculative probe ran — assigning `'speculative_exhausted'` to all
  would be a fiction. Better: let them stay NULL until the company is
  re-probed (which the m064 reset triggers for the 40 FP rows; manual
  retry handles the rest).

- **Build a real Jobvite scraper.** Jobvite hosted career sites have
  no public unauthenticated JSON API and frequently redirect to
  tenant-custom domains (e.g. `jobs.jobvite.com/victaulic` redirects
  to `careers.victaulic.com`). A real scraper needs per-tenant HTML
  parsing — too speculative to ship without sample testing across
  the 7 known tenants. Shipped a stub scanner (returns []) so the
  platform is registered for URL-evidence promotion via B2; the real
  scraper is a separate session.

- **Fetch per-job descriptions for Rippling.** Rippling's list endpoint
  omits descriptions; fetching them requires N additional HTTP calls
  per scan. Matched the existing Recruitee pattern (description=""
  in scanner output, enrichment_tier pipeline fills jd_full
  asynchronously) instead of doubling the scan time.

- **Investigate or fix the ATS-scan 30-min timeout.** Deferred per
  round-6 user instruction ("address ats scan after all other follow
  ups"). All other followups are done as of this session; this is the
  next thing.

- **Brainstorm location parsing.** Same — deferred per user
  instruction ("then brainstorm the location parsing solution and
  spec out the solution for a follow up session"). Should be its
  own session because the user explicitly asked for research into
  established patterns before any code.

- **Apply Pyright `int | None` cleanup in test_ats_scanner.py.**
  Still on the carry-forward list; not relevant to this session's
  scope.

- **Apply the `_make_app` helper fix in test_scheduler.py.** Still
  carried forward.

## What's deferred / remaining

### NEW priority (next session): user bug 1

1. **ATS scan session timeout (>30 min).** User question: "why do we
   have a timeout on this?" Investigation should cover:
   - Is the timeout a Flask session-cookie age (e.g. `SESSION_COOKIE_AGE`
     or `PERMANENT_SESSION_LIFETIME`)? Check `web/__init__.py`.
   - Is it APScheduler's `misfire_grace_time` or `max_instances`?
   - Is it a per-company `_PROBE_TIMEOUT` (8s) accumulating across
     hundreds of companies in `run_ats_scan`?
   - Is it the WSGI worker timeout (Flask dev server has no built-in
     request timeout; gunicorn would, but this app uses dev server)?
   - Or is it the user's BROWSER session timing out the long-polled
     UI page, not the server-side scan itself?
   
   Fix direction depends on diagnosis. If browser-side: stream
   progress events so the UI keeps the connection alive. If
   server-side: investigate whether the scan should be cancellable
   or background-only (it already runs in APScheduler, so likely
   the issue is the UI session, not the scan).

### NEW priority (separate session): user bug 2

2. **Location parsing architectural overhaul.** User asked for
   "architecturally robust way to parse location" with research into
   "existing, well-established code patterns." This is a brainstorm +
   spec session, NOT inline code. Established patterns to research:
   - **libpostal** (street-address parser, Python via pypostal).
   - **geonames** / **geopy** (place-name normalization, lat/long lookup).
   - **CLDR locale data** (city/region canonical names by territory).
   - **Greenhouse / Ashby / Workday native location shapes** — most
     have a structured `{city, region, country, remote_type}` model
     we could canonicalize to.
   - Industry "Remote / Hybrid / Onsite" flag conventions (LinkedIn
     uses `workplaceType ∈ {REMOTE, HYBRID, ONSITE}` — Rippling
     already emits this; we should adopt it as the canonical shape).
   
   Spec deliverable: a normalization schema + remote/hybrid/onsite
   flag + multi-city handling rules + city-name canonicalization
   (so all "San Francisco" / "SF" / "San Francisco, CA" / "SFO"
   variants collapse to one canonical form).

### Audit-track follow-ups

3. **Real Jobvite scraper** (replace the stub). Per-tenant HTML
   parsing with redirect handling. 7 known companies (Victaulic,
   Capcom, ASH, The Institutes, Havas, PulsePoint, NeoGenomics).
   `_platforms_jobvite.py` module docstring documents the deferral.

4. **Workable widget endpoint shape verification.** Datadog and
   Canonical both returned empty `jobs: []` from the widget endpoint
   even though both have many active jobs publicly. The shipped
   scanner uses the documented widget endpoint; if real Workable
   tenants in our DB also return empty, switch to a different
   endpoint (maybe `apply.workable.com/api/v3/accounts/{slug}/jobs`
   — 404'd in my exploration but the docs claim it's the v3 path).
   Verify on the 4 careers_url-tagged companies after they get
   probed.

5. **AI-nav recipes for in-house custom ATS** (Apple, Tesla, Oracle
   Recruiting Cloud, AMD, NVIDIA, ByteDance, Deloitte, Genentech,
   Citi, Kaiser Permanente). Each is a per-company AI-nav recipe
   (Tier-4 crawler). Lower priority than the platform scanners
   because each one only unlocks 1 company.

### Code (carried forward unchanged from rounds 4-6)

6. **Manual company aliases UI** (round-3 deferred). m063 can merge
   by shared job board but not by name alone; salesforce/nvidia/
   amazon duplicate cohorts need manual aliasing.

7. **Pyright `int | None` cleanup** in test_ats_scanner.py. ~5 min.

8. **`_make_app` helper bug** in test_scheduler.py (the
   `app.config.get = lambda` assignment on a real dict). Dormant.

9. **m063 slug-case-sensitivity edge case**, **salary single-value
   extraction**, **mid-name punctuation in company dedupe** — all
   carried forward.

## Quirks the next session should know

All round-3 + round-4 + round-5 + round-6 quirks still apply. Adding:

- **The ATS probe has THREE provenance classes for `hit` rows.**
  Distinguishable by `ats_evidence_trigger`:
  - `'careers_url:...'`           → B2 fast-path (this session)
  - `'scheduled_promote'` / etc.   → reconcile_company_ats path
  - NULL                          → legacy speculative-probe hit
    (no longer creatable for FP-prone platforms after B1a)
  Cohort filters (e.g. B1's NULL evidence + FP platforms) rely on
  this distinction.

- **`_FP_PRONE_PLATFORMS = {bamboohr, personio, recruitee, breezy}`
  is a load-bearing invariant.** Defined in
  `job_finder/web/ats_scanner/_probe.py:45`. The module-level
  `assert` (line ~75) guarantees these are never re-added to
  `_PROBES`. If you ever want to put one back, you must ALSO remove
  it from `_FP_PRONE_PLATFORMS` AND remove the test
  `TestSpeculativeProbeFpExclusion.test_fp_prone_set_is_disjoint_from_probes_ladder`.

- **`_URL_FASTPATH_PLATFORMS` is the set of platforms B2 can
  promote via URL evidence.** Includes all 12 stage-4 platforms PLUS
  the 4 round-6 additions = 16 total. Adding a new platform requires
  updates in 5+ places (see _platforms_X.py modules for the
  checklist). Tests in `TestDispatcherWiring` (in
  test_round6_ats_scanners.py) assert all 4 round-6 platforms are
  consistently wired.

- **Paylocity's "slug" is a GUID, not a name.** The URL regex
  extracts it from the path (e.g.
  `b181f77f-0432-453f-b229-869d786bb46c` from
  `/recruiting/jobs/All/{guid}/...`). Don't confuse with the
  subdomain-slug pattern used by other platforms. `companies.ats_slug`
  for Paylocity rows will look like a GUID; that's correct.

- **Jobvite scanner is a stub.** Calls to `scan_jobvite` always
  return [] — no jobs come in. This is by design (no public API).
  Companies tagged with `ats_platform='jobvite'` still get
  `last_scanned_at` updated and `jobs_found_total += 0` (no error).
  Do NOT try to enable the scanner without building a real per-tenant
  HTML scraper first.

- **`_make_app` helper bug in test_scheduler.py is STILL not fixed.**
  Round 5 noted it as dormant. Still dormant. If you write a test
  that calls `_make_app(testing=False)`, it will crash on
  `app.config.get = lambda` line. Use `_minimal_app` in those tests
  (round 5's workaround pattern).

## Suggested next step (in priority order)

User has previously indicated: items 1-7 first, then ATS scan
timeout, then brainstorm location parsing. All of items 1-7 are
done. The two remaining user-bugs are next.

1. **ATS scan 30-min timeout investigation.** Diagnostic-first: read
   `web/__init__.py` for session config + `scheduler/_jobs.py` for
   scan job config + `blueprints/companies.py` for the UI route that
   triggers ATS scans. The fix depends entirely on diagnosis.

2. **Location parsing brainstorm + spec.** Separate session.
   Deliverable should be a SPEC.md describing the canonical location
   schema, the canonicalization rules, the remote/hybrid/onsite
   flag, and the migration plan from current freeform strings.

If you want to ship more code instead of moving to the user bugs,
the next-highest-value followup is **#3 (real Jobvite scraper)** —
unlocks 7 companies that are currently scanner-stubbed.

## Open questions

- **B1a cohort exclusion is permanent for now.** If a future legitimate
  bamboohr/personio/recruitee/breezy tenant lands in the DB with a
  careers_url that B2 fast-path verifies, the platform gets assigned
  via the URL path. But if the careers_url is missing AND the company
  is legitimately on that platform, the speculative ladder won't find
  them. That's an acceptable trade-off given the 100% historical FP
  rate, but worth revisiting if real ground-truth bamboohr/etc.
  companies turn up.

- **Workable widget endpoint reliability.** Both test fetches
  (Datadog + Canonical) returned empty `jobs: []` even though those
  companies have active careers. Either (a) those companies don't
  use Workable for public careers, (b) the widget endpoint has tenant
  opt-in we don't know about, or (c) the wrong endpoint. Watch the
  first real ingestion+scan cycle that hits the 4 tagged Workable
  companies. If they all return 0 jobs, see followup #4.

## User Bug List (no longer carried — see deferred items 1-2 above)

The two user bugs (ATS scan timeout + location parsing) are now
formally tracked as the next-priority items, not buried at the
bottom of the doc.
