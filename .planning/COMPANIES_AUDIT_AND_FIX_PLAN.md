# Companies Workflow: Audit Report & Implementation Plan

**Date:** 2026-04-03
**Scope:** Full end-to-end evaluation of the Companies subsystem — blueprint, templates, database schema, ATS probing, scanning, enrichment, homepage discovery, backfill linkage, scheduler wiring, test coverage, and production data quality.

---

## Part 1: Audit Findings

### 1.1 Production Data Snapshot (2026-04-03)

| Metric | Value | Verdict |
|--------|-------|---------|
| Total companies | 1,054 | — |
| `ats_probe_status = 'pending'` | 508 (48%) | **BROKEN** — never probed |
| `ats_probe_status = 'hit'` | 214 (20%) | OK |
| `ats_probe_status = 'miss'` | 332 (31%) | See 1.2d |
| `ats_probe_status = 'error'` | 0 | **BROKEN** — retry never fires |
| Companies with `homepage_url` | 28 (2.7%) | **BROKEN** — discovery too slow |
| Companies with `company_size` or `industry` | 0 (0%) | **DEAD CODE** |
| Companies with `miss_reason` populated | 0 | **BROKEN** — field never set |
| Companies with `retry_count > 0` | 0 | **BROKEN** — retry never fires |
| Jobs with `company_id IS NULL` | 125 / 1,883 (6.6%) | **BROKEN** — backfill not scheduled |
| Companies with 0 linked jobs | 3 | Minor — orphan records |
| Last `company_scan_log` entry | 2026-03-23 (11 days ago) | **STALE** — scheduler or app downtime |
| Top ATS platform | Greenhouse (141), Ashby (53), Lever (20) | — |
| Avg `jobs_found_total` for hits | 5.1 (max 207, Airwallex) | — |
| `scan_enabled = 0` | 1 company | — |

### 1.2 Subsystem-Level Assessment

#### 1.2a Blueprint & UI Layer — Grade: B+

The 8-route blueprint (`companies.py`, 354 lines) is well-constructed:
- SQL injection prevention via `_SORT_ALLOWLIST` (line 34) before f-string interpolation into ORDER BY
- Proper `HX-Request` header check for fragment vs full-page response (line 79)
- Correct HTMX swap patterns: `hx-target`, `hx-swap="outerHTML"`, `hx-on:click="event.stopPropagation()"`
- Appropriate HTTP status codes: 400 for invalid state transitions, 404 for missing resources
- Five templates follow the `_` prefix convention for fragments

**Issues identified:**
- No pagination — all 1,054 companies loaded in one query with LEFT JOIN + GROUP BY (line 68-77)
- `search` parameter uses LIKE with `%{search}%` wildcard (line 57-58) — functional but no ESCAPE clause for literal `%` or `_` in search terms
- `ats_platform` filter allowlist (`_ATS_PLATFORM_FILTER_VALUES`, line 35) includes values but is never actually checked — the `elif` chain (lines 60-64) acts as implicit validation

#### 1.2b ATS Probing (Batch) — Grade: D

`probe_ats_slugs()` in `ats_scanner.py:216-313`:
- Runs Mon/Wed at 7:30 AM only (scheduler.py:347)
- Probes ALL pending companies sequentially with 0.5s sleep between each
- **Critical bug:** On probe failure, directly sets `ats_probe_status='miss'` (line 292) WITHOUT calling `_handle_scan_error()`. Every transient failure (429, timeout, connection error) is permanently classified as a miss. The retry state machine in `ats_prober.py` is imported but never invoked from this path.
- The individual probe functions (`_probe_lever`, `_probe_greenhouse`, `_probe_ashby`) swallow ALL exceptions and return False, making transient errors indistinguishable from permanent misses.
- With 508 pending companies × 2 slug candidates × 3 platforms × 0.5s sleep = ~30 min wall-clock time per run

#### 1.2c ATS Probing (Single Company) — Grade: B+

`probe_single_company()` in `ats_prober.py:177-318`:
- Correctly uses `_handle_scan_error()` for transient failures
- Correctly uses `_reset_retry_state()` on success
- Distinguishes `_TRANSIENT_CODES` (429, 5xx) from `_PERMANENT_MISS_CODES` (404, 410)
- Speculative probing via `derive_slug_candidates()` for companies without known platform/slug

**Issue:** Missing `conn.commit()` after line 229-232 (setting `ats_probe_status='hit'`). The success case relies on `_reset_retry_state()`'s internal `conn.commit()` at line 173 to persist both the status update and the retry reset. If `_reset_retry_state()` fails, the hit status update is also lost. Not a showstopper since both are in the same transaction, but fragile.

#### 1.2d ATS Scanning — Grade: B

`run_ats_scan()` in `ats_scanner.py:560-900+`:
- Correctly queries hit + error companies (with retry_after check)
- Correctly calls `_handle_scan_error()` for transient errors (line 756-758)
- Logs scan results to `company_scan_log`, updates `last_scanned_at` and `jobs_found_total`
- Includes HTML fallback loop for miss companies with `homepage_url`
- Homepage discovery pre-step runs before HTML fallback

**Issues:**
- `jobs_found_total` is append-only (`jobs_found_total + ?`, line 744) — never resets, so it's a historical cumulative count, not a current count. Misleading in the UI.
- Runs Mon/Wed at 7:00 AM only — companies with ATS hits are scanned at most 2x/week
- Scan log shows 0 jobs found in recent entries — no differentiation between "0 from API" vs "0 after dedup"

#### 1.2e Company Enrichment — Grade: F

`company_enricher.py` (75 lines):
- Uses `search_duckduckgo()` (DuckDuckGo Instant Answer API) to get company metadata
- Extracts `company_size` via regex (`\d[\d,]*\s+employees?`) from DDG abstract text
- Extracts `industry` via keyword matching against 5 categories
- The code's own docstring acknowledges: "DDG reliability is LOW per research (sparse company data)"
- **Production result: 0/1,054 companies enriched.** The DDG Instant Answer API almost never returns employee count data in its abstract text. This subsystem has zero production value.
- The `company_size` and `industry` columns (migration 16) are populated nowhere else in the codebase.

#### 1.2f Homepage Discovery — Grade: D+

`homepage_discoverer.py` (373 lines):
- Runs daily at 6:30 AM (scheduler.py:391)
- Processes at most `_BATCH_CAP = 10` companies per run
- Three-tier lookup: domain guess → slug heuristic → SerpAPI web search
- **Throughput problem:** 10/day against 1,026 companies needing probing = 103 days to process all. And most will fail Tiers 1-2 (domain guess only works for single-token names like "Stripe").
- **Query bug:** `run_homepage_discovery()` line 191-194 joins jobs by `WHERE company = ?` (text name), not `WHERE company_id = ?` (foreign key). If `name_raw` doesn't exactly match `jobs.company` (casing, suffixes), source_urls are missed.
- **Production result: 28/1,054 (2.7%) have homepage URLs** — discovery rate is extremely low.

#### 1.2g Company Linkage Backfill — Grade: C+

`backfill_companies.py:link_jobs_to_companies()`:
- Well-designed: fuzzy match (threshold 85) → create if no match → append to in-memory list → UPDATE jobs
- Denylist filtering for junk names (Unknown, RemoteHunter, Mercor, etc.)
- **Critical gap: NOT SCHEDULED.** `link_jobs_to_companies()` is not in `scheduler.py`. It only runs when manually invoked. Result: 125 unlinked jobs.
- DDG enrichment (`run_ddg_enrichment()`) called for new companies — but as noted in 1.2e, always returns empty.

#### 1.2h Company Creation Paths — Grade: C

Companies are created through 3 independent paths with inconsistent behavior:

| Path | Fuzzy Match? | Source |
|------|-------------|--------|
| `backfill_companies.py:link_jobs_to_companies()` | Yes (threshold 85) | Manual backfill |
| `companies.py:add()` blueprint route | No | UI button |
| `ats_scanner.py:upsert_company()` via ingestion | No | Automatic |

The UI "Add Company" button calls `upsert_company()` which normalizes but does NOT fuzzy-match. This can create duplicates that `link_jobs_to_companies()` would have caught. Example: Adding "OpenAI Inc." when "openai" already exists — `normalize_company()` strips the suffix, so this specific case is caught. But "Open AI" vs "openai" would create a duplicate (normalization doesn't merge spaces).

#### 1.2i Scheduler Wiring — Grade: C

| Job | Schedule | Frequency |
|-----|----------|-----------|
| ATS scan | Mon/Wed 7:00 AM | 2x/week |
| ATS slug probe | Mon/Wed 7:30 AM | 2x/week |
| Homepage discovery | Daily 6:30 AM | 1x/day |
| Company linkage backfill | **NOT SCHEDULED** | Never (manual only) |
| Company enrichment (DDG) | **NOT SCHEDULED** | Only during manual backfill |

The probing and scanning frequencies are too low for the data volume. 508 pending companies accumulate because new companies are created 3x/day during ingestion, but probing only runs 2x/week.

#### 1.2j Test Coverage — Grade: B-

| Component | Tests | Gaps |
|-----------|-------|------|
| Blueprint routes | 33 | No response content validation, no pagination testing |
| ATS scanner batch | 40+ | **No test for transient error handling in `probe_ats_slugs()`** |
| ATS single probe | Yes | Speculative probe path not fully tested |
| Company linkage | 30+ | No scheduler integration test |
| Company enrichment | Partial | No test for 0-result DDG scenario |
| Homepage discovery | **0 tests** | Entire module untested |
| Scheduler wiring | **0 tests** | No test that jobs are registered |
| State machine E2E | **0 tests** | No pending→hit→error→miss flow test |

---

## Part 2: Implementation Plan

### Fix 1 — `probe_ats_slugs()` Retry State Machine Integration [P0]

**Problem:** `probe_ats_slugs()` (ats_scanner.py:216-313) treats all probe failures as permanent misses. The `_handle_scan_error()` function exists but is never called from this path. The individual probe functions (`_probe_lever`, `_probe_greenhouse`, `_probe_ashby`) swallow exceptions internally and return `False`, so transient errors (429, timeout) look identical to permanent misses (404).

**Root cause:** When `probe_ats_slugs()` was written (Phase 7), the retry state machine didn't exist yet. Phase 14 added the retry infrastructure to `ats_prober.py` and wired it into `run_ats_scan()` and `probe_single_company()`, but never retrofitted `probe_ats_slugs()`.

**Fix:**

1. **Replace exception-swallowing probe functions with result-returning versions in the batch path.** The `_probe_lever_with_result()` function already exists (ats_prober.py:321) and lets `Timeout`/`ConnectionError` propagate. Create similar `_probe_greenhouse_with_result()` and `_probe_ashby_with_result()` functions.

2. **Restructure the `probe_ats_slugs()` inner loop** to:
   - Wrap each slug candidate's probe attempts in try/except
   - On `requests.exceptions.Timeout` or `requests.exceptions.ConnectionError`: call `_handle_scan_error(conn, company_id, company_name, str(e), now)` and break to next company
   - On `requests.exceptions.HTTPError` with status in `_TRANSIENT_CODES`: same as above
   - On permanent failure (all candidates exhausted without transient error): set `ats_probe_status='miss'` as today
   - On permanent miss codes (404, 410): continue to next slug candidate (existing behavior)

3. **Files modified:**
   - `job_finder/web/ats_prober.py` — Add `_probe_greenhouse_with_result()` and `_probe_ashby_with_result()` (mirroring `_probe_lever_with_result()`)
   - `job_finder/web/ats_scanner.py` — Restructure `probe_ats_slugs()` inner loop (lines 258-302) to use the `_with_result` variants and call `_handle_scan_error()` on transient failures

4. **Tests:**
   - Add test: `probe_ats_slugs` with mocked 429 response → company gets `ats_probe_status='error'` and `retry_count=1`
   - Add test: `probe_ats_slugs` with mocked `requests.exceptions.Timeout` → same error+retry behavior
   - Add test: `probe_ats_slugs` with all candidates returning 404 → permanent miss (existing behavior preserved)

**Estimated scope:** ~60 lines changed in ats_prober.py, ~40 lines changed in ats_scanner.py, ~50 lines new tests.

---

### Fix 2 — Schedule Company Linkage Backfill [P0]

**Problem:** `link_jobs_to_companies()` is not in `scheduler.py`. 125 jobs (6.6%) have `company_id IS NULL` because new jobs from ingestion arrive 3x/day but company linkage only runs when manually invoked.

**Fix:**

1. **Add a daily scheduler job** in `scheduler.py` that calls `link_jobs_to_companies()`.

2. **Implementation:**
   - In `scheduler.py`, add a new job after the homepage discovery block (~line 397):
   ```python
   # -- Company linkage backfill (daily 5:00 AM) -----------------------
   def _import_company_linkage():
       from job_finder.web.backfill_companies import link_jobs_to_companies
       return link_jobs_to_companies
   ```
   - Use `_make_simple_job` pattern, BUT `link_jobs_to_companies()` takes `conn` (not `db_path, config`). Need a thin wrapper that opens a `standalone_connection` and calls the function.
   - Schedule: `CronTrigger(hour=5, minute=0)` — daily at 5:00 AM, before probing (7:30 AM) and scanning (7:00 AM). This ensures newly linked companies are available for probing/scanning the same day.

3. **Files modified:**
   - `job_finder/web/scheduler.py` — Add job registration (~15 lines)
   - `job_finder/web/backfill_companies.py` — Add `run_company_linkage(db_path: str, config: dict) -> dict` wrapper that opens a connection and calls `link_jobs_to_companies()`, returning a summary dict compatible with the scheduler logging pattern.

4. **Tests:**
   - Add test: `run_company_linkage()` links unlinked jobs and returns correct counts
   - Add test: `run_company_linkage()` is idempotent (second call returns 0 linked)

**Estimated scope:** ~30 lines new code, ~20 lines new tests.

---

### Fix 3 — Increase Probing Frequency and Add Batch Limits [P0]

**Problem:** `probe_ats_slugs()` runs Mon/Wed only (2x/week) but processes ALL pending companies in one run. With 508 pending companies, each run takes ~30 minutes. Meanwhile, ingestion creates new companies 3x/day.

**Fix:**

1. **Increase frequency to daily** and **add a batch limit** (e.g., 75 companies per run).

2. **Implementation:**
   - In `scheduler.py` line 347: Change `CronTrigger(day_of_week="mon,wed", hour=7, minute=30)` to `CronTrigger(hour=7, minute=30)` (daily)
   - In `probe_ats_slugs()` (ats_scanner.py:245-247): Add `LIMIT 75` to the SQL query:
     ```sql
     SELECT id, name_raw FROM companies
     WHERE ats_probe_status = 'pending'
     ORDER BY created_at ASC
     LIMIT 75
     ```
   - `ORDER BY created_at ASC` ensures oldest pending companies are probed first (FIFO fairness).
   - At 75/day × 3 platforms × 2 candidates × 0.5s sleep, each run is ~3-4 minutes — much more reasonable.
   - Also increase ATS scan frequency to daily: Change `CronTrigger(day_of_week="mon,wed", hour=7, minute=0)` to `CronTrigger(hour=7, minute=0)`

3. **Files modified:**
   - `job_finder/web/scheduler.py` — Two CronTrigger changes (2 lines each)
   - `job_finder/web/ats_scanner.py` — Add LIMIT and ORDER BY to pending query (line 246)

4. **Tests:**
   - Add test: `probe_ats_slugs` with 100 pending companies and LIMIT 75 → only 75 probed

**Estimated scope:** ~10 lines changed, ~15 lines new tests.

---

### Fix 4 — Replace Dead Company Enrichment [P1]

**Problem:** `company_enricher.py` uses DDG Instant Answer API for company metadata extraction. Production result: 0/1,054 companies enriched. The DDG abstract text almost never contains structured employee count data.

**Options evaluated:**

| Approach | Pro | Con |
|----------|-----|-----|
| A. Remove dead code entirely | Simplest. Removes 75-line file + migration columns | Loses the schema for future use |
| B. Replace DDG with Haiku extraction from homepage HTML | Uses existing infrastructure (homepage_url, Haiku client) | Requires homepage_url (only 2.7% have one currently) |
| C. Replace DDG with DDGS web search + Haiku extraction | More data available than Instant Answer | Adds DDG web search dependency (already in enrichment_tiers.py) |

**Recommended: Option C** — Use `DDGS().text()` (already proven in the job enrichment pipeline) to search for company info, then extract structured fields with Haiku from the search snippets. This reuses existing patterns and doesn't depend on homepage_url coverage.

**Fix:**

1. **Rewrite `enrich_company_info()` in `company_enricher.py`:**
   - Replace `search_duckduckgo()` (Instant Answer) with `DDGS().text()` (web search) — same pattern as `enrichment_tiers.py:search_ddg_web()`
   - Search query: `"{company_name}" company about employees industry`
   - Concatenate top 3 result snippets
   - If snippet text > 200 chars: call Haiku via `claude_client.call_claude()` with a structured extraction prompt for company_size, industry
   - If snippet text ≤ 200 chars: fall back to the existing regex/keyword extraction (cheap, no API cost)

2. **Schedule it:** Add to `scheduler.py` as a weekly job (e.g., Sunday 4:00 AM). Process batch of 50 companies per run (companies WHERE `company_size IS NULL` and `industry IS NULL`).

3. **Files modified:**
   - `job_finder/web/company_enricher.py` — Rewrite `enrich_company_info()` (~60 lines)
   - `job_finder/web/scheduler.py` — Add weekly enrichment job (~15 lines)
   - `job_finder/web/backfill_companies.py` — Update `run_ddg_enrichment()` to use new function signature

4. **Tests:**
   - Add test: `enrich_company_info` with mocked DDGS returning employee data → correct company_size
   - Add test: `enrich_company_info` with empty DDGS results → empty dict returned
   - Add test: `enrich_company_info` with long snippets → Haiku extraction called

**Estimated scope:** ~80 lines rewritten, ~15 lines scheduler, ~40 lines tests.

---

### Fix 5 — Increase Homepage Discovery Throughput [P1]

**Problem:** `_BATCH_CAP = 10` processes only 10 companies/day. 1,026 companies need probing. Tiers 1-2 (domain guess and slug heuristic) are zero-cost but have low hit rates for multi-word company names.

**Fix:**

1. **Split discovery into two phases per run:**
   - Phase A (zero-cost): Process up to 50 companies through Tiers 1-2 only (domain guess + slug heuristic). No API cost, sub-second per company.
   - Phase B (SerpAPI): Process up to 10 companies through Tier 3. Preserves SerpAPI quota.

2. **Implementation:**
   - In `homepage_discoverer.py`, add `_FAST_BATCH_CAP = 50` constant
   - In `run_homepage_discovery()`, first query and process up to 50 companies with `api_key=None` (skips SerpAPI tier)
   - Then query and process up to 10 remaining (original path with SerpAPI enabled)
   - This 5x increase in throughput costs nothing extra

3. **Add Tier 2c: multi-word slug as domain.** Currently Tier 1 only handles single-token names. Add a fallback that tries `{word1}{word2}.com` and `{word1}-{word2}.com` for two-word names (e.g., "Palo Alto" → paloalto.com, palo-alto.com). This is zero-cost and would cover a significant fraction of multi-word companies.

4. **Files modified:**
   - `job_finder/web/homepage_discoverer.py` — Restructure `run_homepage_discovery()`, add Tier 2c in `_try_domain_guess()` (~30 lines)

5. **Tests:**
   - Add test: `_try_domain_guess("Palo Alto Networks")` tries paloaltonetworks.com
   - Add test: `run_homepage_discovery` fast batch processes up to 50 companies
   - Add test: `run_homepage_discovery` SerpAPI batch processes up to 10 companies

**Estimated scope:** ~50 lines changed, ~40 lines new tests.

---

### Fix 6 — Unify Company Creation Path [P2]

**Problem:** Three independent creation paths with inconsistent fuzzy matching behavior. The UI "Add Company" button and ingestion path skip fuzzy matching, potentially creating duplicates.

**Fix:**

1. **Create a `find_or_create_company()` function** that both the UI route and `upsert_company()` callers can use:
   ```python
   def find_or_create_company(
       conn: sqlite3.Connection,
       name: str,
       ats_platform: str | None = None,
       ats_slug: str | None = None,
       homepage_url: str | None = None,
   ) -> int | None:
       """Find existing company by normalized name or fuzzy match, or create new."""
   ```

2. **Logic:**
   - First: exact match on `normalize_company(name)` (existing behavior)
   - Second: if no exact match, fuzzy match against all companies (threshold 85)
   - Third: if no fuzzy match, INSERT new company with `ats_probe_status='pending'`
   - Return company_id in all cases

3. **Wire into existing callers:**
   - `companies.py:add()` route (line 173) — replace `upsert_company()` with `find_or_create_company()`
   - `backfill_companies.py:link_jobs_to_companies()` (line 418) — replace inline logic with `find_or_create_company()`
   - `upsert_company()` remains for backward compatibility but delegates to `find_or_create_company()` internally

4. **Files modified:**
   - `job_finder/web/ats_scanner.py` — Add `find_or_create_company()` (~40 lines), refactor `upsert_company()` to delegate
   - `job_finder/web/blueprints/companies.py` — Update `add()` route to use new function
   - `job_finder/web/backfill_companies.py` — Simplify `link_jobs_to_companies()` to use new function

5. **Tests:**
   - Add test: `find_or_create_company` with exact match → returns existing ID
   - Add test: `find_or_create_company` with fuzzy match → returns existing ID
   - Add test: `find_or_create_company` with no match → creates new, returns new ID
   - Add test: UI add route with near-duplicate name → fuzzy matches, no duplicate

**Estimated scope:** ~60 lines new code, ~20 lines refactored, ~40 lines tests.

---

### Fix 7 — Fix Homepage Discovery Query Join [P2]

**Problem:** `run_homepage_discovery()` (homepage_discoverer.py:191) queries jobs by text name (`WHERE company = ?`) instead of foreign key (`WHERE company_id = ?`). If `name_raw` doesn't match `jobs.company`, source_urls are missed.

**Fix:**

1. **Change the SQL query** in `run_homepage_discovery()`:
   - Before (line 191-194):
     ```python
     source_url_rows = conn.execute(
         "SELECT source_urls FROM jobs WHERE company = ? AND source_urls IS NOT NULL AND source_urls != '[]'",
         (name_raw,)
     ).fetchall()
     ```
   - After:
     ```python
     source_url_rows = conn.execute(
         "SELECT source_urls FROM jobs WHERE company_id = ? AND source_urls IS NOT NULL AND source_urls != '[]'",
         (company_id,)
     ).fetchall()
     ```
   - `company_id` is already available in the loop variable from the outer query (line 184).

2. **Files modified:**
   - `job_finder/web/homepage_discoverer.py` — One-line SQL change (line 192)

3. **Tests:**
   - Add test: `run_homepage_discovery` fetches source_urls via company_id, not text name

**Estimated scope:** 1 line changed, ~15 lines new test.

---

### Fix 8 — Add Pagination to Companies Index [P2]

**Problem:** The companies index loads all 1,054 companies in one query with no LIMIT/OFFSET. The HTML response contains 1,054 table rows. This will degrade as the dataset grows.

**Fix:**

1. **Add server-side pagination** with HTMX infinite scroll.

2. **Implementation:**
   - In `companies.py:index()`: Add `page` query param (default 1), `per_page = 50`
   - Add `LIMIT ? OFFSET ?` to the SQL query
   - Return `has_more` flag in template context
   - In `companies/_table.html`: Add sentinel row at bottom with `hx-get="/companies/?page={next_page}&search=...&ats_platform=..."` `hx-trigger="revealed"` `hx-swap="afterend"` for infinite scroll
   - First page load still returns full page; subsequent pages return table row fragments

3. **Files modified:**
   - `job_finder/web/blueprints/companies.py` — Add pagination params to `index()` (~10 lines)
   - `job_finder/web/templates/companies/_table.html` — Add infinite scroll sentinel (~8 lines)

4. **Tests:**
   - Add test: Companies index with page=1 returns 50 companies
   - Add test: Companies index with page=2 returns next 50
   - Add test: HTMX request to page=2 returns fragment (no full page wrapper)

**Estimated scope:** ~20 lines changed, ~25 lines new tests.

---

### Fix 9 — Make `jobs_found_total` Accurate [P2]

**Problem:** `jobs_found_total` is append-only (`jobs_found_total + ?` in ats_scanner.py:744). It never decreases when jobs are delisted. The value represents "total jobs ever found" not "current job count." The UI displays this as if it's a current count.

**Fix (Option A — Computed value):** Replace the stored `jobs_found_total` with a live COUNT query.

**Fix (Option B — Cheaper, recommended):** Keep the column but reset it on each scan to the actual count returned by the ATS API, rather than incrementing.

**Recommended: Option B** — Simpler change, no query restructuring needed.

1. **Implementation:**
   - In `run_ats_scan()` (ats_scanner.py:740-746), change from:
     ```python
     SET last_scanned_at = ?,
         jobs_found_total = jobs_found_total + ?
     WHERE id = ?
     ```
   - To:
     ```python
     SET last_scanned_at = ?,
         jobs_found_total = ?
     WHERE id = ?
     ```
   - And pass `company_jobs_found` instead of adding to the existing value.
   - Note: This changes the semantics from "cumulative" to "latest scan count." If the cumulative value is wanted elsewhere, add a new column `jobs_found_latest`. But based on UI usage, "latest scan count" is what users expect.

2. **Recalibrate existing data:** Run a one-time UPDATE:
   ```sql
   UPDATE companies SET jobs_found_total = (
       SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
   )
   ```

3. **Files modified:**
   - `job_finder/web/ats_scanner.py` — Change UPDATE semantics (lines 740-746, also 876-881 for HTML fallback loop)

4. **Tests:**
   - Update existing test that checks `jobs_found_total` increment behavior

**Estimated scope:** ~5 lines changed, ~5 lines test adjustment.

---

### Fix 10 — Differentiated Scan Logging [P2]

**Problem:** `company_scan_log` records `jobs_found` as a single integer. When it shows 0, there's no way to distinguish "ATS API returned 0 postings" from "ATS API returned 10 postings but all were duplicates/filtered."

**Fix:**

1. **Add `jobs_matched` column** to `company_scan_log` that records the pre-dedup count.

2. **Implementation:**
   - New migration: `ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER DEFAULT NULL`
   - In `run_ats_scan()`, track both:
     - `jobs_matched` = len(job_dicts) after keyword filtering (line 661)
     - `jobs_new` = count of `is_new == True` results from upsert
   - Insert into scan log: `(company_id, scanned_at, jobs_found=jobs_new, jobs_matched=len(job_dicts))`

3. **Files modified:**
   - `job_finder/web/db_migrate.py` — Add migration (3 lines)
   - `job_finder/web/ats_scanner.py` — Track and persist both counts (~10 lines)
   - `job_finder/web/templates/companies/_row_expanded.html` — Show both counts in scan history (optional)

4. **Tests:**
   - Add test: scan log entry has both `jobs_found` and `jobs_matched` populated

**Estimated scope:** ~20 lines changed, ~10 lines new tests.

---

### Fix 11 — Add `probe_single_company()` Commit Safety [P3]

**Problem:** In `probe_single_company()` (ats_prober.py:229-232), the success path does:
```python
conn.execute("UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?", ...)
_reset_retry_state(conn, company_id, now)  # this does conn.commit()
```
There's no `conn.commit()` between the status update and `_reset_retry_state()`. If `_reset_retry_state()` raises before its commit, both changes are lost. While SQLite transactions mean they'd be atomic, the intent is clearly for the hit status to be persisted independently of retry state cleanup.

**Fix:**

1. Add `conn.commit()` after line 232 (the `ats_probe_status = 'hit'` UPDATE).
2. Similarly for the speculative probe paths (lines 276-306), add `conn.commit()` after each `UPDATE companies SET ats_probe_status = 'hit'`.

**Files modified:**
- `job_finder/web/ats_prober.py` — Add 4 `conn.commit()` calls

**Estimated scope:** 4 lines added.

---

### Fix 12 — Homepage Discovery Tests [P3]

**Problem:** `homepage_discoverer.py` has zero test coverage. It's 373 lines of production code with HTTP calls, SerpAPI integration, regex-based URL validation, and parked domain detection — all untested.

**Fix:**

1. **Create `tests/test_homepage_discoverer.py`** with tests for:
   - `_strip_company_suffixes()` — suffix removal (Inc., LLC, Corp., Ltd.)
   - `_name_to_slug()` — slugification with special char handling
   - `discover_homepage()` — Tier 1 success (single-token name, domain reachable)
   - `discover_homepage()` — Tier 1 failure, Tier 2 success (slug as domain)
   - `discover_homepage()` — Tier 3 SerpAPI fallback (mocked response)
   - `discover_homepage()` — all tiers fail → returns None
   - `_try_slug_heuristic()` — parked domain detection
   - `_try_slug_heuristic()` — non-HTML content-type rejection
   - `run_homepage_discovery()` — batch processing with mock DB
   - `run_homepage_discovery()` — SerpAPI quota error stops batch
   - `run_homepage_discovery()` — stamps `homepage_probe_attempted_at` even on failure

2. **Files created:**
   - `tests/test_homepage_discoverer.py` (~200 lines)

**Estimated scope:** ~200 lines new tests.

---

### Fix 13 — Clean Up Orphan Data [P3]

**Problem:** 3 companies with 0 linked jobs, 125 unlinked jobs. Minor data quality issues.

**Fix:**

1. **One-time data cleanup script** (or SQL commands run manually):
   ```sql
   -- Delete orphan companies with no linked jobs and no scan history
   DELETE FROM companies WHERE id NOT IN (
       SELECT DISTINCT company_id FROM jobs WHERE company_id IS NOT NULL
   ) AND id NOT IN (
       SELECT DISTINCT company_id FROM company_scan_log
   );

   -- Recalibrate jobs_found_total from actual linked job count
   UPDATE companies SET jobs_found_total = (
       SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
   );
   ```

2. **Run Fix 2 (scheduled linkage backfill)** to link the 125 orphaned jobs, THEN run the orphan cleanup.

**No code changes needed** — this is a data maintenance operation.

---

### Fix 14 — Add Companies Pipeline Health to Dashboard [P3]

**Problem:** The data problems documented in this audit are invisible to the user. There's no way to see the pending backlog, enrichment failure rate, or probing throughput from the UI.

**Fix:**

1. **Add a "Companies Health" card** to the dashboard (or a section in the companies index page) showing:
   - Pending probe backlog: `{count} companies awaiting ATS probe`
   - Enrichment coverage: `{pct}% with company_size/industry`
   - Homepage coverage: `{pct}% with homepage_url`
   - Unlinked jobs: `{count} jobs not linked to a company`
   - Last scan: `{relative_date}` with age warning if > 3 days

2. **Implementation:**
   - Add a `companies_health()` helper in `companies.py` that runs 5 COUNT queries
   - Include results in the `index()` template context
   - Add a summary bar at the top of `companies/index.html`

3. **Files modified:**
   - `job_finder/web/blueprints/companies.py` — Add `_companies_health()` helper (~20 lines), include in `index()` context
   - `job_finder/web/templates/companies/index.html` — Add health summary bar (~15 lines)

**Estimated scope:** ~35 lines new code.

---

## Part 3: Implementation Order & Dependencies

```
Fix 2 (schedule linkage)     ─── no deps, do first (unblocks Fix 13)
Fix 1 (probe retry)          ─── no deps
Fix 3 (probe frequency)      ─── after Fix 1 (retry must work before increasing volume)
    │
    ├── Fix 13 (orphan cleanup) ─── after Fix 2 + Fix 3 complete
    │
Fix 7 (homepage join fix)    ─── no deps
Fix 5 (homepage throughput)  ─── after Fix 7
Fix 11 (commit safety)       ─── no deps, trivial
    │
Fix 4 (enrichment rewrite)   ─── independent, can parallelize
Fix 6 (unified creation)     ─── independent, can parallelize
    │
Fix 8 (pagination)           ─── independent
Fix 9 (jobs_found_total)     ─── independent
Fix 10 (scan log diff)       ─── independent
Fix 12 (homepage tests)      ─── after Fix 5 + Fix 7
Fix 14 (health dashboard)    ─── after Fixes 1-5 (so the numbers are meaningful)
```

**Suggested execution waves:**

| Wave | Fixes | Theme |
|------|-------|-------|
| 1 | 1, 2, 3, 7, 11 | Fix broken pipelines |
| 2 | 4, 5, 6 | Replace dead subsystems |
| 3 | 8, 9, 10, 12, 13 | Quality & observability |
| 4 | 14 | Dashboard health visibility |

---

## Part 4: Risk Assessment

| Fix | Risk | Mitigation |
|-----|------|------------|
| Fix 1 (probe retry) | Changes probe behavior for all 508 pending companies — some may get `error` status instead of `miss` | Transient errors retry automatically; permanent misses unchanged. Net positive. |
| Fix 3 (daily probing) | Higher API call volume to ATS platforms | Batch limit of 75/day caps volume; 0.5s sleep preserved |
| Fix 4 (enrichment rewrite) | Haiku API cost for company enrichment | Weekly schedule + 50-company batch cap + only companies with NULL fields = bounded cost |
| Fix 6 (unified creation) | Changing `upsert_company()` semantics could affect ingestion | Fuzzy match is additive (finds matches, never creates fewer companies); backward compatible |
| Fix 9 (jobs_found_total) | Changes column semantics from "cumulative" to "current" | Verify no code depends on cumulative semantics before changing |

---

## Part 5: Metrics to Track Post-Fix

After implementing all fixes, track these metrics weekly to verify improvement:

| Metric | Current | Target (30 days) |
|--------|---------|-------------------|
| Companies with `pending` status | 508 (48%) | < 50 (< 5%) |
| Companies with `homepage_url` | 28 (2.7%) | > 150 (> 15%) |
| Companies with `company_size`/`industry` | 0 (0%) | > 100 (> 10%) |
| Jobs with `company_id IS NULL` | 125 (6.6%) | 0 (0%) |
| `miss_reason = 'unreachable'` count | 0 | > 0 (proves retry works) |
| `retry_count > 0` count | 0 | > 0 (proves retry fires) |
| Days since last `company_scan_log` entry | 11 | < 2 |
