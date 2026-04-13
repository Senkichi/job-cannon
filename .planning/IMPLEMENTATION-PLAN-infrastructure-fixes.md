# Implementation Plan: Post-Audit Infrastructure Fixes

## Context

A zero-trust log audit of job-cannon (April 11-13, 2026) revealed 5 systemic issues:
1. Two CronTrigger jobs with `timezone="US/Pacific"` silently stopped firing after app restart, killing ingestion for 3+ days
2. Google OAuth token refresh is duplicated across 3 files with inconsistent error handling, causing a 5-subsystem cascade failure
3. No health monitoring — the system was fully broken for 3 days with no signal
4. OAuth error spam: 220 ERROR+WARNING lines over 2 days from the same failure, no backoff
5. Manual sync blocks for 50+ minutes because scoring is coupled to the fetch path

Additionally, the careers crawl takes 3 hours for 714 companies due to serial execution. Parallelism + tier caching will reduce this to ~30-40 minutes while maintaining daily coverage of every company.

Bugs already fixed in the working tree (not part of this plan):
- `scheduler.py:203` — stale detection signature mismatch (config arg dropped)
- `liveness_checker.py:208` — pipeline_events column names corrected
- `careers_crawler.py:953` — added `exc_info=True` for traceback capture
- `__init__.py:64` — root logger level set to INFO when handler attached
- `test_liveness_checker.py:142` — test fixture schema aligned

---

## Change 1: Remove CronTrigger timezone parameter

**Problem**: `CronTrigger(hour="0,8,16", timezone="US/Pacific")` silently stops firing after app restart on Windows + APScheduler 3.11.2. All non-timezone CronTrigger jobs continue to work.

**File**: `job_finder/web/scheduler.py`

**Change A** — Ingestion poll (line 192):
```python
# Before
trigger=CronTrigger(hour="0,8,16", timezone="US/Pacific"),

# After — use individual hour triggers in local time (same effect, no timezone dependency)
trigger=CronTrigger(hour="0,8,16"),
```

**Change B** — Enrichment backfill (line 560):
```python
# Before
trigger=CronTrigger(hour="1,9,17", timezone="US/Pacific"),

# After
trigger=CronTrigger(hour="1,9,17"),
```

**That's it.** The machine's local timezone IS Pacific. APScheduler 3.x uses local time by default when no timezone is specified.

**Update the startup log** (line 601) to say "local time" instead of "Pacific":
```python
logger.info("Scheduler started: ingestion 3x/day (0:00, 8:00, 16:00 local); enrichment 1h after each (1:00, 9:00, 17:00 local)")
```

**Verification**: After restart, check `logs/app.log` for `apscheduler.executors.default: Running job "...run_pipeline..."` at the next scheduled hour. The ingestion summary log should follow within minutes.

---

## Change 2: Centralize token refresh

**Problem**: Token refresh is independently implemented in 4 files. When the token expired, each failed differently: RuntimeError, ValueError, logged error, and interactive re-auth prompt.

**Files to modify**:
- `job_finder/gmail_auth.py` — add `get_credentials()` public function (lines ~143-210)
- `job_finder/sources/gmail_source.py` — replace `_authenticate()` internals (lines 115-135)
- `job_finder/web/drive_uploader.py` — replace `get_drive_service()` credential logic (lines 37-84)
- `job_finder/web/drive_status.py` — replace `_compute_drive_status()` credential check (lines ~81-84)

**New function in `gmail_auth.py`** (add before existing `authenticate()`):

```python
class AuthenticationError(Exception):
    """Raised when Google OAuth credentials are unavailable or expired."""


def get_credentials(token_path: str = TOKEN_PATH) -> Credentials:
    """Load and refresh Google OAuth credentials.

    Non-interactive — suitable for background services. Raises
    AuthenticationError if the token is missing, revoked, or
    cannot be refreshed.
    """
    if not Path(token_path).exists():
        raise AuthenticationError(
            f"Token file not found: {token_path}. "
            "Run: python -m job_finder.gmail_auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token so next caller doesn't re-refresh
            Path(token_path).write_text(creds.to_json())
            return creds
        except Exception as exc:
            raise AuthenticationError(
                f"Token refresh failed: {exc}. "
                "Run: python -m job_finder.gmail_auth"
            ) from exc

    raise AuthenticationError(
        "Token is invalid and cannot be refreshed. "
        "Run: python -m job_finder.gmail_auth"
    )
```

Note: The existing runtime callers (gmail_source, drive_uploader) do NOT persist refreshed tokens to disk. This centralized version does — a behavior improvement that prevents redundant refreshes across callers.

**Update `gmail_source.py`** `_authenticate()` (lines 115-135):
Replace the inline credential loading with:
```python
from job_finder.gmail_auth import get_credentials, AuthenticationError
creds = get_credentials(token_path)
return build("gmail", "v1", credentials=creds)
```
Wrap in try/except that converts `AuthenticationError` to `RuntimeError` (preserving the existing interface contract for callers).

**Update `drive_uploader.py`** `get_drive_service()` (lines 37-84):
Replace the inline credential loading with:
```python
from job_finder.gmail_auth import get_credentials, AuthenticationError
creds = get_credentials(token_path)
return build("drive", "v3", credentials=creds)
```
Wrap in try/except that converts `AuthenticationError` to `ValueError` (preserving existing interface).

**Update `drive_status.py`** `_compute_drive_status()` (lines ~81-84):
Replace inline `creds.expired` check with call to `get_credentials()` wrapped in try/except.

**Verification**: Run `uv run --active pytest tests/ -k "gmail or drive" -q --tb=short`. Then trigger a sync and verify Gmail fetches jobs (check `logs/app.log` for "Gmail: fetched N jobs").

---

## Change 3: Scheduler health heartbeat

**Problem**: The system was broken for 3+ days with no signal. No health monitoring exists.

**File**: `job_finder/web/scheduler.py`

**Add a new scheduled job** that runs daily at 6:00 AM local time. It queries the DB for recent activity and logs a single health summary line.

**New function** — must be defined INSIDE `init_scheduler(app)` to access the `app` closure variable (same pattern as `run_pipeline()` and `_run_enrichment_backfill`). Add around line 460:

```python
def _run_health_check():
    """Daily health heartbeat — verify key subsystems ran recently."""
    with app.app_context():
        db_path = app.config.get("DB_PATH", "jobs.db")
        config = get_config_snapshot(app)
        issues = []

        try:
            from job_finder.web.db_helpers import standalone_connection
            with standalone_connection(db_path) as conn:
                # Action string constants (from activity_tracker.py):
                #   ACTION_SCHEDULED_SYNC = "scheduled_sync"
                #   ACTION_SCHEDULED_STALE_DETECTION = "scheduled_stale_detection"

                # 1. Did ingestion run in the last 14 hours?
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action IN ('scheduled_sync', 'sync') "
                    "AND occurred_at >= datetime('now', '-14 hours')"
                ).fetchone()
                if not row[0]:
                    issues.append("No ingestion in last 14h")

                # 2. Did stale detection run last night?
                row = conn.execute(
                    "SELECT MAX(occurred_at) FROM user_activity "
                    "WHERE action = 'scheduled_stale_detection' "
                    "AND occurred_at >= datetime('now', '-26 hours')"
                ).fetchone()
                if not row[0]:
                    issues.append("Stale detection missed last night")

                # 3. Are there recent consecutive errors from the same source?
                # (Check for >10 errors in last 24h from any single action)
                rows = conn.execute(
                    "SELECT action, COUNT(*) as cnt FROM user_activity "
                    "WHERE json_extract(metadata, '$.status') = 'failed' "
                    "AND occurred_at >= datetime('now', '-24 hours') "
                    "GROUP BY action HAVING cnt >= 5"
                ).fetchall()
                for r in rows:
                    issues.append(f"{r[0]}: {r[1]} failures in 24h")

                # 4. OAuth token validity
                try:
                    from job_finder.gmail_auth import get_credentials
                    get_credentials()
                except Exception as e:
                    issues.append(f"OAuth token invalid: {e}")

        except Exception as e:
            issues.append(f"Health check DB error: {e}")

        if issues:
            logger.warning("HEALTH_DEGRADED: %s", "; ".join(issues))
        else:
            logger.info("HEALTH_OK: ingestion, stale detection, OAuth all nominal")
```

**Register the job** (add after the existing job registrations, around line 530):

```python
scheduler.add_job(
    _run_health_check,
    trigger=CronTrigger(hour=6, minute=0),
    id="health_heartbeat",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)
```

**Verification**: Run the health check manually:
```python
uv run --active python -c "
from job_finder.web import create_app
from job_finder.config import load_config
app = create_app(config=load_config())
with app.app_context():
    # call the health check function directly
"
```
Then check `logs/app.log` for `HEALTH_OK` or `HEALTH_DEGRADED`.

---

## Change 4: Rate-limit repeated error logging

**Problem**: The OAuth failure generated 220 log lines in 2 days from 2 subsystems logging the same error every 30 minutes. After the first occurrence, these are noise.

**New file**: `job_finder/web/log_throttle.py`

Create a lightweight decorator that suppresses repeated identical error messages:

```python
"""Rate-limited logging for high-frequency scheduled jobs.

Tracks (logger_name, message_template) pairs. After the first occurrence,
identical messages are suppressed for `cooldown_seconds` (default 3600 = 1 hour).
Suppressed messages log at DEBUG with a count of how many were suppressed.
"""

import logging
import threading
import time

_lock = threading.Lock()
_seen: dict[tuple[str, str], tuple[float, int]] = {}  # (logger, msg) -> (last_logged_at, suppress_count)

DEFAULT_COOLDOWN_SECONDS = 3600  # 1 hour


def throttled_log(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args,
    cooldown: int = DEFAULT_COOLDOWN_SECONDS,
    **kwargs,
) -> None:
    """Log a message, suppressing duplicates within the cooldown window.

    First occurrence always logs at the requested level. Subsequent identical
    messages within `cooldown` seconds log at DEBUG with suppression count.
    After the cooldown expires, the next occurrence logs at full level again
    with a separate suppression summary line.
    """
    key = (logger.name, msg)
    now = time.monotonic()

    with _lock:
        if key in _seen:
            last_time, count = _seen[key]
            if now - last_time < cooldown:
                # Within cooldown — suppress to DEBUG
                _seen[key] = (last_time, count + 1)
                logger.debug("[suppressed %d] %s", count + 1, msg % args if args else msg)
                return
            else:
                # Cooldown expired — log at full level, then note suppressions
                _seen[key] = (now, 0)
                logger.log(level, msg, *args, **kwargs)
                if count > 0:
                    logger.log(level, "[%d identical messages suppressed in last %ds]", count, cooldown)
                return

        # First occurrence
        _seen[key] = (now, 0)

    logger.log(level, msg, *args, **kwargs)
```

Design note: The suppression summary is a separate log line rather than appended to the message template. This avoids format-string fragility — the original `msg` and `*args` are always passed through unmodified to `logger.log()`, preserving compatibility with any %-style format specifiers and `**kwargs` (e.g., `exc_info`, `stack_info`).

**Apply to the two main spam sources:**

In `job_finder/web/resume_feedback.py` (line 334):
```python
# Before
logger.error("Drive service unavailable for feedback poll: %s", e)

# After
from job_finder.web.log_throttle import throttled_log
throttled_log(logger, logging.ERROR, "Drive service unavailable for feedback poll: %s", e)
```

In `job_finder/web/pipeline_detector.py` (line 216):
```python
# Before
logger.warning("Pipeline detection: Gmail auth failed: %s", e)

# After
from job_finder.web.log_throttle import throttled_log
throttled_log(logger, logging.WARNING, "Pipeline detection: Gmail auth failed: %s", e)
```

**Verification**: Temporarily revoke the OAuth token, restart the app, wait for 2+ scheduled cycles (1 hour). The log should show the error once at ERROR/WARNING, then subsequent occurrences at DEBUG with suppression count. After 1 hour, the next occurrence logs at full level again with "[N suppressed in last 3600s]".

---

## Change 5: Decouple scoring from sync

**Problem**: `run_ingestion()` in `pipeline_runner.py` calls `run_haiku_scoring()` and `run_sonnet_evaluation()` synchronously after fetching jobs. This makes the manual sync block for 50+ minutes. The batch scoring system (`batch_scoring.py`) already provides standalone Haiku/Sonnet scoring with progress tracking and cancellation.

**Files to modify**:
- `job_finder/web/pipeline_runner.py` — add `score` parameter to `run_ingestion()`
- `job_finder/web/scheduler.py` — pass `score=True` in the scheduled job, `score=False` in `run_sync_now()`
- `job_finder/web/blueprints/sync.py` — trigger batch scoring after sync completes

### pipeline_runner.py

**Add `score` parameter** to `run_ingestion()` (line 67):
```python
def run_ingestion(db_path: str, config: dict, *, score: bool = True) -> dict:
```

**Guard the scoring block** (lines 158-167):
```python
# --- Two-tier AI scoring (runs after DB connection is closed) ---
if score and new_job_keys:
    sonnet_queue, haiku_scored_count = run_haiku_scoring(new_job_keys, config, db_path)
    ...
```

This is backward-compatible — existing callers that don't pass `score` get the current behavior.

### scheduler.py

**In `run_pipeline()`** (line 154) — keep scoring for scheduled runs:
```python
summary = run_ingestion(db_path, config, score=True)
```
No change needed (default is `True`).

**In `run_sync_now()`** (line 620) — skip scoring for manual sync:
```python
summary = run_ingestion(db_path, config, score=False)
```

Add `new_job_keys` to the summary return so the caller can trigger batch scoring:
After the `run_ingestion` call, also return the new job keys count:
```python
summary["has_new_jobs"] = summary.get("jobs_new", 0) > 0
```

### blueprints/sync.py

**In the `sync()` endpoint** (line 237) — after `run_sync_now` returns, auto-trigger batch haiku if new jobs were found.

The batch scoring start mechanism (from `batch_scoring.py` lines 19-60) is:
1. Count unscored jobs: `SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL AND pipeline_status NOT IN ('dismissed', 'archived')`
2. Insert session: `INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) VALUES ('haiku', 'running', ?, 0, ?)`
3. Spawn daemon thread: `threading.Thread(target=_run_batch_haiku_bg, args=(db_path, session_id, config), daemon=True).start()`

Replicate this pattern in `sync()`:
```python
summary = run_sync_now(current_app._get_current_object())

# Auto-trigger batch Haiku scoring if new jobs were ingested
if summary.get("has_new_jobs"):
    try:
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg
        db_path = current_app.config["DB_PATH"]
        config = current_app.config.get("JF_CONFIG", {})
        with standalone_connection(db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL "
                "AND pipeline_status NOT IN ('dismissed', 'archived')"
            ).fetchone()[0]
            if total > 0:
                now = utc_now_iso()
                conn.execute(
                    "INSERT INTO batch_score_sessions "
                    "(session_type, status, total, scored, started_at) "
                    "VALUES ('haiku', 'running', ?, 0, ?)",
                    (total, now),
                )
                conn.commit()
                session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                threading.Thread(
                    target=_run_batch_haiku_bg,
                    args=(db_path, session_id, config),
                    daemon=True,
                ).start()
                flash(f"Sync complete. Batch scoring {total} unscored jobs in background.", "success")
    except Exception:
        pass  # Batch scoring failure doesn't invalidate the sync
```

The `sync.py` file already imports `standalone_connection` and `utc_now_iso`. Add `import threading` at the top if not already present.

**In the `_run_sync_bg()` function** (line 179) — same change, skip scoring during sync:
The background sync path (`/sync/start`) also calls `run_sync_now()`, so it automatically gets `score=False`.

**Verification**:
1. Trigger a manual sync via the dashboard "Sync Now" button
2. It should complete in ~15-30 seconds (fetch + dedup only, no scoring)
3. Check that a batch scoring session was automatically created
4. Batch scoring should appear in the dashboard with its own progress bar
5. Verify scheduled ingestion (at the next 0/8/16 hour) still scores inline

---

## Change 6: Parallel careers crawl with tier caching

**Problem**: The careers crawl processes 714 companies serially in 3 hours. 293 need Playwright rendering (~15-20s each). Each company is a different domain, so there's no rate-limit reason to serialize.

**Files to modify**:
- `job_finder/web/db_migrate.py` — add `careers_crawl_tier` column
- `job_finder/web/careers_crawler.py` — parallelize `_crawl_companies()`, add tier caching

### Migration (db_migrate.py)

Add a new migration to the migrations list:
```python
# Migration N: careers crawl tier caching
[
    "ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT DEFAULT NULL",
],
```

This stores the last successful extraction tier per company: `'static'`, `'url_param'`, `'playwright'`, `'api_cached'`.

### Parallel crawl (careers_crawler.py)

**Key architectural decisions**:
- Playwright's sync API is NOT thread-safe. Each worker needs its own browser instance.
- `standalone_connection()` creates a new SQLite connection per call. WAL mode supports concurrent writes. Each worker gets its own connection.
- The `summary` dict is shared mutable state — use per-worker accumulation + merge.
- `all_new_job_keys` is shared mutable state — use per-worker lists + merge.
- `_score_new_jobs()` remains single-threaded (runs after all crawling completes).
- `_POLITE_DELAY` between companies on different domains is unnecessary for parallel execution. Keep it as intra-worker delay if desired for CPU/bandwidth smoothing.

**Refactor `_crawl_companies()`** (currently lines 685-802). The current function signature is `_crawl_companies(companies, db_path, config, browser, summary, all_new_job_keys)` — it takes a shared browser instance and mutates `summary`/`all_new_job_keys` in place. The new version manages its own per-worker browsers and returns results instead of mutating shared state.

Also update `crawl_careers_batch()` (line 630-639) to remove the single-browser setup. Currently it does:
```python
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    try:
        _crawl_companies(companies, db_path, config, browser, summary, all_new_job_keys)
    finally:
        browser.close()
```
Replace with a direct call to the refactored `_crawl_companies()` which handles its own browsers internally.

Update the company selection query (line 609) to include `careers_crawl_tier`:
```sql
SELECT c.id, c.name_raw, c.careers_url, c.careers_api_endpoint, c.careers_crawl_tier
FROM companies c
WHERE ...
```

The new structure:

```python
def _crawl_companies(
    companies: list,
    db_path: str,
    config: dict,
    max_workers: int = 4,  # configurable via config.careers_crawl.max_workers
) -> tuple[dict, list[str]]:
    """Crawl companies in parallel with per-worker Playwright browsers."""
    
    max_workers = config.get("careers_crawl", {}).get("max_workers", 4)
    
    # Worker function — each gets its own browser + DB connection
    def _crawl_worker(company_batch: list[dict]) -> tuple[dict, list[str]]:
        local_summary = {key: 0 for key in SUMMARY_KEYS}
        local_summary["errors"] = []
        local_new_keys = []
        
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for company in company_batch:
                    try:
                        # PRESERVE the existing per-company tier escalation
                        # logic wholesale (lines 728-787 of current code).
                        # The only additions are:
                        # 1. Check cached_tier before the escalation chain
                        # 2. Update local_summary counters instead of shared summary
                        # 3. Call _upsert_and_log() with per-worker connection
                        # 4. Write careers_crawl_tier on successful crawl
                        pass  # (placeholder — move existing tier logic here)
                    except Exception as e:
                        local_summary["errors"].append(str(e))
                    time.sleep(_POLITE_DELAY)
            finally:
                browser.close()
        
        return local_summary, local_new_keys
    
    # Split companies into batches for workers
    batches = [companies[i::max_workers] for i in range(max_workers)]
    
    # Run workers in parallel
    merged_summary = {key: 0 for key in SUMMARY_KEYS}
    merged_summary["errors"] = []
    all_new_keys = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_crawl_worker, batch) for batch in batches if batch]
        for future in concurrent.futures.as_completed(futures):
            worker_summary, worker_keys = future.result()
            # Merge counters
            for key in SUMMARY_KEYS:
                if key != "errors":
                    merged_summary[key] += worker_summary[key]
            merged_summary["errors"].extend(worker_summary["errors"])
            all_new_keys.extend(worker_keys)
    
    return merged_summary, all_new_keys
```

**Implementation notes**:
- Each worker launches its own Chromium instance via `sync_playwright()`. Playwright's sync API is not thread-safe, so each thread needs its own playwright context manager + browser.
- Companies are distributed round-robin across workers (`companies[i::max_workers]`) so stalest-first ordering is preserved within each batch.
- The `_POLITE_DELAY` remains as intra-worker pacing (each worker sleeps 1s between its companies). With 4 workers, effective throughput is ~4 companies/second for static, ~4 Playwright renders in parallel.
- Per-worker `standalone_connection()` for DB writes (upsert, timestamp update). WAL mode handles concurrent writes with 30s busy timeout.
- The main thread waits for all workers, merges results, then calls `_score_new_jobs()` single-threaded (unchanged).

**Config knob** (add to `config.example.yaml` under `careers_crawl:`):
```yaml
careers_crawl:
  max_workers: 4  # parallel browser instances for careers page crawling
```

### Tier caching logic

**In the per-company crawl logic**, before the tier escalation chain:

```python
# Load cached tier from company row
cached_tier = company["careers_crawl_tier"]  # from the new column

# If cache exists and is fresh (crawled within 7 days), try cached tier first
if cached_tier and _is_tier_cache_fresh(company, days=7):
    jobs = _try_tier(cached_tier, ...)
    if jobs:
        tier_used = cached_tier
        # skip to upsert
```

**After successful crawl**, update the cache:
```python
# In _upsert_and_log(), add to the UPDATE statement:
UPDATE companies
SET careers_crawl_last_at = ?,
    careers_crawl_tier = ?,
    ...
WHERE id = ?
```

**Cache invalidation**: Every 7 days (`_is_tier_cache_fresh` checks `careers_crawl_last_at`), the cache is ignored and the full escalation chain runs. This catches sites that redesigned their careers page.

**Expected impact**:
- Static companies (~421): save ~5s each by skipping URL probe when cache says "static" → ~35 min saved (serial), ~9 min saved (parallel)
- Playwright companies (~293): no skip benefit (still need to render), but skip static+URL probe attempts → ~3-5s each → ~20 min saved (serial), ~5 min saved (parallel)
- Combined with parallelism: **3 hours → ~30-40 minutes**

---

## Verification Plan

After implementing all changes, validate end-to-end:

1. **Run tests**: `uv run --active pytest tests/ -q --tb=short` — all tests must pass
2. **Restart app** and check `logs/app.log`:
   - `Scheduler started` message should appear (logging fix already applied)
   - No `timezone` in the CronTrigger job descriptions
3. **Trigger manual sync** via dashboard → should complete in <30s (fetch only)
   - Batch scoring should auto-start and show progress
4. **Wait for next scheduled ingestion** (0, 8, or 16 hour) → verify APScheduler `Running job "run_pipeline"` appears in logs
5. **Check health heartbeat** at 6:00 AM next day → `HEALTH_OK` or `HEALTH_DEGRADED` in logs
6. **Verify OAuth centralization**: temporarily rename `token.json`, trigger sync → should get a single consistent `AuthenticationError` from both Gmail and Drive paths
7. **Run careers crawl manually**:
   ```python
   uv run --active python -c "
   from job_finder.config import load_config
   from job_finder.web.careers_crawler import crawl_careers_batch
   cfg = load_config()
   # Set freshness to 0 to force re-crawl
   cfg.setdefault('careers_crawl', {})['freshness_days'] = 0
   result = crawl_careers_batch('jobs.db', cfg)
   import json; print(json.dumps(result, indent=2))
   "
   ```
   - Should complete in <45 min (vs 3h previously)
   - `playwright_rendered` count should match previous runs
   - `jobs_found` and `jobs_new` should be comparable to the morning run
8. **Error rate limiting**: after confirming OAuth works, intentionally break it (rename token.json), wait 2 scheduled cycles, check that the error appears once at ERROR level, then at DEBUG for subsequent occurrences

---

## Files Modified Summary

| File | Change |
|------|--------|
| `job_finder/web/scheduler.py` | Drop timezone from 2 CronTriggers, update startup log, add health heartbeat job, pass `score=False` in `run_sync_now()` |
| `job_finder/gmail_auth.py` | Add `get_credentials()` and `AuthenticationError` |
| `job_finder/sources/gmail_source.py` | Use centralized `get_credentials()` |
| `job_finder/web/drive_uploader.py` | Use centralized `get_credentials()` |
| `job_finder/web/drive_status.py` | Use centralized `get_credentials()` |
| `job_finder/web/pipeline_runner.py` | Add `score` kwarg to `run_ingestion()` |
| `job_finder/web/blueprints/sync.py` | Auto-trigger batch scoring after fetch-only sync |
| `job_finder/web/log_throttle.py` | **New file** — throttled logging helper |
| `job_finder/web/resume_feedback.py` | Use `throttled_log` for Drive error |
| `job_finder/web/pipeline_detector.py` | Use `throttled_log` for Gmail auth error |
| `job_finder/web/db_migrate.py` | Add migration for `careers_crawl_tier` column |
| `job_finder/web/careers_crawler.py` | Parallelize `_crawl_companies()`, add tier cache logic |
| `config.example.yaml` | Add `careers_crawl.max_workers: 4` |
| `tests/test_log_throttle.py` | **New file** — tests for throttled logging (first occurrence logs, suppression within cooldown, cooldown expiry re-logs with count) |
| `tests/test_gmail_auth.py` | Add tests for `get_credentials()` (valid token, expired-refreshable, expired-unrefreshable, missing file) |
| `tests/test_careers_crawler.py` | Update tests for parallel crawl + tier caching (mock ThreadPoolExecutor, verify summary merge, verify tier cache read/write) |
| `tests/test_pipeline_runner.py` or equivalent | Add test for `run_ingestion(score=False)` — verify scoring functions are NOT called |

## Implementation Order

Execute in this order to minimize risk:
1. **Change 1** (CronTrigger) — 2 lines, zero risk, immediate value
2. **Change 4** (log throttle) — new file, no existing code dependencies
3. **Change 2** (token centralization) — refactor, test OAuth paths carefully
4. **Change 3** (health heartbeat) — new scheduled job, additive
5. **Change 5** (decouple scoring) — behavioral change, test sync + batch interaction
6. **Change 6** (parallel crawl) — most complex, test with `freshness_days=0` override
