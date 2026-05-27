# FOLLOWUPS — 2026-05-27 round 4 (Op #5 closed; handoff-runbook drift documented)

## Project goal (briefly restated)

Job Cannon is a single-user, local-only Flask command center for job
search. The previous three rounds in 2026-05-27 closed the bulk of the
recent user bug list and operational deferred items. This round is a
short continuation session: pick up the prior session's #1 suggested
next step (operational item #5 — live USAJobs/Adzuna/Jooble ingestion
run + monitor) and surface the runbook discrepancies discovered while
executing it.

## What this session did (no code commits)

This was an observation/verification session — no code changed and no
new commits landed.

1. **Verified the prior handoff.** All 7 commits land as advertised
   (48943f1…82b1b02), tree clean, +28 ahead of origin. Ran the
   handoff-specified test suite — all 547 tests green. Live DB:
   `user_version=63`, `companies=3608` (was 3691), cigna workday cluster
   collapsed to 1 row. Helpers `normalize_for_display`,
   `_find_running_scan_session`, the `progress_callback` parameter on
   `run_ats_scan`, the `HX-Trigger-After-Settle` collapse contract — all
   present in code. Orphan template `_scan_result.html` is gone.

2. **Executed operational #5 (USAJobs / Adzuna / Jooble ingestion
   run + monitor).** Triggered `/admin/jobs/ingestion_poll/run-now` at
   2026-05-27 12:51:58 PT against the already-running dev server (PID
   34520, port 5000). The ingestion phase completed in ~78 seconds.
   Per-portal job counts from `logs/app.log` (12:52:37 – 12:53:16 PT):

   | Portal              | Jobs matched |
   |---------------------|--------------|
   | RemoteOK            | 0            |
   | Remotive            | 0            |
   | Himalayas           | 20           |
   | Jobicy              | 1            |
   | YC workatastartup   | 30           |
   | **USAJobs**         | **58**       |
   | **Adzuna**          | **123**      |
   | **Jooble**          | **293**      |
   | Title-gate          | 525 → 364    |

   No `Portal search failed` / `_inject_portal_search_creds` errors.
   Comparing against historic log entries (00:00, 08:00, 08:39 PT —
   all earlier today) — **USAJobs/Adzuna/Jooble were missing from
   every prior ingestion**. The credentials must have landed in the
   keyring between 08:39 PT and 12:51 PT today; this trigger was the
   first proof-of-life for all three.

   Scoring on the 279 net-new jobs started after ingestion completed
   (12:53 PT), tracking healthy: every job in the post-trigger window
   routes provider=`ollama` with zero cascade fall-throughs to
   claude_code_cli / anthropic. Scoring will continue in the background
   on the live dev server for ~60 min after the user reads this — that
   is normal and not a concern.

## How to verify

```powershell
# Confirm scoring run from this session is complete (~13:53 PT)
uv run --active python -c "
import sqlite3
c = sqlite3.connect('jobs.db')
# Should see one row > 4209 with action='scheduled_sync' status=success
for r in c.execute('SELECT id, occurred_at, action, metadata FROM user_activity WHERE id > 4209 ORDER BY id').fetchall():
    print(r)
print('max_runs_id:', c.execute('SELECT MAX(id) FROM runs').fetchone()[0])
print('runs from this trigger:', list(c.execute('SELECT * FROM runs WHERE id > 7969').fetchall()))
"

# Find the per-portal log lines (will persist in logs/app.log)
Select-String -Path logs/app.log -Pattern "USAJobs:|Adzuna:|Jooble:|Portal search title-gate" | Select-Object -Last 8
```

## Handoff-runbook drift discovered (THE main finding of this session)

The prior FOLLOWUPS.md (round 3, 48943f1) gave a runbook for
operational #5 that has three concrete bugs. Future sessions trying
to follow it will hit dead ends:

1. **No per-provider scheduler job IDs.** Runbook said
   `POST /admin/jobs/usajobs_ingest/run-now` (and adzuna/jooble
   variants). `scheduler/_jobs.py:88-95` registers a SINGLE ingestion
   job with id=`ingestion_poll`; USAJobs/Adzuna/Jooble are bundled
   together inside that job's `_fetch_portal_search` step. There is no
   way to trigger just one provider via the admin endpoint. Calling
   the bogus IDs returns 404 (`{"error": "no such job: usajobs_ingest"}`).

   *Correct invocation:* `POST /admin/jobs/ingestion_poll/run-now` —
   runs the whole pipeline.

2. **portal_search providers do NOT log to the `runs` table.**
   Runbook said "scheduler/_runners.py records each provider's result
   row in `runs`." False — `pipeline_runner.py:172-196` only inserts
   runs rows for gmail / imap / serpapi / dataforseo. portal_search
   results land in the `summary` dict, never in `runs`. So tailing
   `SELECT * FROM runs WHERE source='usajobs'` will always be empty,
   no matter how many jobs USAJobs returned.

3. **`scheduled_sync` user_activity metadata is missing the
   `portal_*_fetched` keys.** `scheduler/_jobs.py:63-75` (the
   activity-log call inside `run_pipeline`) writes only
   `jobs_new`, `gmail_fetched`, `serpapi_fetched`, `thordata_fetched`,
   `dataforseo_fetched`, `duration_seconds`, `status` — NO portal
   counts. The dashboard's Recent Activity therefore can't tell the
   user whether USAJobs/Adzuna/Jooble fired. (The manual "Sync Now"
   UI path emits `action='sync'` with the full per-portal breakdown —
   see `_sync.py` — but the scheduled+admin-triggered path emits
   `action='scheduled_sync'` with the slimmed-down schema.)

   The only reliable monitoring surface for portal_search is
   `logs/app.log`. That's how this session verified the three
   providers were firing.

   *This is a real observability gap* and a small focused fix (~5
   lines, add the portal keys to the metadata dict). Deliberately
   NOT applied this session: scope was "run + monitor," not "fix
   observability." Listed under "What's deferred" so the user can
   decide whether to take it on next session.

## What I tried / considered but didn't do

- **Apply the scheduled_sync metadata fix immediately.** Tempting —
  would have closed observability gap #3 above in five lines. Did
  NOT: the prior session deliberately defined "operational #5" as a
  run+monitor task, not a code change. A surprise scope expansion
  in a continuation session is the exact pattern the session-intro
  rules warned against. Leaving for the user to greenlight.

- **Trigger a single provider in isolation.** Not possible without
  code changes — `_fetch_portal_search` runs all enabled portals as
  one shot inside `fetch_all_portals`. Could be added (a
  `?providers=usajobs,adzuna,jooble` query-string filter on the
  run-now endpoint, or a separate admin route), but again — scope.

- **Wait for the scheduled_sync activity row to land before writing
  this handoff.** Scoring on 279 new jobs at ~14s/job ≈ 65 min; would
  have padded session time pointlessly because the validation
  question (did the three target providers fire?) was already
  answered from logs by 12:53 PT. Verification command above lets
  the user confirm the activity row's `jobs_new` count when scoring
  finishes.

## What's deferred / remaining (carried forward from round 3)

### Operational

- **Operational #6: audit 30 random no-ATS companies + big-name
  failures.** Unchanged from round 3 — see prior handoff's runbook.
  Now that #5 is closed, #6 is the next operational priority.
  Expected 1–2 hours of investigation; outputs are bug reports for
  individual companies + a prioritized list of new ATS platforms
  to add.

### Code

- **NEW: Add portal_* counts to scheduled_sync user_activity
  metadata.** ~5-line fix in `scheduler/_jobs.py` lines 63-75. The
  fix is: copy the same per-portal Counter logic that
  `_fetch_portal_search` line 488-492 produces — those keys already
  live in `summary` — into the `metadata=` dict. After the fix the
  dashboard Recent Activity will surface USAJobs/Adzuna/Jooble
  counts, making future monitor passes drop the need to grep logs.
  Low-risk, high-leverage observability win.

- **Manual company aliases UI** for cases m063 can't resolve. ~2
  hours. Carried forward unchanged from round 3.

- **Pyright `int | None` cleanup** in test_ats_scanner.py. ~5 min.
  Carried forward unchanged.

- **m063 slug-case-sensitivity edge case.** Carried forward
  unchanged — "Flock%20Safety" vs "flock-safety" still won't merge.
  Probably correct as-is, but flag if duplicates persist after m063.

- **Salary single-value extraction** ("$120K base" / "Up to $150K"
  unhandled). Carried forward unchanged.

- **Mid-name punctuation in company dedupe** ("Goldman Sachs & Co"
  vs "Goldman Sachs"). Carried forward unchanged.

## Quirks the next session should know

All quirks from round 3 still apply (migration count is now 63,
`error_msg` dual-purpose for ats_scan sessions, m063 pass order,
`normalize_for_display` is read-side only, collapse-row HX-Trigger
contract, transient-connection per-tick scan progress writes,
Pyright lag). Adding two:

- **`logs/app.log` timestamps are in LOCAL system time (PDT here),
  but DB timestamp columns use `datetime.now().isoformat()`** which
  is also naive local time — so they're directly comparable. (I
  initially read DB times as UTC due to confused mental model; the
  runs row at `2026-05-27T19:53:17` was the dev server's UTC clock
  drift — actually no, on second look the DB writes naive local; the
  19:53 vs 12:53 PT mismatch was actually the row landing in UTC
  because the dev server's `datetime.now()` happens to return UTC
  on this Windows machine for whatever Python config reason. Worth
  knowing: **DB timestamp shapes don't necessarily match the local
  log timestamp shape on this machine.** Comparing them requires
  treating DB times as UTC-naive and adding -7 hours.)

- **The scheduled_sync metadata schema is narrower than the sync
  metadata schema.** Manual "Sync Now" (action=`sync`) emits the
  full per-portal breakdown. The admin run-now-trigger path and the
  3x/day cron schedule (both action=`scheduled_sync`) emit only the
  jobs_new + 4 paid-provider counts. See drift item #3 above. If
  you ever need to know "did portal X fire from a scheduled run?",
  the answer is currently `logs/app.log` only.

## Suggested next step (in priority order)

1. **Apply the scheduled_sync metadata fix (drift #3 above).**
   ~5 minutes. Closes a real observability gap that's
   silently blocking the user from seeing whether the
   USAJobs/Adzuna/Jooble ingestion paths are firing on the cron
   cadence. Lowest-risk highest-leverage thing on the board.

2. **Operational #6 (audit no-ATS companies + big names).** 1–2
   hours of investigation; produces bug reports + a prioritized
   list of new ATS platforms. Worth doing while the user is
   actively driving job-search momentum.

3. **Manual company aliases UI.** ~2 hours, code-only. Only worth
   doing after the user signals it's still a meaningful problem.

4. **Pyright `int | None` cleanup.** ~5 min. Low-leverage; do if
   spare context.

## Open questions

- Should the next session also add a `provider=` query-string filter
  to the run-now endpoint so individual ingestion providers CAN be
  triggered in isolation? Useful for future debug + smoke tests,
  but adds API surface area for a single-user app — probably not
  worth it. Default: skip.

- Is the user fine relying on `logs/app.log` for portal-level
  observability, or do they want the scheduled_sync metadata fix
  applied? (See drift #3 + "Suggested next step #1".)
