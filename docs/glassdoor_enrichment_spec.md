# Glassdoor Enrichment Recovery Plan

## Status Snapshot

Historical LinkedIn remediation completed successfully using targeted re-enrichment.

- `linkedin_missing_jd`: `138 -> 15`
- `linkedin_with_jd`: `120 -> 243`
- `unscored_good_jd`: `61 -> 66`
- Remaining misses are mostly non-standard/aggregator artifacts.

Current Glassdoor behavior in pipeline:

- Free-tier direct fetch for Glassdoor URLs is skipped via an inline `elif "glassdoor.com/" in url: continue` check in [`data_enricher.py`](job_finder/web/data_enricher.py).
- [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) implements a domain-aware canonical-link–prioritizing enrichment engine with `_BLOCKED_DOMAINS` (a `frozenset`) and `_PRIORITY_DOMAINS` (an ordered `list`) and a `run_agentic_backfill()` entry point, but it is **never called** — not wired into the scheduler or any periodic runner.

## Problem Statement

The core problem is missing integration.

**The correct integration point**: `run_agentic_backfill()` manages Playwright lifecycle correctly — one browser context for the entire batch. It should be registered as a scheduled job in [`scheduler.py`](job_finder/web/scheduler.py). The tier pipeline ([`data_enricher.py`](job_finder/web/data_enricher.py)) is not touched for the agentic case because opening a browser per field call is prohibitively expensive.

The missing integrations and required adjustments:

1. `run_agentic_backfill()` needs logging conversion (currently uses `print()`) and must be registered in [`scheduler.py`](job_finder/web/scheduler.py) using the `_make_tracked_job` infrastructure to ensure it appears in the dashboard activity tracker. A new `ACTION_SCHEDULED_AGENTIC_BACKFILL` constant must be added to [`activity_tracker.py`](job_finder/web/activity_tracker.py).
2. [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py)'s `_ELIGIBLE_TIERS_QUERY` does not exclude `'agentic'` or `'agentic_exhausted'`, meaning agentic backfilled jobs would loop endlessly.
3. [`data_enricher.py`](job_finder/web/data_enricher.py)'s `run_enrichment_backfill()` has two independent bugs: (a) Phase 1 RESET incorrectly resets `agentic_exhausted` rows — these must remain stranded; (b) Phase 2 SELECT is missing `'agentic'` from the exclusion list, meaning the 6-hourly scheduler run would re-enqueue agentic-enriched jobs through `enrich_job()`, overwriting valid data. These are addressed separately in Step 5d.
4. SerpAPI Tier 3 silently discards `apply_options` from Google Jobs responses, which often contain canonical ATS URLs (greenhouse.io, lever.co) at zero additional API cost.
5. Domain policy (`_BLOCKED_DOMAINS`, `_PRIORITY_DOMAINS`) is duplicated. It belongs in a central location.
6. HTTP fetch constants (`_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS`, auth-wall signals) are copy-pasted across [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py), [`careers_scraper.py`](job_finder/web/careers_scraper.py), and [`homepage_discoverer.py`](job_finder/web/homepage_discoverer.py). Critically, [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) already has `_AUTH_WALL_SIGNATURES` and [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) has an inline `auth_signals` list with overlapping but different strings — these must be unified into one canonical constant, not supplemented with a third.
7. [`agentic_enricher.run_agentic_backfill()`](job_finder/web/agentic_enricher.py) holds a single database connection open across minutes of Playwright operations, which is unsafe for concurrent SQLite operations. Per-job write connections must include an optimistic concurrency check.
8. [`agentic_enricher._call_ollama()`](job_finder/web/agentic_enricher.py) hand-rolls HTTP bypassing `OllamaProvider` entirely. It should instantiate `OllamaProvider` directly inside `run_agentic_backfill()` and pass it down to `_generate_queries()`, `_validate_page()`, and `enrich_single_job()`. `OllamaProvider.call()` returns a `ModelResult` with `.data` already parsed as a `dict` — callers must NOT call `json.loads()` on it.

## Constraints and Non-Goals

- Local single-user app. No deployment burden.
- Cost-sensitive: SerpAPI free tier is 100 queries/month. Every fix must respect the existing budget-gate patterns.
- Must not violate Glassdoor ToS: no direct scraping of Glassdoor pages.
- Do not introduce new DB columns on `jobs`.
- Do not add `"agentic"` to `TIER_ORDER` in [`data_enricher.py`](job_finder/web/data_enricher.py).

## Implementation Plan

### Step 1: Unify auth-wall signals and centralize HTTP fetch constants into `job_finder/web/enrichment_tiers.py`

**Why**: [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) already defines `_AUTH_WALL_SIGNATURES`. [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) has an inline `auth_signals` list with partially different strings. Adding a third constant `_SHORT_PAGE_AUTH_SIGNALS` would create three competing lists. Instead, merge both existing signal lists into the canonical `_AUTH_WALL_SIGNATURES` in [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) and expose a single helper function. HTTP constants are also copy-pasted across multiple modules and must be consolidated.

**What**:
- In [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py):
  - Merge the agentic inline `auth_signals` list (`"sign in"`, `"log in"`, `"create account"`, `"verify you are a human"`, `"access denied"`, `"captcha"`, `"please verify"`, `"just a moment"`) into the existing `_AUTH_WALL_SIGNATURES` list, deduplicating any overlap.
  - Add `is_short_auth_page(text: str) -> bool` function: returns `True` when any signal in `_AUTH_WALL_SIGNATURES` matches `text[:500].lower()` AND `len(text) < 2000`.
  - No new constant name is introduced; `_AUTH_WALL_SIGNATURES` remains the single source of truth.
- In [`careers_scraper.py`](job_finder/web/careers_scraper.py): delete `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS`, and the local `_AUTH_WALL_SIGNATURES` (4-entry list) — all four constants must be removed; import `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS` from `enrichment_tiers`. Replace all usages of the local `_AUTH_WALL_SIGNATURES` and its inline auth check with calls to `is_short_auth_page(text)` and `is_chrome_or_login_page(text)` from `enrichment_tiers`.
  - **Note on underscore-prefixed imports**: The import of `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS` from `enrichment_tiers` uses underscore-prefixed (nominally private) names. This is an intentional pragmatic choice for a single-codebase local app — these are genuinely shared infrastructure constants, not implementation details. This import coupling is acceptable and aligns with the existing pattern in [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) which already imports `is_chrome_or_login_page` from `enrichment_tiers`.
- In [`homepage_discoverer.py`](job_finder/web/homepage_discoverer.py): import `_HEADERS`, `_TIMEOUT` from `enrichment_tiers` and delete local definitions.
- In [`agentic_enricher._fetch_page_text()`](job_finder/web/agentic_enricher.py): replace the inline `auth_signals` list and its length-gated check with a call to `is_short_auth_page(text)` from `enrichment_tiers`. The existing `is_chrome_or_login_page(text)` call remains unchanged.

### Step 2: Centralize domain policy → `job_finder/web/domain_policy.py` (new file)

**Why**: Domain blocking is shared policy needed by multiple modules. The existing `_PRIORITY_DOMAINS` in [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) is a `list[str]` ordered by priority rank — this ordering is load-bearing for `_domain_priority()` and must be preserved.

**Module dependency constraint**: [`domain_policy.py`](job_finder/web/domain_policy.py) must have **zero imports from any `job_finder.web.*` module** — only Python stdlib is permitted. All data (domain strings) is defined as module-level constants. This prevents circular import risk: `data_enricher` → `domain_policy` ← `enrichment_tiers` ← `data_enricher` is safe only if `domain_policy` has no back-edges into the graph.

**`BLOCKED_DOMAINS` membership**: `BLOCKED_DOMAINS` must contain only: `"glassdoor.com"`, `"glassdoor.co.uk"`, `"indeed.com"`, `"ziprecruiter.com"`, `"dice.com"`. **LinkedIn (`linkedin.com`) must NOT be added to `BLOCKED_DOMAINS`**. The free-tier pipeline calls [`fetch_linkedin_jd()`](job_finder/web/enrichment_tiers.py) for `linkedin.com/jobs/` URLs; adding LinkedIn to `BLOCKED_DOMAINS` would cause the `is_blocked_domain()` check in [`data_enricher.py`](job_finder/web/data_enricher.py) to skip all LinkedIn URL fetching in the free tier. The agentic enricher lists `"linkedin.com/jobs"` in `PRIORITY_DOMAINS` (treated as a Playwright fetch target), not as a blocked domain.

**What**:
- Create [`domain_policy.py`](job_finder/web/domain_policy.py) exporting:
  - `BLOCKED_DOMAINS: frozenset[str]` — the union of all blocked domain strings (see membership constraint above).
  - `PRIORITY_DOMAINS: list[str]` — **must be a `list`, not a `frozenset`**, ordered from highest to lowest priority, as `domain_priority()` uses `enumerate()` on it.
  - `is_blocked_domain(url: str) -> bool` — returns `True` if any string in `BLOCKED_DOMAINS` appears in `url.lower()`.
  - `domain_priority(url: str) -> int` — iterates `PRIORITY_DOMAINS` with `enumerate()`; returns the index of the first match, or `100` if no match (lower = higher priority).
- In [`agentic_enricher.py`](job_finder/web/agentic_enricher.py): delete the local `_BLOCKED_DOMAINS`, `_PRIORITY_DOMAINS`, `_is_blocked_domain()`, and `_domain_priority()` definitions; import `is_blocked_domain`, `domain_priority` from `job_finder.web.domain_policy`.
- In [`data_enricher.py`](job_finder/web/data_enricher.py) free-tier fetch loop: replace `elif "glassdoor.com/" in url: continue` with `elif is_blocked_domain(url):` and log the skip via `logger.debug()` without recording costs.

### Step 3: Surface `apply_options` from Tier 3 SerpAPI with explicit tuple returns

**Why**: [`search_serpapi()`](job_finder/web/enrichment_tiers.py) retrieves `apply_options` but discards the ATS URLs.

**Python version note**: The codebase targets Python 3.10+ (evidenced by `X | Y` union syntax in [`scheduler.py`](job_finder/web/scheduler.py) line 28). Use lowercase `tuple` for the return annotation without importing `Tuple` from `typing`. The existing `from typing import Optional, Any` import in [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) requires no change.

**What**:
- In [`enrichment_tiers.search_serpapi()`](job_finder/web/enrichment_tiers.py):
  - Add `from job_finder.web.domain_policy import is_blocked_domain, domain_priority` to the imports in `enrichment_tiers.py`.
  - Change the return annotation to `tuple[Optional[dict], list[str]]`. The function now always returns a 2-tuple; `(None, [])` replaces bare `None` returns.
  - After extracting `job`, iterate `apply_options`. Filter out blocked domains via `is_blocked_domain()`, sort by `domain_priority()`, and attempt `fetch_direct_jd()` on each valid URL.
  - If any `fetch_direct_jd()` call returns text, treat it as a candidate job description: add it to the result dict under `"url_jd"` (NOT `"apply_option_jd"`) — this key is already handled by `_resolve_from_fragments()` as a `jd_full` candidate and is excluded from `_persist()`'s column allowlist enforcement. Do not introduce a new key that would collide with `_ENRICHABLE_COLUMNS`.
  - Return `(result, apply_option_urls)` where `apply_option_urls` is the sorted list of valid ATS URL strings. Return `(None, [])` when no jobs found.
  - `TransientEnrichmentError` continues to propagate as a raised exception; it is not affected by the return type change.

- In [`data_enricher.py`](job_finder/web/data_enricher.py):
  - Initialize `apply_urls: list[str] = []` **before** the `search_serpapi()` call so the variable is always defined even if `TransientEnrichmentError` is raised (which bypasses the tuple assignment, leaving `apply_urls` unbound).
  - **Replace** `serpapi_result = search_serpapi(query, serpapi_key)` with `serpapi_result, apply_urls = search_serpapi(query, serpapi_key)`.
  - **Replace** `if serpapi_result:` guard with `if serpapi_result is not None:` (a tuple is always truthy; unpacking eliminates the old guard pattern).
  - Persist `apply_urls` to `source_urls` using a **direct SQL UPDATE that bypasses `_persist()`**, because `source_urls` is intentionally excluded from `_ENRICHABLE_COLUMNS` (it is a JSON array column, not a scalar enrichment field — adding it to `_ENRICHABLE_COLUMNS` would break the allowlist safety guarantee). The read-merge-write pattern is:
    ```python
    if apply_urls and conn is not None and job_row.get("dedup_key"):
        existing_row = conn.execute(
            "SELECT source_urls FROM jobs WHERE dedup_key = ?",
            (job_row["dedup_key"],),
        ).fetchone()
        existing_json = existing_row["source_urls"] if existing_row else None
        try:
            existing_list = json.loads(existing_json) if existing_json else []
            if not isinstance(existing_list, list):
                existing_list = []
        except (json.JSONDecodeError, TypeError):
            existing_list = []
        merged = existing_list + [u for u in apply_urls if u not in existing_list]
        conn.execute(
            "UPDATE jobs SET source_urls = ? WHERE dedup_key = ?",
            (json.dumps(merged), job_row["dedup_key"]),
        )
        conn.commit()
    ```
  - `source_urls` is NOT added to `_ENRICHABLE_COLUMNS`. This is enforced in Definition of Done.
  - The `TransientEnrichmentError` catch block is unchanged; `apply_urls` remains `[]` in that path.

### Step 4: Register `run_agentic_backfill()` in `job_finder/web/activity_tracker.py` and `job_finder/web/scheduler.py`

**Why**: Playwright needs a single browser for the batch run, and the background job must emit status to the dashboard activity tracker using the established `_make_tracked_job` pattern.

**What**:

**4a. [`activity_tracker.py`](job_finder/web/activity_tracker.py)**:
- Add constant: `ACTION_SCHEDULED_AGENTIC_BACKFILL = "scheduled_agentic_backfill"`.
- This is required by `_make_tracked_job`'s `import_action` closure pattern; omitting it causes an `AttributeError` at scheduler startup.

**4b. [`agentic_enricher.py`](job_finder/web/agentic_enricher.py)**:
- Replace all `print()` calls with `logger.info()` / `logger.debug()`.

**4c. [`scheduler.py`](job_finder/web/scheduler.py)**:
- Register the job using `_make_tracked_job()`.

- **Import factory pattern**: Use the **lambda-wrapper pattern** that matches the existing `_import_stale` pattern (lines 199-200 in [`scheduler.py`](job_finder/web/scheduler.py)). This prevents signature drift if `run_agentic_backfill` gains new required parameters in the future:
  ```python
  def _import_agentic_backfill():
      from job_finder.web.agentic_enricher import run_agentic_backfill
      return lambda db_path, config: run_agentic_backfill(db_path, config)
  ```
  Do NOT return `run_agentic_backfill` directly — always use the lambda wrapper to match the established pattern.

- **Config compatibility**: `get_config_snapshot(app)` returns a plain `dict` (it reads from `app.config`, which is a standard Flask config dict). Passing this as `config` to `run_agentic_backfill(db_path, config)` is safe — `OllamaProvider.__init__` calls `config.get("providers", {}).get("ollama", {})` which works on any plain dict.

- `OllamaProvider` is instantiated **inside** `run_agentic_backfill()`, NOT in the scheduler closure — the scheduler closure only defers the import. `ImportError` and `RuntimeError` are caught inside `run_agentic_backfill()` (Step 5b guard), so `run_agentic_backfill()` always returns `0` cleanly when prerequisites are unmet; `_make_tracked_job`'s generic exception handler is never reached for those cases.

- **Activity metadata key**: Use `"jobs_enriched"` to match the naming convention used by other tracked jobs (`"jobs_found"`, `"jobs_new"`, `"jobs_scanned"`) so the dashboard activity display renders consistently.

- **Pausing**: `scheduler.pause_job()` requires the scheduler to be running. All `add_job()` calls occur before `scheduler.start()` in `init_scheduler()`. Therefore, `pause_job("agentic_backfill")` must be called **after** `scheduler.start()`, at the very end of `init_scheduler()`. To prevent any accidental first-fire before `pause_job()` executes, also pass `next_run_time=None` in `add_job()` — this defers the initial trigger and eliminates any race window.

- Implementation pattern (mirrors existing tracked jobs, with correct pause placement):

```python
def _import_agentic_backfill():
    from job_finder.web.agentic_enricher import run_agentic_backfill
    return lambda db_path, config: run_agentic_backfill(db_path, config)

def _import_agentic_action():
    from job_finder.web.activity_tracker import ACTION_SCHEDULED_AGENTIC_BACKFILL
    return ACTION_SCHEDULED_AGENTIC_BACKFILL

scheduler.add_job(
    _make_tracked_job(
        app, "Agentic backfill",
        import_func=_import_agentic_backfill,
        import_action=_import_agentic_action,
        extract_metadata=lambda r: {"jobs_enriched": r if isinstance(r, int) else 0},
    ),
    trigger=CronTrigger(hour=3, minute=30),
    id="agentic_backfill",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    next_run_time=None,  # defer first fire; prevents race before pause_job()
)

# ... all other add_job() calls ...

scheduler.start()
_scheduler = scheduler
logger.info("Scheduler started: Gmail + SerpAPI polling every 30 minutes")

# Pause AFTER start() — pause_job() requires a running scheduler.
# Resume manually via: get_scheduler().resume_job("agentic_backfill")
scheduler.pause_job("agentic_backfill")
logger.info("agentic_backfill registered and paused — manual resume required before first run")
```

### Step 5: Uncouple agentic DB scope, fix OllamaProvider integration, and patch tier exclusion guards

**Why**: Endless retries occur because agentic tiers aren't excluded in data enricher loops. `run_agentic_backfill()` holds a single SQLite DB connection for a multi-minute network-bound Playwright loop. `agentic_enricher` bypasses `OllamaProvider`, causing dead HTTP code that diverges from the rest of the model dispatch infrastructure.

**What**:

**5a. DB scoping in [`agentic_enricher.run_agentic_backfill()`](job_finder/web/agentic_enricher.py)**:
- Remove the outer long-lived `with standalone_connection(db_path) as conn:` that wraps all operations.
- Open a short `standalone_connection()` only for the SELECT, call `.fetchall()`, then exit the `with` block to release the connection before Playwright work begins.
- Inside the per-job loop, open a **new** `standalone_connection()` for each write. Use an **optimistic concurrency check** in the UPDATE to prevent overwriting state changed by another process between SELECT and write:
  ```sql
  UPDATE jobs SET jd_full = ?, enrichment_tier = 'agentic'
  WHERE dedup_key = ? AND enrichment_tier = 'exhausted'
  ```
  And for the not-found case:
  ```sql
  UPDATE jobs SET enrichment_tier = 'agentic_exhausted'
  WHERE dedup_key = ? AND enrichment_tier = 'exhausted'
  ```
- **Concurrency check handling**:
  - If `rowcount == 0` after the **success** UPDATE (JD was found): log at `logger.warning()` level including the `dedup_key` and JD character length — the JD cannot be safely persisted but the warning gives the operator a recovery path to manually persist it if needed.
  - If `rowcount == 0` after the **not-found** UPDATE (no JD found): skip silently — another process already advanced the tier.

**5b. OllamaProvider integration in [`agentic_enricher.py`](job_finder/web/agentic_enricher.py)**:
- Delete `_call_ollama()` entirely.
- Update `run_agentic_backfill()` to instantiate `OllamaProvider` once at the top of the function, guarded by `try/except (ImportError, RuntimeError)`:
  - `ImportError`: raised if the `ollama_provider` module or Playwright is not installed.
  - `RuntimeError`: raised by `OllamaProvider.__init__._check_health()` when the Ollama service is unreachable (connection refused / timeout). This is documented behavior of the provider, not an unexpected failure.
  ```python
  try:
      from job_finder.web.providers.ollama_provider import OllamaProvider
      from playwright.sync_api import sync_playwright
      provider = OllamaProvider(config=config)
  except (ImportError, RuntimeError) as exc:
      logger.warning("Agentic backfill unavailable: %s", exc)
      return 0
  ```
  This guard is placed before any DB or Playwright operations so `0` is always returned cleanly when prerequisites are unmet.

- Update **all three** functions that currently call `_call_ollama()` to accept `provider: OllamaProvider` as a parameter:
  - `_generate_queries(title, company, n, provider: OllamaProvider) -> list[str]`
  - `_validate_page(text, title, company, model, provider: OllamaProvider) -> tuple[bool, float]`
  - `enrich_single_job(job_row, page, model, provider: OllamaProvider) -> Optional[str]`

- Update the call site inside `run_agentic_backfill()` to pass `provider` through:
  ```python
  jd = enrich_single_job(job, page, model=model, provider=provider)
  ```

- `enrich_single_job()` must update its internal calls to pass `provider` through:
  ```python
  queries = _generate_queries(title, company, n=_MAX_SEARCH_QUERIES, provider=provider)
  ...
  is_match, confidence = _validate_page(text, title, company, model=model, provider=provider)
  ```

- Call `provider.call()` with the **system prompt in the `system` positional argument** and only user messages in `messages`. Do NOT include the system message in `messages` — `OllamaProvider.call()` prepends it internally as a system-role message (see [`ollama_provider.py`](job_finder/web/providers/ollama_provider.py) line 115: `[{"role": "system", "content": system_with_schema}] + messages`). Including the system in `messages` would double-inject it.

- Consume `result.data` directly as a dict — **do NOT call `json.loads()`** on `result.data` because `OllamaProvider.call()` already parses the JSON response internally.

- Example call site for `_generate_queries()`:
  ```python
  try:
      result = provider.call(
          model, system, [{"role": "user", "content": user_msg}], max_tokens=max_tokens
      )
      data = result.data
  except Exception as exc:
      logger.warning("OllamaProvider call failed: %s", exc)
      return _fallback_queries(title, company)
  ```

- Example call site for `_validate_page()`:
  ```python
  try:
      result = provider.call(
          model, system, [{"role": "user", "content": user_msg}], max_tokens=256
      )
      data = result.data
  except Exception as exc:
      logger.warning("OllamaProvider call failed: %s", exc)
      return False, 0.0
  ```

- `_generate_queries()` currently handles both `list` and `dict` response shapes (keys `"queries"`, `"search_queries"`, `"results"`). These shape-dispatch branches must be preserved, operating on `result.data` instead of the previous `json.loads(response)` output.

- The outer `(ImportError, RuntimeError)` guard handles Ollama-unreachable at startup; the inner `except Exception` handles mid-run transient failures (e.g., model timeout, malformed JSON from a specific query).

**5c. Tier exclusion fixes in [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py)**:
- Update `_ELIGIBLE_TIERS_QUERY`:
  ```python
  _ELIGIBLE_TIERS_QUERY = (
      "enrichment_tier IS NULL OR enrichment_tier NOT IN "
      "('exhausted', 'agentic', 'agentic_exhausted', 'serpapi', 'sonnet')"
  )
  ```
- This is the **primary gate**: `agentic` and `agentic_exhausted` jobs are never fetched by `run_enrichment_pass()`, so `enrich_job()` is never called for them from this path.

**5d. Tier exclusion + agentic guard in [`data_enricher.py`](job_finder/web/data_enricher.py)**:

This section makes three independent changes to [`data_enricher.py`](job_finder/web/data_enricher.py).

*Phase 1 RESET — remove `agentic_exhausted` from the reset clause (current code resets both `'exhausted'` and `'agentic_exhausted'`):*
```sql
UPDATE jobs SET enrichment_tier = NULL
WHERE enrichment_tier = 'exhausted'
  AND (jd_full IS NULL OR TRIM(jd_full) = '' OR LENGTH(TRIM(jd_full)) < 200)
```
`agentic_exhausted` is intentionally excluded: these jobs had Playwright + Ollama attempts fail and should not be reset by the 6-hourly standard backfill. They are intentionally stranded — re-queuing them into the standard pipeline would waste quota and overwrite valid state.

*Phase 2 SELECT — add `'agentic'` to the exclusion list (currently missing from live code):*
```sql
WHERE enrichment_tier IS NULL
   OR enrichment_tier NOT IN ('exhausted', 'agentic', 'agentic_exhausted', 'serpapi', 'sonnet')
```

*`enrich_job()` guard (secondary defense-in-depth layer):*
```python
current_tier = job_row.get("enrichment_tier")
if current_tier in ("agentic", "agentic_exhausted"):
    return {}
```
Add this guard immediately after the existing `if current_tier == "exhausted": return {}` check.

**Two-layer defense-in-depth rationale**: The `_ELIGIBLE_TIERS_QUERY` update in Step 5c is the **primary gate** — [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py)'s `run_enrichment_pass()` never fetches `agentic`/`agentic_exhausted` jobs. The `enrich_job()` guard here is the **secondary defense** for direct `enrich_job()` callers that bypass `_ELIGIBLE_TIERS_QUERY` entirely (e.g., ad-hoc scripts, future callers reaching `enrich_job()` directly with a pre-fetched job row). Both layers are required: the query gate protects the batch pipeline; the function guard protects all call sites.

**Salary gap decision**: Jobs with `enrichment_tier = 'agentic'` that have a valid `jd_full` but `NULL salary_min` will NOT be re-processed by the standard pipeline after this fix. This is the correct behavior: agentic enrichment is specifically for Glassdoor-blocked jobs; re-entering the standard pipeline would overwrite `'agentic'` tier state. Salary for these jobs can be extracted via the standard [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py) CLI (which calls `enrich_job()` directly), which the implementer may optionally choose to run post-agentic-backfill. No automated pipeline change is required.

### Step 6: Glassdoor historical backfill validation

**Why**: The scheduler job should be verified manually to ensure agentic fetches succeed on Glassdoor-heavy posting pipelines.

**Validation procedure**:
1. Run `run_agentic_backfill()` manually via script on a small sample of Glassdoor-exhausted jobs.
2. Measure strictly using `enrichment_tier` state snapshots.

   **Pre-run baseline query**:
   ```sql
   SELECT enrichment_tier, COUNT(*) AS cnt
   FROM jobs
   WHERE enrichment_tier IN ('exhausted', 'agentic', 'agentic_exhausted')
   GROUP BY enrichment_tier
   ORDER BY cnt DESC
   ```

   **Post-run diff query** (same query after manual run — compare counts):
   ```sql
   SELECT enrichment_tier, COUNT(*) AS cnt
   FROM jobs
   WHERE enrichment_tier IN ('exhausted', 'agentic', 'agentic_exhausted')
   GROUP BY enrichment_tier
   ORDER BY cnt DESC
   ```

   Validate that `exhausted` count decreased and `agentic` + `agentic_exhausted` counts increased by the corresponding amount.

3. Upon verifying correct tier transitions, resume the paused scheduler job:
   ```python
   from job_finder.web.scheduler import get_scheduler
   get_scheduler().resume_job("agentic_backfill")
   ```

## File Change Summary

| File | Change type | What changes |
|---|---|---|
| [`domain_policy.py`](job_finder/web/domain_policy.py) | **New** | `BLOCKED_DOMAINS: frozenset[str]` (glassdoor/indeed/ziprecruiter/dice only — LinkedIn excluded), `PRIORITY_DOMAINS: list[str]` (ordered), `is_blocked_domain()`, `domain_priority()`; zero imports from `job_finder.web.*` |
| [`activity_tracker.py`](job_finder/web/activity_tracker.py) | Modify | Add `ACTION_SCHEDULED_AGENTIC_BACKFILL = "scheduled_agentic_backfill"` constant |
| [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) | Modify | Add `from job_finder.web.domain_policy import is_blocked_domain, domain_priority`; merge agentic auth signals into `_AUTH_WALL_SIGNATURES`; add `is_short_auth_page()`; change `search_serpapi` return annotation to `tuple[Optional[dict], list[str]]` (Python 3.10+ lowercase); `(None, [])` for empty results; filter/sort `apply_options` URLs; store ATS fetch text under `"url_jd"` key |
| [`careers_scraper.py`](job_finder/web/careers_scraper.py) | Modify | Delete `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS`, and local `_AUTH_WALL_SIGNATURES`; import `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS` from `enrichment_tiers`; replace local auth check with `is_short_auth_page()` and `is_chrome_or_login_page()` from `enrichment_tiers` |
| [`homepage_discoverer.py`](job_finder/web/homepage_discoverer.py) | Modify | Delete local `_HEADERS`, `_TIMEOUT`; import from `enrichment_tiers` |
| [`agentic_enricher.py`](job_finder/web/agentic_enricher.py) | Modify | Delete `_call_ollama()`; instantiate `OllamaProvider` at top of `run_agentic_backfill()` guarded by `try/except (ImportError, RuntimeError)`; add `provider: OllamaProvider` param to `_generate_queries()`, `_validate_page()`, and `enrich_single_job()`; pass `provider` from `run_agentic_backfill()` → `enrich_single_job()` → `_generate_queries()` / `_validate_page()`; call `provider.call(model, system, [{"role":"user","content":user_msg}])` — system in `system` arg only; consume `ModelResult.data` directly (no `json.loads()`); preserve list/dict shape dispatch; inner `try/except Exception` on `provider.call()`; scope DB connections per-job with optimistic concurrency UPDATE; log `WARNING` when success UPDATE rowcount==0 (JD found but not persisted); replace `print()` with `logger`; import `is_blocked_domain`, `domain_priority` from `domain_policy` |
| [`data_enricher.py`](job_finder/web/data_enricher.py) | Modify | Add `agentic`/`agentic_exhausted` guard in `enrich_job()` (secondary defense); adapt free-tier skip to use `is_blocked_domain()`; initialize `apply_urls = []` before `search_serpapi()` call; unpack tuple; guard result with `is not None`; `source_urls` JSON-merge UPDATE (bypasses `_persist()`); patch Phase 1 RESET (remove `agentic_exhausted`); patch Phase 2 SELECT (add `'agentic'`) |
| [`scheduler.py`](job_finder/web/scheduler.py) | Modify | Add `_import_agentic_backfill` lambda-wrapper + `_import_agentic_action` closures; register `agentic_backfill` via `_make_tracked_job()` with `next_run_time=None`; use `"jobs_enriched"` metadata key; call `scheduler.pause_job("agentic_backfill")` **after** `scheduler.start()` |
| [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py) | Modify | Patch `_ELIGIBLE_TIERS_QUERY` to exclude `'agentic'` and `'agentic_exhausted'` (primary gate) |

## Test Impact

The following existing tests are affected by the changes in this plan and must be updated or extended before the implementation is considered complete:

| Test file | Impact | Required action |
|---|---|---|
| [`tests/test_data_enricher.py`](tests/test_data_enricher.py) | `search_serpapi` call sites now receive a 2-tuple; `enrich_job()` guard for agentic tiers | Update all mock patches of `search_serpapi` to return `(result_dict, [])` or `(None, [])`; add test cases for `enrich_job()` returning `{}` for `enrichment_tier` in `('agentic', 'agentic_exhausted')` |
| [`tests/test_backfill_enrichment.py`](tests/test_backfill_enrichment.py) | `_ELIGIBLE_TIERS_QUERY` adds two new exclusions | Audit fixture data; add test cases for exclusion of `'agentic'`/`'agentic_exhausted'` tier jobs |
| [`tests/test_scheduler.py`](tests/test_scheduler.py) | New `agentic_backfill` job must be registered and paused | Add assertions: job exists in scheduler, job is paused after `init_scheduler()` |
| [`tests/test_enrichment_tiers.py`](tests/test_enrichment_tiers.py) | New `is_short_auth_page()`; `search_serpapi` return type | Tests for `is_short_auth_page()` at `len==1999` (True), `len==2000` (False), no signal (False); tests for `search_serpapi` `(None,[])` return, `apply_options` filtering, `domain_priority()` sort order in result, `"url_jd"` key placement |
| *(new)* [`tests/test_agentic_enricher.py`](tests/test_agentic_enricher.py) | No tests exist for `agentic_enricher` | Unit tests for `_generate_queries()` and `_validate_page()` with mocked `OllamaProvider`; `enrich_single_job()` with mocked provider; `run_agentic_backfill()` with mocked playwright and DB; assert `WARNING` logged when success UPDATE rowcount==0 |
| *(new)* [`tests/test_domain_policy.py`](tests/test_domain_policy.py) | New public module | `is_blocked_domain()` edge cases (subdomain matching, case insensitivity, empty string, LinkedIn NOT blocked); `domain_priority()` ordering; `isinstance(PRIORITY_DOMAINS, list)` assertion |

## Definition of Done

- All existing tests pass with no regressions.
- No duplicate `_HEADERS`, `_TIMEOUT`, `_NOISE_TAGS`, or `_AUTH_WALL_SIGNATURES` constants remain in [`careers_scraper.py`](job_finder/web/careers_scraper.py) or [`homepage_discoverer.py`](job_finder/web/homepage_discoverer.py).
- [`enrichment_tiers.py`](job_finder/web/enrichment_tiers.py) has a single `_AUTH_WALL_SIGNATURES` constant; `is_short_auth_page()` references it.
- [`domain_policy.py`](job_finder/web/domain_policy.py) exports `PRIORITY_DOMAINS` as a `list[str]`; `BLOCKED_DOMAINS` contains only glassdoor/indeed/ziprecruiter/dice (LinkedIn excluded); zero imports from any `job_finder.web.*` module.
- [`search_serpapi()`](job_finder/web/enrichment_tiers.py) returns `tuple[Optional[dict], list[str]]`; `"url_jd"` key used for ATS fetch text; all call sites initialize `apply_urls = []` before the call; `source_urls` JSON-merge UPDATE bypasses `_persist()`; `source_urls` NOT in `_ENRICHABLE_COLUMNS`.
- [`activity_tracker.py`](job_finder/web/activity_tracker.py) contains `ACTION_SCHEDULED_AGENTIC_BACKFILL`.
- `agentic_backfill` registered in [`scheduler.py`](job_finder/web/scheduler.py) via `_make_tracked_job()` with lambda-wrapper import, `"jobs_enriched"` metadata key, `next_run_time=None`, `pause_job()` called after `start()`.
- `run_agentic_backfill()` guarded by `try/except (ImportError, RuntimeError)`; returns `0` cleanly.
- DB connections scoped per-job; optimistic UPDATE includes `AND enrichment_tier = 'exhausted'`; `WARNING` logged when success UPDATE rowcount==0; no connection held across Playwright.
- `_call_ollama()` deleted; `OllamaProvider` instantiated once in `run_agentic_backfill()`, passed to `enrich_single_job()` → `_generate_queries()` / `_validate_page()`; system prompt in `system` arg only; no `json.loads()` on `ModelResult.data`; inner `try/except Exception` wraps `provider.call()`.
- [`backfill_enrichment.py`](job_finder/web/backfill_enrichment.py) `_ELIGIBLE_TIERS_QUERY` excludes `'agentic'` and `'agentic_exhausted'` (primary gate).
- [`data_enricher.enrich_job()`](job_finder/web/data_enricher.py) returns `{}` for `enrichment_tier` in `('agentic', 'agentic_exhausted')` (secondary guard).
- Phase 1 RESET resets only `enrichment_tier = 'exhausted'` rows with short/missing jd_full.
- Phase 2 SELECT excludes `'agentic'` and `'agentic_exhausted'`.
- Validation query (Step 6) shows transition from `exhausted` → `agentic` or `agentic_exhausted` after manual backfill run.
- [`tests/test_agentic_enricher.py`](tests/test_agentic_enricher.py) exists with coverage of `_generate_queries()`, `_validate_page()`, `enrich_single_job()`, and `run_agentic_backfill()`.
- [`tests/test_domain_policy.py`](tests/test_domain_policy.py) exists with coverage of `is_blocked_domain()`, `domain_priority()`, `PRIORITY_DOMAINS` type, and LinkedIn exclusion.
- [`tests/test_enrichment_tiers.py`](tests/test_enrichment_tiers.py) covers `is_short_auth_page()` boundaries and `search_serpapi` 2-tuple return paths.

## Architectural Revisions Made

*(Iteration 1 — fixes for ARCHITECT_CRITIQUE.md Iteration 1)*

- **FLAW-1**: Separated Phase 1 RESET and Phase 2 SELECT as two independent SQL changes in Step 5d. Clarified that `agentic_exhausted` rows are intentionally stranded.
- **FLAW-2**: Standardized `_import_agentic_backfill` to lambda-wrapper pattern matching `_import_stale`.
- **FLAW-3**: Added explicit read-merge-write code for `source_urls` JSON merge UPDATE, bypassing `_persist()`. Added to Definition of Done.
- **FLAW-4**: Added explicit `provider.call()` call sites showing system prompt in `system` arg, user messages only in `messages` list.
- **FLAW-5**: Added two-layer defense-in-depth rationale for query gate vs. function guard.
- **FLAW-6**: Added `domain_policy.py` zero-import constraint to Step 2 and Definition of Done.
- **FLAW-7**: Added `tests/test_domain_policy.py` and `is_short_auth_page()` boundary tests to Test Impact and Definition of Done.
- **FLAW-8**: Changed `extract_metadata` key from `"enriched"` to `"jobs_enriched"` to match naming convention.

*(Iteration 2 — fixes for ARCHITECT_CRITIQUE.md Iteration 2)*

- **FLAW-A**: Collapsed duplicate Problem Statement items 3/4 into single item 3. Renumbered to clean 1-8 list.
- **FLAW-B**: Removed orphaned dangling code block from Step 5d.
- **FLAW-C**: Added note acknowledging underscore-prefixed import pragmatism in Step 1.
- **FLAW-D**: Added Python 3.10+ version note to Step 3; specified lowercase `tuple` annotation.
- **FLAW-E**: Added config compatibility note to Step 4c confirming `get_config_snapshot()` returns plain dict.
- **FLAW-F**: Added `BLOCKED_DOMAINS` membership constraint to Step 2 explicitly excluding LinkedIn and explaining why.
- **FLAW-G**: Added `tests/test_enrichment_tiers.py` row to Test Impact table covering `search_serpapi` changes.

*(Iteration 3 — fixes for ARCHITECT_CRITIQUE.md Iteration 3)*

- **FLAW-I**: Added `provider: OllamaProvider` parameter to `enrich_single_job()` signature in Step 5b. Specified that `run_agentic_backfill()` passes `provider` to `enrich_single_job()`, which passes it to `_generate_queries()` and `_validate_page()`. Updated Problem Statement item 8 and File Change Summary accordingly.
- **FLAW-II**: Added explicit `from job_finder.web.domain_policy import is_blocked_domain, domain_priority` import instruction to Step 3 and updated File Change Summary for `enrichment_tiers.py`.
- **FLAW-III**: Replaced "skip silently" for success-path `rowcount==0` with `logger.warning()` log including `dedup_key` and JD length. Kept silent skip for not-found-path `rowcount==0`. Updated File Change Summary, Definition of Done, and Test Impact for `tests/test_agentic_enricher.py`.
