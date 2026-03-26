---
phase: 16-homepage-discovery
plan: 02
subsystem: infra
tags: [apscheduler, scheduler, homepage-discovery, cron]

# Dependency graph
requires:
  - phase: 16-01
    provides: discover_homepages_batch function with (db_path, config) signature
provides:
  - homepage_discovery APScheduler cron job registered in scheduler.py, runs daily at 06:30
affects: [scheduler, homepage-discoverer]

# Tech tracking
tech-stack:
  added: []
  patterns: [_make_simple_job factory pattern for new scheduler jobs]

key-files:
  created: []
  modified:
    - job_finder/web/scheduler.py

key-decisions:
  - "Used _make_simple_job (not _make_tracked_job) — homepage discovery has no activity_tracker action constant"
  - "No day_of_week restriction — runs daily unlike ATS jobs (Mon/Wed only)"
  - "No guard function — homepage discovery should always run"

patterns-established:
  - "New scheduler jobs follow: def _import_X(): ... + scheduler.add_job(_make_simple_job(...), CronTrigger(...))"

requirements-completed: [DISC-04]

# Metrics
duration: 3min
completed: 2026-03-26
---

# Phase 16 Plan 02: Scheduler Registration Summary

**Daily 06:30 APScheduler cron job for homepage_discovery registered in scheduler.py using _make_simple_job factory with lazy import pattern**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-26T03:04:14Z
- **Completed:** 2026-03-26T03:07:00Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments
- homepage_discovery_job registered as a daily CronTrigger at 06:30 in scheduler.py
- Follows _make_simple_job factory pattern (same as ats_slug_probe and drive_feedback_poll)
- Lazy import of discover_homepages_batch via _import_homepage_discovery function
- max_instances=1 and coalesce=True prevent concurrent or stacked runs
- All 1429 tests pass (15 scheduler tests + full suite)

## Task Commits

Each task was committed atomically:

1. **Task 1: Register homepage_discovery_job in scheduler.py** - `aaeb699` (feat)

## Files Created/Modified
- `job_finder/web/scheduler.py` - Added homepage_discovery job block after expiry_check, before scheduler.start()

## Decisions Made
- Used `_make_simple_job` instead of `_make_tracked_job` because homepage_discoverer has no corresponding `activity_tracker` ACTION constant. Simple logging is sufficient.
- No `guard` function added — homepage discovery should always attempt to run (no config flag to disable it).
- No `day_of_week` restriction — unlike ATS jobs that run Mon/Wed, discovery benefits from daily cadence.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Homepage discovery scheduler job is now wired up and will run automatically at 06:30 daily
- Phase 16 complete — all plans executed (16-01: homepage_discoverer.py refactor, 16-02: scheduler registration)
- No blockers for subsequent phases

---
*Phase: 16-homepage-discovery*
*Completed: 2026-03-26*
