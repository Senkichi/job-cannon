---
phase: 16-homepage-discovery
plan: "01"
subsystem: homepage-discoverer
tags: [homepage, discovery, serpapi, migration, tdd]
dependency_graph:
  requires: []
  provides: [three-tier-homepage-discovery, migration-17-probe-tracking]
  affects: [discover_homepages_batch, homepage_discoverer, db_migrate]
tech_stack:
  added: []
  patterns: [SerpAPI engine=google web search, retry-avoidance via probe timestamp]
key_files:
  created: []
  modified:
    - job_finder/web/homepage_discoverer.py
    - job_finder/web/db_migrate.py
    - tests/test_homepage_discoverer.py
    - tests/test_migration.py
decisions:
  - "_strip_company_suffixes strips trailing Inc/LLC/Corp/Co/Ltd/Group tokens (with and without dot) before slug/domain check"
  - "Tier 1 returns None immediately for multi-word names — let Tier 2 name-slug handle them"
  - "Tier 2b name-derived slug only tried when name_slug != ats_slug to avoid redundant HEAD request"
  - "_search_serpapi raises SerpAPIQuotaError on JSON error key; batch catches and short-circuits"
  - "test_migration_count_is_thirteen updated to 17 — Rule 1 fix (pre-existing count assertion)"
metrics:
  duration: "9min"
  completed: "2026-03-26"
  tasks: 2
  files: 4
---

# Phase 16 Plan 01: Homepage Discoverer Refactor Summary

**One-liner:** Three-tier homepage discovery (domain guess + slug normalization + SerpAPI engine=google) replacing broken DDG HTML search, with retry-avoidance probe tracking via Migration 17.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for three-tier discovery | b2a1b35 | tests/test_homepage_discoverer.py |
| 1 (GREEN) | Three-tier implementation + Migration 17 | 3010081 | homepage_discoverer.py, db_migrate.py, tests/test_migration.py |
| 2 | Test suite rewrite (complete via TDD in Task 1) | 3010081 | tests/test_homepage_discoverer.py |

## What Was Built

**homepage_discoverer.py** — Full refactor from broken two-tier (slug + DDG) to reliable three-tier discovery:

- **Tier 1 (`_try_domain_guess`):** Strip company suffixes (Inc, LLC, Corp, Co, Ltd, Group), check if single token remains. Try `https://{token}.com` via HEAD + parked-domain guard. Zero API cost. "Stripe Inc" → "stripe" → stripe.com.
- **Tier 2 (`_try_slug_heuristic` with name-derived fallback):** Try ats_slug first, then name-derived slug from `_name_to_slug`. "Hinge Health" → "hinge-health" → hinge-health.com. Zero API cost.
- **Tier 3 (`_search_serpapi`):** SerpAPI `engine=google` query `"{name}" homepage`. Skips `_SKIP_DOMAINS` (glassdoor, crunchbase, bloomberg, zoominfo, pitchbook, linkedin, wikipedia). Raises `SerpAPIQuotaError` on error response.

**discover_homepage signature change:** Added `api_key: str | None = None` parameter. Tier 3 skipped when `api_key=None`.

**discover_homepages_batch changes:**
- Batch query now filters `homepage_probe_attempted_at IS NULL` (retry-avoidance)
- Every company stamped with `homepage_probe_attempted_at = datetime('now')` — success, failure, or quota error
- Short-circuits entire batch on `SerpAPIQuotaError` (logs error, stamps current company, breaks)
- `_BATCH_CAP` changed from 50 to 10 (conservative SerpAPI quota)
- Removed DDG delay (`time.sleep`) — no rate limiting needed for SerpAPI

**DDG code removed:** `_search_ddg`, `_DDG_HTML_URL`, `_DDG_DELAY`, `import time`, `from bs4 import BeautifulSoup` all deleted.

**db_migrate.py:** Migration 17 appended — `ALTER TABLE companies ADD COLUMN homepage_probe_attempted_at TEXT DEFAULT NULL` + index.

**tests/test_homepage_discoverer.py:** Complete rewrite with 27 tests:
- 7 normalization helper tests (`_strip_company_suffixes`, `_name_to_slug`)
- 12 `discover_homepage` tests (Tier 1/2/3 paths, skip domains, quota error, no-api-key guard)
- 8 `discover_homepages_batch` tests (probe tracking, cap=10, quota short-circuit, api_key threading, skip existing)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed stale migration count assertion in test_migration.py**
- **Found during:** Task 1 GREEN — full suite run after implementation
- **Issue:** `test_migration_count_is_thirteen` asserted `len(MIGRATIONS) == 16`; now 17 after adding Migration 17
- **Fix:** Updated assertion to `== 17`, updated docstring to document Migration 17
- **Files modified:** tests/test_migration.py
- **Commit:** 3010081

### TDD Approach Note

Task 2 (test suite rewrite) was completed as part of Task 1 TDD cycle. The plan structure had Task 1 as `tdd="true"` and Task 2 as a separate test rewrite. Since TDD RED required writing the complete test suite first, Task 2's deliverables were produced in Task 1's RED commit (b2a1b35). Task 2 has no additional work beyond verifying all acceptance criteria pass (confirmed).

## Verification

All acceptance criteria met:
- `grep -c "_try_domain_guess" homepage_discoverer.py` → 2
- `grep -c "_search_serpapi" homepage_discoverer.py` → 2
- `grep -c "_search_ddg" homepage_discoverer.py` → 0
- `_BATCH_CAP = 10` present
- All 7 skip domains present
- `SerpAPIQuotaError` class defined and used
- `api_key: str | None = None` in `discover_homepage` signature
- `homepage_probe_attempted_at IS NULL` in batch query
- `homepage_probe_attempted_at = datetime('now')` stamped in 4 locations
- Migration 17 + index present in db_migrate.py
- 27 tests pass (≥18 required), 0 DDG references
- Full suite: 1519 passed, 0 failures

## Self-Check: PASSED

- `/c/Users/senki/repos/job-cannon/job_finder/web/homepage_discoverer.py` — FOUND
- `/c/Users/senki/repos/job-cannon/job_finder/web/db_migrate.py` — FOUND
- `/c/Users/senki/repos/job-cannon/tests/test_homepage_discoverer.py` — FOUND
- Commit b2a1b35 — FOUND (TDD RED)
- Commit 3010081 — FOUND (TDD GREEN + Migration 17)
