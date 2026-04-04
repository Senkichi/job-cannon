# Sync Execution Audit & Optimization Plan

**Date:** 2026-04-04
**Auditor posture:** Zero-Trust Principal Systems Architect
**Scope:** Last 3 scheduled syncs, full pipeline codepath analysis

---

## Executive Summary

The sync pipeline works correctly but wastes **95-98% of its wall-clock time** on redundant work. Gmail re-fetches ~1,100 already-parsed emails every run, DataForSEO blocks the thread for up to 7.5 minutes polling, parse failure logging creates thousands of junk `runs` rows, and there's no message-level dedup to prevent re-parsing identical emails. The pipeline produces 11-27 new jobs per run from 1,100-1,340 fetched items — a **1.3-2.0% yield rate**.

**Key numbers from the last 3 syncs:**

| Metric | Run 1 (15:02) | Run 2 (07:00) | Run 3 (23:01) |
|--------|--------------|--------------|--------------|
| Duration | 450.5s | 290.8s | 212.1s |
| Gmail fetched | 1,232 | 1,060 | 1,113 |
| Thordata fetched | 12 | 14 | 13 |
| DataForSEO fetched | 97 | 0 | 0 |
| **New jobs** | **18** | **27** | **11** |
| Yield rate | 1.3% | 2.5% | 1.0% |

---

## Finding 1: Gmail Re-Fetch Waste (CRITICAL)

### Diagnosis

Every sync fetches **all** Gmail job alert emails from the last `lookback_days` window (default 7 days). With 7 senders x ~500 messages max, the Gmail API is queried for 1,000-1,200+ messages every 8 hours. Each message requires:
1. A `messages.list()` paginated query (up to 5 pages per sender)
2. A `messages.get(format="full")` for each message ID
3. Body extraction and parsing

**The same emails are fetched, decoded, and parsed 3x/day for 7 days = up to 21 times per email.**

The dedup happens *after* parsing — `upsert_job()` catches duplicate `dedup_key`s — but the Gmail API calls, base64 decoding, HTML parsing, and Job object construction are all wasted work.

### Evidence

- `gmail_source.py:135-167`: `fetch_jobs()` runs a full `_search_messages()` + `_get_message()` + `parser_fn()` loop every call with no caching
- `email_parse_log` has only 404 entries (run-level, not message-level) — it tracks runs, not individual messages
- The `runs` table shows Gmail consistently fetching 1,060-1,232 per run but only 0-27 are new

### Impact

- **~1,100 redundant Gmail API calls per sync** (messages.get)
- **~60-80s per sync** wasted on Gmail parsing alone (estimated from timing: runs without DataForSEO take 65-75s, and Gmail is the dominant source)
- **Gmail API quota consumption**: at 3 syncs/day, that's ~3,300 messages.get calls/day. Gmail's default quota is 15,000 units/day (each messages.get costs 5 units = 16,500 units/day). This is at risk of hitting rate limits.

### Root Cause

No message-level deduplication. The `email_parse_log` table exists but is used for run-level logging (`message_id = "gmail_run_{timestamp}"`), not per-message tracking.

---

## Finding 2: Parse Failure Log Bloat (HIGH)

### Diagnosis

Every Gmail email that parses to zero jobs generates an `INSERT INTO runs` entry with `source="{domain}_parse_failure"`. This creates massive table bloat:

| Day | linkedin_com_parse_failure | glassdoor_com_parse_failure |
|-----|--------------------------|---------------------------|
| Apr 4 | 20 | 0 |
| Apr 3 | 133 | 7 |
| Apr 2 | 814 | 90 |
| Apr 1 | 885 | 144 |

**The `runs` table has 7,546 total entries**, the majority of which are parse failure noise. These are re-logged every sync for the same emails that repeatedly fail parsing.

### Evidence

- `pipeline_runner.py:242-258`: Every failure creates a new `runs` row with no dedup
- LinkedIn digest/meta emails that contain no parseable jobs (weekly summaries, "people also viewed", notification settings) generate a failure entry every time they're seen

### Impact

- `runs` table growing by ~1,000+ rows/day
- Activity feed UI polluted with noise
- No actionable signal — the same failures repeat every sync

### Root Cause

1. No message-level tracking — same email generates a new failure row every sync
2. No distinction between "email parsed but contained no jobs" (expected for meta emails) and "parser failed on a real job email" (bug)

---

## Finding 3: DataForSEO Synchronous Blocking Poll (MEDIUM)

### Diagnosis

When DataForSEO is active, `_collect_results()` blocks the ingestion thread in a `time.sleep(30)` polling loop for up to 360 seconds. During this time, *no other work happens* — all sources have already been fetched sequentially, so this is pure dead time.

### Evidence

- `dataforseo_source.py:144-169`: Synchronous sleep loop
- Run 1 (15:02): 450.5s total with DataForSEO active (97 results). Run 3 (23:01): 212.1s without DataForSEO. **DataForSEO adds ~240s of blocking time** (most of which is sleep)
- The poll interval is 30 seconds and typical task completion takes 60-120s, so 2-4 sleep cycles

### Impact

- Sync duration jumps from ~210s to ~450s when DataForSEO is active
- The APScheduler thread is blocked, preventing any other work
- With `max_instances=1`, if the sync takes too long, the next scheduled run is skipped

### Root Cause

Sequential source fetching with blocking poll. Sources are fetched one after another (Gmail → SerpAPI → Thordata → ScaleSerp → DataForSEO), then the DataForSEO poll blocks the thread. DataForSEO tasks could be submitted early and polled during/after other source fetches.

---

## Finding 4: Sequential Source Fetching (MEDIUM)

### Diagnosis

All five sources are fetched sequentially in `run_ingestion()`:

```python
gmail_jobs = _fetch_gmail(config, runner_conn, summary)      # ~60-80s
serpapi_jobs = _fetch_serpapi(config, summary)                # ~5s (disabled)
thordata_jobs = _fetch_thordata(config, summary)             # ~5-10s
scaleserp_jobs = _fetch_scaleserp(config, summary)           # ~5s (disabled)
dataforseo_jobs = _fetch_dataforseo(config, summary)         # ~30-240s (poll)
```

Only Gmail, Thordata, and DataForSEO are currently active. Gmail takes the longest due to message volume, and DataForSEO is the most variable due to the polling delay.

### Evidence

- `pipeline_runner.py:111-123`: Pure sequential execution
- Without DataForSEO: 65-212s. With DataForSEO: 290-450s.

### Impact

- Wall-clock time is the SUM of all sources, not the MAX
- DataForSEO tasks could be submitted BEFORE Gmail fetching begins, and polled after Gmail completes (overlapping I/O)

### Root Cause

No concurrency in source fetching. Gmail needs the DB connection (for parse failure logging), but SerpAPI/Thordata/DataForSEO are pure HTTP — they could run in parallel or be overlapped.

---

## Finding 5: Missing Pre-Ingestion Dedup Check (MEDIUM)

### Diagnosis

Every source returns Job objects that are individually scored (via `JobScorer`) and then upserted. For Gmail's ~1,100 jobs, approximately 1,080 will be duplicates that already exist in the DB. Each of these still goes through:

1. `scorer.score_jobs([job])` — keyword scoring (cheap but not free)
2. `upsert_job(conn, job)` — SELECT + UPDATE with JSON merge logic
3. `_upsert_job_company(conn, job)` — Company FK update attempt

### Evidence

- `pipeline_runner.py:129-130`: Every job is scored and persisted
- Last 3 syncs: 1,100-1,340 total fetched, only 11-27 new = **97-99% are known duplicates**
- `db.py:87-90`: Each upsert does a SELECT by dedup_key, then a full UPDATE even for no-change jobs

### Impact

- ~1,100 unnecessary `upsert_job()` UPDATE operations per sync (each with JSON parsing, merge logic, commit)
- ~1,100 unnecessary `_upsert_job_company()` calls per sync
- ~1,100 unnecessary `scorer.score_jobs()` calls per sync
- Estimated 5-15s of pure DB I/O waste per sync

### Root Cause

No batch "already-known" precheck. The pipeline could query `SELECT dedup_key FROM jobs WHERE dedup_key IN (...)` before the per-job loop and skip known jobs entirely (or at minimum skip scoring and company upsert for them).

---

## Finding 6: Enrichment Pipeline Gaps (LOW-MEDIUM)

### Diagnosis

334 jobs (15.8%) remain unscored by Haiku. Of the enrichment tier distribution:

| Tier | Total | Unscored |
|------|-------|----------|
| ddg | 515 | 155 |
| free | 417 | 74 |
| serpapi | 415 | 48 |
| haiku (enrichment) | 86 | 38 |
| exhausted | 437 | 19 |
| NULL | 152 | 0 |

155 jobs stuck at `ddg` tier are unscored — they went through DDG enrichment but still don't have sufficient JD content for scoring. These need to be escalated to higher tiers.

### Evidence

- `data_enricher.py:512-515`: Backfill query selects `enrichment_tier IS NULL OR enrichment_tier NOT IN ('exhausted', 'agentic', 'agentic_exhausted', 'serpapi', 'sonnet')`
- Jobs at `ddg` and `free` tiers ARE re-processed by backfill, but they may repeatedly fail at the same tier without advancing

### Impact

- 334 jobs will never get Haiku scores (no `jd_full` means stub detection blocks scoring)
- These jobs appear in the UI with no AI analysis, reducing signal quality

### Root Cause

The enrichment pipeline processes jobs from the top but may repeatedly attempt and fail the same tiers. The `free` and `ddg` tiers are especially prone to auth walls and low-quality results. The tier state tracks "last attempted tier" but doesn't force escalation when lower tiers have been exhausted.

---

## Finding 7: `runs` Table Not Pruned (LOW)

### Diagnosis

The `runs` table has 7,546 entries with no TTL or pruning. At the current rate (~100-200 new entries/day from actual runs + ~1,000/day from parse failures), this table will reach 100K+ rows within a few months.

### Evidence

- No `DELETE FROM runs WHERE timestamp < ...` in any scheduler job
- `orphan_cleanup` only handles the `companies` table, not `runs`

### Impact

- Slow activity feed queries over time
- DB file bloat (minor for SQLite but unnecessary)

---

## Finding 8: Scoring Provider Cost Efficiency (INFORMATIONAL)

### Diagnosis

The app is using Ollama (qwen2.5:14b) for 666 Sonnet-tier evaluations at $0 cost, plus Cerebras for 42. Anthropic's Haiku costs dominate at $0.44/day (246 calls + 56 reeval today).

The batch scoring sessions show good skip logic — session #41 at 07:13 processed 1,245 total but only scored 147 (those were the new jobs from a manual sync). Later sessions show 277 skipped, 0 scored (all already scored).

### Impact

- Cost management is working well. No waste in scoring.
- Ollama/Cerebras routing is saving significant Sonnet spend.

---

## Implementation Plan

### Priority 1: Gmail Message-Level Dedup (High impact, moderate effort)

**Goal:** Skip re-fetching and re-parsing Gmail messages already processed in a previous sync.

**Approach:** Use the `email_parse_log` table (already exists with UNIQUE on `message_id`) to track individual Gmail message IDs. Before calling `_get_message()` + parser, check if the message_id was already processed.

**Changes:**

1. **`gmail_source.py` — Add message-level dedup**
   - Accept an optional `processed_message_ids: set[str]` parameter in `fetch_jobs()`
   - After `_search_messages()`, filter out message IDs already in the set
   - Log the skip count: `"Gmail: skipping {N} already-processed messages"`

2. **`pipeline_runner.py:_fetch_gmail()` — Pass known message IDs**
   - Before calling `source.fetch_jobs()`, query:
     ```sql
     SELECT message_id FROM email_parse_log
     WHERE sender = 'gmail' AND processed_at >= datetime('now', '-{lookback_days} days')
     ```
   - Pass as `processed_message_ids` to `GmailSource.fetch_jobs()`

3. **`pipeline_runner.py:_fetch_gmail()` — Log per-message to email_parse_log**
   - After parsing each message, insert the Gmail message_id into `email_parse_log`
   - This requires passing `conn` into `GmailSource` or returning message metadata alongside jobs
   - **Preferred approach:** Return `(jobs, message_ids_processed)` from `fetch_jobs()` and bulk-insert after

**Expected impact:** Reduce Gmail API calls from ~1,100/sync to ~20-50/sync (only new emails since last run). Reduce sync time by 40-60s.

**Estimated complexity:** ~80 lines changed across 2 files.

---

### Priority 2: Pre-Ingestion Batch Dedup (High impact, low effort)

**Goal:** Skip scoring, upsert merging, and company updates for jobs whose `dedup_key` already exists and whose data hasn't meaningfully changed.

**Approach:** Before the per-job loop, do a single batch query to find all known dedup_keys, then only call `_score_and_persist()` for new jobs. For known jobs, just update `last_seen`.

**Changes:**

1. **`pipeline_runner.py:run_ingestion()` — Add pre-loop dedup**
   ```python
   # After combining all_jobs:
   candidate_keys = {job.dedup_key for job in all_jobs}
   existing_keys = set()
   if candidate_keys:
       placeholders = ",".join("?" * len(candidate_keys))
       rows = runner_conn.execute(
           f"SELECT dedup_key FROM jobs WHERE dedup_key IN ({placeholders})",
           list(candidate_keys),
       ).fetchall()
       existing_keys = {r[0] for r in rows}

   for job in all_jobs:
       if job.dedup_key in existing_keys:
           # Lightweight update: just touch last_seen and merge sources
           _touch_existing_job(job, runner_conn, summary)
       else:
           _score_and_persist(job, scorer, runner_conn, summary, new_job_keys)
   ```

2. **`pipeline_runner.py` — Add `_touch_existing_job()` lightweight updater**
   - Only updates `last_seen`, merges `sources` array, and adds new `source_url` if present
   - Skips: scoring, company upsert, full description merge, salary coalesce
   - Still increments `jobs_updated` in summary
   - **Important:** Must still call full `upsert_job()` if the job has new data (e.g., now has salary when it didn't before). Use a fast check: if `job.salary_min` is not None and existing job's `salary_min` is NULL, route to full upsert.

3. **Alternative simpler approach: skip scoring only**
   - Keep calling `upsert_job()` for all jobs (maintains merge correctness)
   - Only skip `scorer.score_jobs()` and `_upsert_job_company()` for known keys
   - Trade: less dramatic perf gain but zero risk of missing data merges

**Expected impact:** Eliminate ~1,080 scorer calls and company upserts per sync. Save 5-15s and reduce SQLite write load.

**Estimated complexity:** ~40 lines added to pipeline_runner.py.

---

### Priority 3: DataForSEO Early Submit + Overlapped Poll (Medium impact, moderate effort)

**Goal:** Submit DataForSEO tasks at the START of ingestion, then poll for results AFTER other sources complete.

**Approach:** Restructure `run_ingestion()` to submit DataForSEO tasks first, run Gmail/Thordata in parallel with the DataForSEO poll, then collect DataForSEO results.

**Changes:**

1. **`dataforseo_source.py` — Split into submit + collect**
   - Add `submit_tasks(queries) -> list[str]` public method (returns task_ids)
   - Add `collect_results(task_ids) -> list[Job]` public method (polls + fetches)
   - Keep `fetch_jobs()` as a convenience wrapper that calls both

2. **`pipeline_runner.py:run_ingestion()` — Restructure fetch order**
   ```python
   # Phase 1: Submit DataForSEO tasks (non-blocking, just HTTP POST)
   dataforseo_task_ids = _submit_dataforseo_tasks(config, summary)

   # Phase 2: Fetch other sources (while DataForSEO processes)
   gmail_jobs = _fetch_gmail(config, runner_conn, summary)
   thordata_jobs = _fetch_thordata(config, summary)

   # Phase 3: Collect DataForSEO results (tasks likely ready by now)
   dataforseo_jobs = _collect_dataforseo_results(config, summary, dataforseo_task_ids)
   ```

**Expected impact:** DataForSEO's 60-120s processing time overlaps with Gmail's 60-80s fetch time. Net savings: 60-120s when DataForSEO is active (450s -> 290-330s).

**Estimated complexity:** ~60 lines refactored across 2 files.

---

### Priority 4: Parse Failure Log Dedup (Medium impact, low effort)

**Goal:** Stop creating duplicate `runs` entries for the same failing emails every sync.

**Approach:** Use the `email_parse_log` table (with its UNIQUE message_id constraint) to track which messages have been logged as failures, and skip re-logging.

**Changes:**

1. **`pipeline_runner.py:_fetch_gmail()` — Deduplicate failure logging**
   - When `fetch_jobs()` returns per-message results (from Priority 1), only log failures for messages not already in `email_parse_log`
   - If Priority 1 is implemented, this comes for free: skipped messages are never parsed, so they never generate failure entries

2. **`pipeline_runner.py:_fetch_gmail()` — Classify meta emails**
   - Before logging a parse failure, check if the email body matches meta indicators (already implemented in `_should_archive_failure`)
   - Only log to `runs` if it's a genuine parsing failure, not a digest/meta email

3. **Add `runs` table pruning to orphan_cleanup scheduler job**
   - Add a `DELETE FROM runs WHERE timestamp < datetime('now', '-30 days') AND source LIKE '%parse_failure%'` step
   - Keeps recent failures for debugging, prunes historical noise

**Expected impact:** Reduce `runs` table growth from ~1,000/day to <20/day. Cleaner activity feed.

**Estimated complexity:** ~25 lines across 2 files.

---

### Priority 5: Source-Level Throttling Config (Low impact, low effort)

**Goal:** Make throttling visible and configurable for each source provider.

**Approach:** The existing throttling is adequate for current providers. The main gap is documentation and observability.

**Changes:**

1. **Add timing telemetry to each source fetch**
   - Log wall-clock time per source in the sync summary:
     ```python
     summary["gmail_duration_seconds"] = elapsed
     summary["dataforseo_duration_seconds"] = elapsed
     ```
   - Persist in `user_activity` metadata for dashboard visibility

2. **Add DataForSEO adaptive poll interval**
   - Current: fixed 30s poll. Tasks typically complete in 60-90s.
   - Change to: start polling at 45s, then every 15s (faster convergence once initial wait passes)
   - `poll_initial_delay_seconds` (default 45) + `poll_retry_interval_seconds` (default 15)

3. **Document throttle_delays in config.example.yaml**
   - The model_provider throttle system exists but has no config template entry

**Expected impact:** Better observability. Minor improvement in DataForSEO poll timing (~15s savings).

**Estimated complexity:** ~30 lines.

---

### Priority 6: Enrichment Tier Escalation Guard (Low impact, moderate effort)

**Goal:** Ensure jobs stuck at `ddg` or `free` tiers with insufficient JD content are escalated to paid tiers.

**Approach:** The enrichment backfill already re-processes `ddg` and `free` tier jobs. The issue is that `enrich_job()` may attempt the same failing tier again. Add a "minimum tier" parameter that forces escalation past already-failed tiers.

**Changes:**

1. **`data_enricher.py:enrich_job()` — Respect minimum tier**
   - If `enrichment_tier` is `'free'` and the job still has no JD, skip the free tier on re-entry and start from `ddg`
   - If `enrichment_tier` is `'ddg'` and still no JD, skip to `haiku`
   - This prevents re-running the same tier that already failed

2. **Track tier attempts in the DB**
   - Alternative: add an `enrichment_attempts` integer column
   - Increment on each `enrich_job()` call. If attempts > 2 at the same tier, force escalation
   - This is more robust but requires a migration

**Expected impact:** Reduce the 155 jobs stuck at `ddg` with no JD. Some will get escalated to `haiku`/`serpapi` and succeed.

**Estimated complexity:** ~50 lines + optional migration.

---

### Priority 7: `runs` Table Pruning (Low impact, trivial effort)

**Goal:** Prevent unbounded growth of the `runs` table.

**Changes:**

1. **`scheduler.py:_run_enrichment_backfill()` or `orphan_cleanup` — Add TTL purge**
   ```python
   conn.execute("""
       DELETE FROM runs
       WHERE timestamp < datetime('now', '-30 days')
         AND source LIKE '%parse_failure%'
   """)
   conn.execute("""
       DELETE FROM runs
       WHERE timestamp < datetime('now', '-90 days')
   """)
   ```

**Expected impact:** Keeps `runs` table under ~10K rows permanently.

**Estimated complexity:** ~10 lines.

---

## Implementation Order

| # | Fix | Impact | Effort | Dependencies |
|---|-----|--------|--------|-------------|
| 1 | Gmail message-level dedup | HIGH | Medium | None |
| 2 | Pre-ingestion batch dedup | HIGH | Low | None |
| 3 | DataForSEO overlapped poll | MEDIUM | Medium | None |
| 4 | Parse failure log dedup | MEDIUM | Low | Pairs with #1 |
| 5 | Source-level timing telemetry | LOW | Low | None |
| 6 | Enrichment tier escalation | LOW-MED | Medium | None |
| 7 | `runs` table pruning | LOW | Trivial | None |

Fixes 1-4 are independent and can be implemented in parallel. Fix 4 gets simpler if Fix 1 is done first (message-level tracking eliminates the root cause of duplicate failure logs).

**Estimated total impact:** Sync time reduced from 210-450s to ~60-120s. Gmail API calls reduced by ~95%. `runs` table growth reduced by ~98%.

---

## Non-Recommendations (Things That Are Working Well)

- **Dedup key normalization**: Company+title normalization is robust and well-tested
- **Cost gating**: Budget system is effective; Haiku always-allowed is correct
- **Per-job error isolation**: Solid defensive pattern, no changes needed
- **Ollama/Cerebras routing**: Excellent cost optimization, saving $15+/month on Sonnet
- **Enrichment tier ordering**: Cost-ordered pipeline is architecturally sound
- **Scheduler guards**: max_instances=1 + coalesce=True prevents runaway jobs
- **Fragment accumulation**: Composable enrichment tiers are well-designed
