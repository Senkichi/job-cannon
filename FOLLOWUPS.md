# FOLLOWUPS — 2026-05-27 round 6 (audit corrected; Pinpoint was already done)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. Round 5 (prior session) shipped two scheduler/ingestion drift
fixes and produced an ATS coverage audit recommending "build Pinpoint
scanner first." Round 6 (this session) **verified that Pinpoint and
Breezy were already fully shipped** in the F1 polish-review refactor
of 2026-05-26 — the day before round 5's audit was written. The audit
was based on SQL queries against a correct DB but never grepped the
codebase to check scanner-file existence. Round 6 rewrote the audit
as v2 with corrected scanner-status table, refreshed B1 cohort count
(41 rows, not 5–10), and corrected execution-order priorities.

## What this session shipped

**No code commits.** This session was an audit-refresh + planning
session. The deliverable is the corrected audit doc.

Files changed:
1. `.planning/ATS-COVERAGE-AUDIT-2026-05-27.md` — rewritten as v2.
   Added v1→v2 changelog at the bottom for audit-trail honesty.
   File is gitignored (`.planning/*` is in `.gitignore`), so no
   commit; this is consistent with how the prior audit was tracked.
2. `FOLLOWUPS.md` — this file.

## How to verify (this session's work)

```powershell
# Verify Pinpoint scanner actually exists and is dispatched:
Get-Content job_finder/web/ats_platforms_internal/_platforms_pinpoint.py | Select-String -Pattern "SCANNER"
# Expected: line 69, SCANNER = PlatformScanner(name="pinpoint", ...)

# Verify Pinpoint is in the dispatcher:
Select-String -Path job_finder/web/ats_scanner/_run.py -Pattern "PINPOINT_SCANNER"
# Expected: 2 hits (import line + dict entry line ~69)

# Verify Pinpoint companies are being scanned today:
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
rows = c.execute('''
SELECT id, name_raw, last_scanned_at, jobs_found_total
FROM companies WHERE ats_platform=\"pinpoint\"
  AND last_scanned_at > \"2026-05-27\"
ORDER BY last_scanned_at DESC LIMIT 5
''').fetchall()
for r in rows: print(r)
"
# Expected: 5 rows with today's date, jobs_found_total > 0

# Verify B1 FAANG FP cohort count (now 41, not 5-10):
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
print(c.execute('''
SELECT COUNT(*) FROM companies
WHERE jobs_found_total > 0
  AND ats_evidence_trigger IS NULL
  AND ats_platform IN (\"bamboohr\",\"personio\",\"recruitee\",\"breezy\")
''').fetchone())
"
# Expected: (41,)

# Round-5 tests still pass (unchanged):
uv run --active pytest `
  tests/test_ingestion.py::TestPerPortalBreakdown::test_portal_search_logged_in_runs_table `
  tests/test_scheduler.py::TestScheduledSyncPortalMetadata -v
```

## What I tried / considered but didn't do

- **Pivot straight to Jobvite (the audit's true #1) without surfacing
  the audit error.** User explicitly approved "Pinpoint first" in the
  round-5 FOLLOWUPS edit. Silently switching targets would have
  inherited the prior session's blind spot. Stopped and surfaced
  the divergence first; user chose "fix audit, then re-decide."

- **Apply the B1 reset migration this session.** Tempted — user has
  already approved it. But the cohort turned out to be 41 rows, not
  5-10. That's an 8x bigger blast radius than approved. And there's a
  prerequisite: without the corroboration gate landing first, the next
  probe cycle will recreate the FPs. Both decisions deserve user
  reconsideration after seeing the corrected numbers.

- **Add a Jobvite/Workable/Paylocity/Rippling scanner stub.** Out of
  scope per the user's "fix audit, then re-decide" answer.

- **Investigate the ATS-scan 30-minute timeout user bug.** Deferred
  per user instruction ("address ats scan after all other follow ups").

- **Brainstorm location parsing.** Deferred per user instruction
  ("then brainstorm the location parsing solution and spec out the
  solution for a follow up session"). Should be its own session —
  user explicitly asked for established patterns / well-trodden
  prior art, which warrants research + spec before any code.

## What's deferred / remaining

### Newly elevated (audit v2 corrected priorities)

1. **B1 reset migration + cohort-bias gate** — user approved with the
   "5-10 rows" estimate; actual is 41 rows. User should reconfirm
   before this lands. Migration must run AFTER the probe gate, or
   the next probe pass will recreate the FPs. See audit B1 for the
   exact SQL.

2. **B2 hostname-pattern fast-path** in `ats_prober.py` — adds regex
   pre-pass for `jobs.ashbyhq.com / careers.smartrecruiters.com /
   jobs.lever.co / boards.greenhouse.io` before the platform-discovery
   loop. ~50 LOC. Reclassifies 6 probe-regression rows (Ashby×3,
   SmartRecruiters×3) and prevents future regressions.

3. **B4 populate `miss_reason`** — thread categorical failure reason
   through probe → upsert. Migration column or codes table needed.
   2563 misses currently lack a reason. Pays back every future audit.

### Audit-roadmap (still genuinely missing platforms)

4. **Jobvite scanner** — 7 careers_url hits. Mirrors
   `_platforms_pinpoint.py` (76 LOC). Add to `_PLATFORM_SCANNERS`
   dict in `_run.py`.

5. **Workable scanner** — 4 careers_url hits. Public job-board API.

6. **Paylocity scanner** — 4 careers_url hits.

7. **Rippling scanner** — 3 careers_url hits.

### User-reported bugs (deferred per user instruction)

8. **ATS scan session timeout (>30 min).** User asked: "why do we
   have a timeout on this?" Address after items 1–7. Investigation
   should cover: where the timeout is set, whether it's a Flask
   session timeout (32-bit cookie age?) vs an APScheduler job timeout
   vs a per-company HTTP timeout, and whether the scan should be
   cancellable rather than time-bounded.

9. **Location parsing architectural overhaul.** User asked for an
   "architecturally robust way" with research into "existing,
   well-established code patterns." This is a brainstorm + spec
   session, NOT an inline fix. Established patterns to research:
   libpostal (street-address parser, has Python bindings via pypostal),
   geopy / geonames (place-name normalization), CLDR locale data
   (city/region canonical names), and how Greenhouse / Ashby /
   Workday themselves represent locations (most have a structured
   `{city, region, country, remote_type}` shape we could canonicalize
   to). Spec deliverable: a normalization schema +
   remote/hybrid/onsite flag + multi-city handling rules.

### Code (carried forward unchanged from round 5)

- **Manual company aliases UI** (round-3 deferred). Still relevant —
  audit B3.
- **Pyright `int | None` cleanup** in test_ats_scanner.py. ~5 min.
- **`_make_app` helper bug** in test_scheduler.py (the
  `app.config.get = lambda` assignment on a real dict). Dormant.
- **m063 slug-case-sensitivity edge case**, **salary single-value
  extraction**, **mid-name punctuation in company dedupe**.

## Quirks the next session should know

All round-3 + round-4 + round-5 quirks still apply. Adding:

- **Audit v1 was wrong; audit v2 is the source of truth.** If you
  read references to "Pinpoint scanner is missing" anywhere in
  `.planning/` or in earlier round handoffs, the actual state is:
  Pinpoint + Breezy were shipped 2026-05-26 in the F1 polish-review
  refactor (commit 62024c3) and are running daily. The v1 audit doc
  was overwritten in place; this FOLLOWUPS (round 6) supersedes
  round 5's recommendations.

- **Before claiming an ATS platform scanner is "missing," do BOTH
  checks:**
  1. `Glob job_finder/web/ats_platforms_internal/*<name>*` — does
     a `_platforms_<name>.py` file exist?
  2. `Grep "<name>_SCANNER" job_finder/web/ats_scanner/_run.py`
     — is it in the `_PLATFORM_SCANNERS` dispatcher dict?

  v1 ran live DB queries (correct numbers) but never grepped the
  codebase. That's the failure mode to avoid.

- **B1 cohort is bigger than v1 estimated.** 41 rows, not 5-10. The
  expanded list includes YouTube, Accenture, EY, Microsoft, Meta,
  Amazon, IQVIA, EY, Leidos, Scribd, KBR, Tata, Gong, Hilton,
  Under Armour, Conduent, AnswerLab, Specright… The cohort gate
  is mandatory before any reset, or the same FPs come back on the
  next probe cycle.

- **The user is paused on items 8 + 9 deliberately.** Don't pull
  the ATS-scan timeout investigation or location-parsing brainstorm
  forward without explicit user request — they specifically said
  "address ats scan AFTER all other follow ups."

## Suggested next step (in priority order)

User has already directed: "fix the audit, then re-decide." Audit is
fixed. **The natural next decision is item 1 (B1 reset migration +
cohort-bias gate)** — it was the most operationally relevant of the
v1 deferreds, and the user already approved it (subject to the
cohort-count reconfirmation noted above).

Alternative orderings worth considering:

- **B2 first** (hostname-pattern fast-path) — pure addition, no
  destructive migration, smallest blast radius. Could land alongside
  B1 if both are landing this milestone.

- **Jobvite scanner first** if user wants to demonstrate a new
  platform shipping cleanly before touching the probe layer. Zero
  risk to existing data.

## Open questions

- **B1 cohort count is 41, not 5–10. Reconfirm the reset is still
  approved at this larger scale?** Alternative: filter the destructive
  reset to a narrower cohort (e.g. only rows where the probe's
  no-evidence claim is corroborated by a follow-up `_probe_<platform>`
  returning False at audit time).

- **Order of operations for B1 + cohort gate:** the gate MUST land
  first or simultaneously. Suggested approach: ship the cohort-bias
  gate to `ats_prober.py` as commit 1 (test-covered, no data
  changes), then the migration as commit 2 (runs against the now-safe
  probe path).

- **Jobvite vs Workable as first new scanner:** Jobvite has more
  indirect hits (7 vs 4) but Workable has a cleaner public API and
  more permissive CORS. Either is fine; default recommendation =
  Jobvite for raw coverage win.

## User Bug List (carried forward, do NOT address until items 1–7 land)

- ATS scan failed: Session timed out (>30 min) — why do we have a
  timeout on this? *(round 6 expanded scope: see deferred item 8)*

- We need to take a step back and find an architecturally robust way
  to parse location. I mean, just look at all the varieties of san
  francisco. that can't be good for the scorer/parsers, either. need
  to find a way to flag remote/hybrid jobs, need a way to parse
  multi city locations better, need to find a way to parse multiple
  formattings better. this is probably a problem that has already
  been solved - what are the existing, well established code
  patterns for such a thing? *(round 6: brainstorm + spec session,
  see deferred item 9 for research starting points)*
