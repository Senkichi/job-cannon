# DataForSEO Google Jobs Source — Implementation Plan

**Created:** 2026-04-03  
**Status:** Ready for implementation  
**Prerequisite:** API key already acquired and validated via live test (see research session notes)

---

## 1. Context and Motivation

### The Problem

Thordata (`engine=google`) caps at 3 results per query (Google's organic Jobs snippet). SerpAPI
(`engine=google_jobs`) hits the full index but is 429'd on monthly quota. Every "SerpAPI-compatible"
clone tested (ScaleSerp/ValueSerp/SerpWow/Trajextdata, Serpstack, HasData, Zenserp, SpaceSerp,
Smartproxy/Decodo, Serper.dev) does **not** support the dedicated Google Jobs engine — all confirmed
via live API test or documentation review.

### DataForSEO — Confirmed Working

Live API test on 2026-04-03 with credentials `dfs.garlic151@passinbox.com` confirmed:
- Full Google Jobs index access (not the 3-card organic snippet)
- 20 structured job items returned for `depth=20` query on "Staff Data Scientist / San Francisco"
- `google_jobs_item` type in response, confirming it's the dedicated jobs engine
- Cost: $0.0012 per task at `depth=20` (2 billing units × $0.0006)

### Current Source Landscape After This Change

| Source | Results/Query | Index | Status After |
|--------|--------------|-------|--------------|
| Gmail | Varies | LinkedIn/Glassdoor/ZipRecruiter | Active |
| SerpAPI | 10 + paginated | Full Google Jobs | Active (429'd until quota reset) |
| Thordata | 3 max | Organic snippet only | Active (limited but free-ish) |
| ScaleSerp | — | Does not support google_jobs | Wired/disabled — leave as-is |
| **DataForSEO** | **20** (configurable 10–200) | **Full Google Jobs** | **New** |

---

## 2. DataForSEO API Reference

All findings sourced directly from live documentation fetched 2026-04-03. Every claim has a source URL.

### 2.1 Authentication

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/task_post/

HTTP Basic Auth. The `Authorization` header value is `Basic <base64(login:password)>`. The trial
account credentials are already base64-encoded — the value `ZGZzLmdhcmxpYzE1MUBwYXNzaW5ib3guY29tOjA1OTVlMDVhNDcyNTlkOTI=`
decodes to `dfs.garlic151@passinbox.com:0595e05a47259d92`.

Config stores the raw pre-encoded string and passes it directly to the `Authorization` header.

### 2.2 Endpoint Architecture

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/overview/

**CRITICAL:** Google Jobs has **no live endpoint**. The `/v3/serp/google/jobs/live/` path returns
HTTP 404. This was verified both by sitemap crawl and by a live HTTP request during the research
session. Contrast with `google/organic` which does have a live endpoint. Every request must go
through the async task queue.

The three endpoints used in normal operation:

```
POST  https://api.dataforseo.com/v3/serp/google/jobs/task_post
GET   https://api.dataforseo.com/v3/serp/google/jobs/tasks_ready
GET   https://api.dataforseo.com/v3/serp/google/jobs/task_get/advanced/{id}
```

Sandbox (free, returns dummy data, identical response structure):
```
POST  https://sandbox.dataforseo.com/v3/serp/google/jobs/task_post
GET   https://sandbox.dataforseo.com/v3/serp/google/jobs/tasks_ready
GET   https://sandbox.dataforseo.com/v3/serp/google/jobs/task_get/advanced/00000000-0000-0000-0000-000000000000
```

### 2.3 task_post — Request Parameters

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/task_post/

POST body is a JSON array. Up to 100 task objects per request.

| Parameter | Type | Required | Notes |
|-----------|------|----------|-------|
| `keyword` | string | Yes | Max 700 chars. `+` decoded as space. |
| `location_name` | string | Yes (or `location_code`) | e.g. `"San Francisco,California,United States"` |
| `location_code` | integer | Yes (or `location_name`) | e.g. `2840` for United States |
| `language_name` | string | Yes (or `language_code`) | e.g. `"English"` |
| `language_code` | string | Yes (or `language_code`) | e.g. `"en"` |
| `depth` | integer | No | Default: 10. Max: 200. Each 10 = 1 billing unit. |
| `priority` | integer | No | `1` = normal (default, ~5 min), `2` = high (~1 min avg). |
| `tag` | string | No | Max 255 chars. Echoed back in responses. Useful for correlating tasks. |
| `employment_type` | array | No | `["fulltime", "partime", "contractor", "intern"]` — note: `partime` not `parttime`. |
| `location_radius` | string | No | 0–300 km. |
| `pingback_url` | string | No | GET notification when task completes. |
| `postback_url` | string | No | POST results delivered directly. Requires `postback_data`. |
| `postback_data` | string | Cond. | `"regular"`, `"advanced"`, or `"html"`. Required if `postback_url` set. |

**Notes on location:**
- Use `location_name` for human-readable input from config (e.g., `"San Francisco Bay Area"`)
- DataForSEO location names are specific — see https://docs.dataforseo.com/v3/serp/google/jobs/locations/
- If location lookup fails, fall back to embedding location in the keyword (e.g., `"Staff Data Scientist San Francisco"`)

### 2.4 task_post — Response

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/task_post/

```json
{
  "status_code": 20000,
  "status_message": "Ok.",
  "cost": 0.0012,
  "tasks": [{
    "id": "04040206-1578-0447-0000-9235ecd52e07",
    "status_code": 20100,
    "status_message": "Task Created.",
    "cost": 0.0012,
    "result": null
  }]
}
```

Collect each task's `id` (UUID string). `result` is always null at this stage.
Billing occurs at task_post time, not at task_get time.

### 2.5 tasks_ready — Polling

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/tasks_ready/

```
GET https://api.dataforseo.com/v3/serp/google/jobs/tasks_ready
```

Returns completed task IDs that haven't been collected yet. Key constraints:

- **Rate limit: 20 calls per minute** (much lower than the general 2000/min limit)
- Returns up to 1000 completed task IDs per call
- Tasks remain on the list for **3 days** after completion — no urgency to collect immediately
- Tasks submitted with `postback_url` do NOT appear here (results are pushed)
- Cost: free

Response result items:
```json
{
  "id": "04040206-1578-0447-0000-9235ecd52e07",
  "se": "google",
  "se_type": "jobs",
  "date_posted": "2026-04-03 20:00:00 +00:00",
  "tag": "staff_ds_sf",
  "endpoint_advanced": "/v3/serp/google/jobs/task_get/advanced/04040206-..."
}
```

### 2.6 task_get/advanced — Results

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/task_get/advanced/

```
GET https://api.dataforseo.com/v3/serp/google/jobs/task_get/advanced/{id}
```

Cost: **free**. Results retained for **30 days**. Beyond 30 days: `status_code: 40403`.

Result object fields:

| Field | Type | Notes |
|-------|------|-------|
| `keyword` | string | Echoed search keyword |
| `se_results_count` | integer | Usually 0 for Jobs (not meaningful) |
| `items_count` | integer | Actual items returned |
| `items` | array | `google_jobs_item` objects |
| `check_url` | string | Google URL to verify results in Incognito |
| `refinement_chips` | object/null | Search refinement suggestions |

`google_jobs_item` fields (confirmed from live test + docs):

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | Always `"google_jobs_item"` |
| `job_id` | string | Google-stable ID, e.g. `"gys8I-Zhk2IO1l5VAAAAAA=="`. Use as `source_id`. |
| `title` | string | Job title |
| `employer_name` | string | Company name |
| `employer_url` | string/null | Employer website (may be null) |
| `employer_image_url` | string/null | Logo CDN URL via DataForSEO CDN |
| `location` | string | Location string |
| `source_name` | string | Posting board, e.g. `"via LinkedIn"`, `"via Asana"` |
| `source_url` | string | Direct URL to job on the source board (or Google Jobs deep link) |
| `salary` | string/null | e.g. `"$160K–$200K a year"` or null |
| `contract_type` | string/null | e.g. `"Full-time"`, `"Contractor"` |
| `timestamp` | string | ISO-8601 UTC posting datetime: `"2026-03-23 23:06:53 +00:00"` |
| `time_ago` | string | Human-readable: `"11 days ago"` |
| `rank_group` | integer | 1-based position within result type |
| `rank_absolute` | integer | 1-based absolute SERP position |
| `position` | string | `"left"` or `"right"` |
| `xpath` | string | DOM XPath (ignore) |
| `rectangle` | null | Always null in advanced mode |

**What is NOT in the response:**
- Job description text — the enrichment pipeline (DDG search + ATS prober) fills this
- Apply links per-board breakdown — `source_url` is a single link
- Job highlights / qualifications breakdown

### 2.7 Depth and Billing

**Source:** https://dataforseo.com/pricing/serp/google-jobs-serp-api  
**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/task_post/

Billing unit = 10 results. Each unit costs $0.0006 (normal priority) or $0.0012 (high priority).

| depth | billing units | cost/task (normal) | cost/task (high) |
|-------|-------------|--------------------|--------------------|
| 10 | 1 | $0.0006 | $0.0012 |
| 20 | 2 | $0.0012 | $0.0024 |
| 30 | 3 | $0.0018 | $0.0036 |
| 50 | 5 | $0.0030 | $0.0060 |
| 100 | 10 | $0.0060 | $0.0120 |
| 200 | 20 | $0.0120 | $0.0240 |

If Google returns fewer results than `depth`, the difference is automatically refunded.

**Recommended default: `depth=20`**. Rationale:
- Doubles result coverage vs default
- Cost: $0.0012/task (2× $0.0006)
- 8 queries × $0.0012 = $0.0096/run × 3 runs/day = ~$0.86/month
- Google Jobs typically returns 10–25 results for niche seniority queries — depth=20 captures most
- Going to depth=30+ has diminishing returns and doubles/triples cost

### 2.8 Rate Limits

**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/overview/  
**Source:** https://docs.dataforseo.com/v3/serp/google/jobs/tasks_ready/

| Limit | Value |
|-------|-------|
| API calls per minute (general) | 2,000 |
| Tasks per POST request | 100 |
| Concurrent simultaneous requests | 30 |
| `tasks_ready` polling | **20 calls/minute** |

The `tasks_ready` rate limit (20/min) is the binding constraint for the polling loop.
Safe poll interval: 30 seconds (0.5/min, well within limit).

### 2.9 Error Codes

**Source:** https://docs.dataforseo.com/v3/appendix/errors/

Error classification for retry logic:

**Permanent — do not retry, log and return []:**
| Code | Meaning | Action |
|------|---------|--------|
| 40100 | Bad credentials | Log error, disable source |
| 40200 | Insufficient balance ($0) | Log warning, return [] |
| 40210 | Insufficient funds for this request | Log warning, return [] |
| 40203 | Daily cost limit exceeded | Log warning, return [] |
| 40104 | Account not verified | Log error |
| 40207 | IP not whitelisted | Log error |
| 40402 | Invalid path | Log error (code bug) |
| 40401 | Task not found | Skip that task ID |
| 40403 | Results expired (>30 days) | Skip that task ID |

**Retryable — back off and retry:**
| Code | Meaning | Action |
|------|---------|--------|
| 40202 | Rate limit exceeded | Wait 60s, retry |
| 40209 | Too many concurrent requests | Wait 10s, retry |
| 40103 | Task execution failed | Retry task (resubmit) |
| 40101 | Search engine error | Log warning, skip |
| 50301 | 3rd-party service unavailable | Log warning, return [] |
| 50303 | API update in progress | Wait 5 min, retry |

**Normal — not an error:**
| Code | Meaning | Action |
|------|---------|--------|
| 40102 | No search results | Return [] for that query |
| 40601 | Task handed (not yet queued) | Continue polling |
| 40602 | Task in queue | Continue polling |

**HTTP-level codes (before JSON parsing):**
- HTTP 401 → bad credentials
- HTTP 402 → billing issue
- HTTP 404 → wrong endpoint URL
- HTTP 500 → server error, retry

Note: **balance zero fails explicitly** (`40200`) — never silent.

### 2.10 Sandbox

**Source:** https://docs.dataforseo.com/v3/appendix/sandbox/

Replace `api.dataforseo.com` with `sandbox.dataforseo.com`. Free, no charges.
Returns dummy data with identical response structure. Use for unit test HTTP mocking validation
and for manual smoke tests without spending balance.

Fixed test task ID (always works in sandbox):
```
GET https://sandbox.dataforseo.com/v3/serp/google/jobs/task_get/advanced/00000000-0000-0000-0000-000000000000
```

---

## 3. Architecture Decision: Synchronous Polling Within `_fetch_dataforseo()`

### Options Considered

**Option A: Synchronous polling (chosen)**
Submit all tasks in one POST → poll `tasks_ready` every 30s → collect results → return jobs.
The function blocks until all tasks complete or timeout is reached.

**Option B: Fire-and-forget with deferred collection**
Submit tasks at the start of the scheduler window. A second scheduled job (e.g., 10 minutes later)
polls and collects. Requires persisting task IDs across scheduler invocations (e.g., in the DB or
a file). Considerably more complex.

**Option C: High-priority tasks + short sleep**
Use `priority=2` (~1 min avg). Submit tasks, sleep 90 seconds, call tasks_ready once, collect.
If not all ready, fall through (collect next run from 3-day tasks_ready list).

### Decision: Option A

**Rationale:** Job Cannon's scheduler runs every 8 hours. A 5-minute blocking wait inside
`_fetch_dataforseo()` is trivially acceptable for a background job. The code is simple, stateless,
and consistent with how SerpAPI and Thordata already work (synchronous request/response).

The 3-day tasks_ready retention means even if the poll loop times out, the tasks can be collected
on the next run. This is a natural fallback that Option A gets for free.

**Priority setting:** Default `priority=1` (normal, ~5 min, $0.60/1K). Make configurable. There's
no reason to default to the 2× more expensive high-priority queue for a background job with an
8-hour window between runs.

**Poll timeout:** Configurable, default 360 seconds (6 minutes). Gives normal-priority tasks enough
time. If timeout is hit, the partially-collected results are returned (partial is better than none)
and remaining task IDs are abandoned (they'll expire off tasks_ready in 3 days — acceptable loss).

---

## 4. Implementation Spec

### 4.1 New File: `job_finder/sources/dataforseo_source.py`

**Do not subclass SerpAPISource or ThordataSource.** The API shape is different enough that a
standalone class is cleaner and avoids fragile inheritance coupling.

```
job_finder/sources/dataforseo_source.py
```

#### Module docstring

```python
"""DataForSEO source — fetches jobs from Google Jobs via DataForSEO SERP API.

Async task-queue API (no live endpoint). Flow:
  1. POST tasks to task_post (billed here)
  2. Poll tasks_ready every 30s until all task IDs appear
  3. GET task_get/advanced/{id} for each completed task (free)

Pricing: $0.0006 per 10 results (normal priority), $0.0012 (high priority).
At depth=20 with 8 queries: ~$0.01/run, ~$0.86/month.
Docs: https://docs.dataforseo.com/v3/serp/google/jobs/overview/
"""
```

#### Constants

```python
_BASE_URL = "https://api.dataforseo.com"

# Matches: "$204K–$276K a year", "$160K-$180K", "204,000–276,000 a year"
_SALARY_RE = re.compile(
    r"\$?(\d[\d,]*)\s*[K]?\s*[–\-—]\s*\$?(\d[\d,]*)\s*[K]?",
    re.IGNORECASE,
)
```

#### Class signature

```python
class DataForSEOSource:
    """Fetch jobs from Google Jobs via DataForSEO SERP API."""

    def __init__(
        self,
        api_key: str,
        max_age_days: int = 7,
        depth: int = 20,
        priority: int = 1,
        poll_interval_seconds: int = 30,
        poll_timeout_seconds: int = 360,
    ):
        self._auth = api_key  # pre-encoded base64 "login:password"
        self.max_age_days = max_age_days
        self.depth = depth
        self.priority = priority
        self.poll_interval = poll_interval_seconds
        self.poll_timeout = poll_timeout_seconds
```

`api_key` is stored as `_auth` and passed directly to the `Authorization: Basic {api_key}` header.
No base64 encoding in this class — the config value is already the encoded string.

#### Public method: `fetch_jobs`

```python
def fetch_jobs(self, queries: list[dict]) -> list[Job]:
    """Submit all queries as tasks, poll until complete, return combined jobs."""
```

Flow:
1. Call `_submit_tasks(queries)` → returns `list[str]` of task UUIDs
2. If empty (all submissions failed) → return []
3. Call `_collect_results(task_ids)` → returns `list[Job]`
4. Return jobs

#### Private method: `_submit_tasks`

```python
def _submit_tasks(self, queries: list[dict]) -> list[str]:
    """POST all queries as a single batch. Returns list of task UUIDs."""
```

- Build the payload array: one dict per query
- Each dict: `{"keyword": query, "location_name": location, "language_code": "en", "depth": self.depth, "priority": self.priority}`
- If `location` is falsy, omit `location_name` and use `location_code: 2840` (United States) as default
- Single `requests.post()` call to `{_BASE_URL}/v3/serp/google/jobs/task_post`
- Parse response; collect task IDs from tasks where `status_code == 20100`
- Log tasks that failed to create (by status_code)
- Return list of UUIDs

**Important:** DataForSEO accepts all queries in a single POST (up to 100). We will always be well
under 100 (8 queries max currently). One HTTP call for all submissions.

#### Private method: `_collect_results`

```python
def _collect_results(self, task_ids: list[str]) -> list[Job]:
    """Poll tasks_ready until all IDs appear, then fetch each. Returns all jobs."""
```

Algorithm:
```python
pending = set(task_ids)
collected = []
deadline = time.monotonic() + self.poll_timeout

while pending and time.monotonic() < deadline:
    time.sleep(self.poll_interval)  # Wait before polling
    ready_ids = self._get_ready_task_ids()  # Call tasks_ready
    for task_id in ready_ids:
        if task_id in pending:
            jobs = self._fetch_task_results(task_id)
            collected.extend(jobs)
            pending.discard(task_id)
    if pending:
        logger.debug("DataForSEO: %d tasks still pending", len(pending))

if pending:
    logger.warning(
        "DataForSEO: %d tasks did not complete within %ds timeout: %s",
        len(pending), self.poll_timeout, list(pending)
    )

return collected
```

**Note on first sleep:** Sleep BEFORE the first poll, not after. Tasks need processing time.
With `priority=1` (normal), Google Jobs tasks typically need 1–5 minutes. Sleeping 30s before
the first poll avoids a guaranteed-empty poll on the first call.

#### Private method: `_get_ready_task_ids`

```python
def _get_ready_task_ids(self) -> list[str]:
    """Call tasks_ready endpoint. Returns list of completed task UUIDs."""
```

- GET `{_BASE_URL}/v3/serp/google/jobs/tasks_ready`
- Parse response; collect `result[].id` from completed tasks
- On error: log warning, return [] (poll loop will retry)
- Rate limit: 20/min. Our poll interval (30s) keeps us at 2/min — no issue.

#### Private method: `_fetch_task_results`

```python
def _fetch_task_results(self, task_id: str) -> list[Job]:
    """GET task_get/advanced/{id}. Returns parsed Job objects."""
```

- GET `{_BASE_URL}/v3/serp/google/jobs/task_get/advanced/{task_id}`
- Check task `status_code`:
  - `20000` → proceed
  - `40102` → no results, return []
  - `40401` / `40403` → log warning, return []
  - Other error → log warning, return []
- Parse `result[0].items[]` via `_parse_item()`
- Return jobs that pass the age filter

#### Private method: `_parse_item`

```python
def _parse_item(self, item: dict) -> Optional[Job]:
    """Parse a single google_jobs_item dict into a Job. Returns None if filtered."""
```

Field mapping:
- `item["title"]` → `Job.title` (required; return None if absent)
- `item["employer_name"]` → `Job.company` (required; return None if absent)
- `item.get("location", "")` → `Job.location`
- `"dataforseo"` → `Job.source`
- `item.get("source_url", "")` → `Job.source_url`
- `item.get("job_id", "")` → `Job.source_id`
- `item.get("description")` → `None` (always; enrichment fills this)
- `_parse_timestamp(item.get("timestamp", ""))` → `Job.posted_date`
- `_extract_salary(item.get("salary"))` → `(Job.salary_min, Job.salary_max)`

Age filter using `posted_date`:
```python
if posted_date is not None:
    age_days = (datetime.now(timezone.utc) - posted_date).days
    if age_days > self.max_age_days:
        logger.info("Skipping '%s' @ '%s' — %d days old", title, company, age_days)
        return None
```

#### Private method: `_parse_timestamp`

```python
def _parse_timestamp(self, ts: str) -> Optional[datetime]:
    """Parse DataForSEO timestamp string to UTC-aware datetime."""
```

DataForSEO format: `"2026-03-23 23:06:53 +00:00"`. Use `datetime.fromisoformat(ts)`.
Return None on parse failure (job treated as includeable, same as Thordata pattern).

**Important:** The returned datetime must be timezone-aware (UTC) for comparison with
`datetime.now(timezone.utc)` in the age filter. `fromisoformat` preserves the offset.

#### Private method: `_extract_salary`

```python
def _extract_salary(self, salary_str: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Parse salary string into (min, max) USD integers."""
```

Apply `_SALARY_RE` to `salary_str`. Same K-suffix logic as `ThordataSource._extract_salary_from_extensions`.
Return `(None, None)` if null or no match.

#### HTTP helper pattern

All HTTP calls:
```python
headers = {
    "Authorization": f"Basic {self._auth}",
    "Content-Type": "application/json",
}
resp = requests.post(url, headers=headers, json=payload, timeout=30)
resp.raise_for_status()
data = resp.json()
```

Check `data["status_code"]` (not HTTP status) for DataForSEO-level errors.

#### Imports needed

```python
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from job_finder.models import Job
```

---

### 4.2 Changes to `job_finder/web/pipeline_runner.py`

Three surgical changes. Read the file before editing.

#### Change 1: Add DataForSEO keys to `summary` dict (lines 83–101)

Add after the `"scaleserp_errors": []` line:

```python
"dataforseo_fetched": 0,
"dataforseo_errors": [],
```

#### Change 2: Add `_fetch_dataforseo` call in `run_ingestion` (lines 112–121)

After the `scaleserp_jobs = _fetch_scaleserp(config, summary)` line, add:

```python
# --- DataForSEO ingestion ---
dataforseo_jobs = _fetch_dataforseo(config, summary)
```

Update the combine line:
```python
all_jobs = gmail_jobs + serpapi_jobs + thordata_jobs + scaleserp_jobs + dataforseo_jobs
```

#### Change 3: Add log_run call (lines 149–153 area)

After the ScaleSerp `log_run` block, add:

```python
if summary["dataforseo_fetched"] > 0 or summary["dataforseo_errors"]:
    try:
        log_run(runner_conn, "dataforseo", summary["dataforseo_fetched"], jobs_new, jobs_scored)
    except Exception as e:
        logger.warning("Failed to log DataForSEO run: %s", e)
```

#### Change 4: Add `_fetch_dataforseo` function

Add after `_fetch_scaleserp` (around line 380). Pattern follows `_fetch_thordata` exactly:

```python
def _fetch_dataforseo(config: dict, summary: dict) -> list[Job]:
    """Fetch jobs from DataForSEO Google Jobs SERP API with error isolation.

    Uses async task queue (no live endpoint). Submits all queries as a single
    POST batch, then polls tasks_ready until all complete or timeout is reached.

    Args:
        config: Full config dict.
        summary: Mutable summary dict to update.

    Returns:
        List of Job objects from DataForSEO.
    """
    dataforseo_config = config.get("sources", {}).get("dataforseo", {})
    if not dataforseo_config.get("enabled", False):
        logger.debug("DataForSEO source disabled in config.")
        return []

    api_key = dataforseo_config.get("api_key", "")
    if not api_key:
        msg = "DataForSEO API key not configured"
        summary["dataforseo_errors"].append(msg)
        logger.warning(msg)
        return []

    queries = dataforseo_config.get("queries", [])
    if not queries:
        logger.debug("No DataForSEO queries configured.")
        return []

    max_age_days = dataforseo_config.get("max_age_days", 7)
    depth = dataforseo_config.get("depth", 20)
    priority = dataforseo_config.get("priority", 1)
    poll_interval = dataforseo_config.get("poll_interval_seconds", 30)
    poll_timeout = dataforseo_config.get("poll_timeout_seconds", 360)

    try:
        from job_finder.sources.dataforseo_source import DataForSEOSource

        source = DataForSEOSource(
            api_key,
            max_age_days=max_age_days,
            depth=depth,
            priority=priority,
            poll_interval_seconds=poll_interval,
            poll_timeout_seconds=poll_timeout,
        )
        jobs = source.fetch_jobs(queries)
        summary["dataforseo_fetched"] = len(jobs)

        logger.info("DataForSEO: fetched %d jobs", len(jobs))
        return jobs

    except Exception as e:
        error_msg = str(e)
        summary["dataforseo_errors"].append(error_msg)
        logger.warning("DataForSEO ingestion failed: %s", error_msg)
        return []
```

---

### 4.3 Changes to `config.example.yaml`

Add after the `scaleserp:` block (line 76), before the `# --- AI scoring ---` comment:

```yaml
  dataforseo:
    # Optional: DataForSEO Google Jobs SERP API — full index, ~$0.87/month at default settings
    # Task-queue API (async: submits tasks, polls for completion, no live endpoint).
    enabled: false
    # Credentials: base64(login:password) — copy from https://app.dataforseo.com/api-access
    api_key: ""
    # Results per query (10–200). Each 10 = 1 billing unit ($0.0006 normal / $0.0012 high).
    # depth=20 recommended: doubles coverage, ~$0.0012/query.
    depth: 20
    # Only ingest jobs posted within this many days (uses posting timestamp, not time_ago string)
    max_age_days: 7
    # Task priority: 1=normal (~5 min, $0.0006/10 results), 2=high (~1 min, $0.0012/10 results)
    priority: 1
    # How often to poll for completed tasks (seconds). Min safe: 3s (20 polls/min rate limit).
    poll_interval_seconds: 30
    # Give up waiting after this many seconds. Partial results returned; remainder abandoned.
    poll_timeout_seconds: 360
    queries:
      - query: Data Scientist
        location: San Francisco Bay Area
```

---

## 5. Test Spec: `tests/test_dataforseo_source.py`

Follow the structure of `tests/test_thordata_source.py`. Use `unittest.mock.patch` for HTTP calls.

### Fixtures

```python
@pytest.fixture
def source():
    return DataForSEOSource(
        api_key="dGVzdDp0ZXN0",  # base64("test:test")
        max_age_days=7,
        depth=20,
        priority=1,
        poll_interval_seconds=0,   # No real sleeping in tests
        poll_timeout_seconds=5,
    )
```

Provide a `_make_item()` helper that returns a valid `google_jobs_item` dict:

```python
def _make_item(**overrides) -> dict:
    base = {
        "type": "google_jobs_item",
        "rank_group": 1,
        "rank_absolute": 1,
        "position": "right",
        "xpath": "/html[1]/...",
        "job_id": "gys8I-Zhk2IO1l5VAAAAAA==",
        "title": "Staff Data Scientist",
        "employer_name": "Acme Corp",
        "employer_url": None,
        "employer_image_url": None,
        "location": "San Francisco, CA",
        "source_name": "via LinkedIn",
        "source_url": "https://linkedin.com/jobs/view/12345",
        "salary": None,
        "contract_type": "Full-time",
        "timestamp": "2026-04-01 12:00:00 +00:00",
        "time_ago": "2 days ago",
        "rectangle": None,
    }
    base.update(overrides)
    return base
```

### Test Classes

#### `TestParseItem`

- `test_extracts_title`: `item["title"]` → `job.title`
- `test_extracts_company`: `item["employer_name"]` → `job.company`
- `test_extracts_location`: `item["location"]` → `job.location`
- `test_extracts_source_id`: `item["job_id"]` → `job.source_id`
- `test_extracts_source_url`: `item["source_url"]` → `job.source_url`
- `test_source_field_is_dataforseo`: `job.source == "dataforseo"`
- `test_description_is_none`: enrichment fills it; never set here
- `test_missing_title_returns_none`: `_make_item(title="")` → None
- `test_missing_employer_returns_none`: `_make_item(employer_name="")` → None

#### `TestAgeFilter`

- `test_rejects_job_older_than_max_age`: timestamp 8 days ago with max_age_days=7 → None
- `test_accepts_job_within_max_age`: timestamp 6 days ago with max_age_days=7 → Job
- `test_accepts_job_with_no_timestamp`: timestamp="" → Job (no timestamp = includeable)
- `test_accepts_job_posted_today`: timestamp = today → Job

#### `TestSalaryExtraction`

- `test_k_range_with_en_dash`: `"$160K–$200K a year"` → `(160000, 200000)`
- `test_k_range_with_hyphen`: `"$160K-$200K"` → `(160000, 200000)`
- `test_full_numbers_with_commas`: `"$160,000–$200,000 a year"` → `(160000, 200000)`
- `test_none_returns_none_none`: `None` → `(None, None)`
- `test_no_match_returns_none_none`: `"Competitive"` → `(None, None)`

#### `TestParseTimestamp`

- `test_parses_dataforseo_format`: `"2026-04-01 12:00:00 +00:00"` → aware datetime
- `test_returns_none_on_empty_string`: `""` → None
- `test_returns_none_on_invalid`: `"not a date"` → None

#### `TestSubmitTasks`

Mock `requests.post`. Test:
- `test_posts_to_correct_url`: assert URL is `…/v3/serp/google/jobs/task_post`
- `test_sends_basic_auth`: `Authorization` header starts with `"Basic "`
- `test_sends_all_queries_in_one_request`: 3 queries → 1 POST call, 3-element body
- `test_embeds_depth_and_priority`: payload items have `depth=20`, `priority=1`
- `test_uses_location_name_from_query`: `{"query": "DS", "location": "SF Bay Area"}` → `location_name: "SF Bay Area"`
- `test_uses_location_code_when_no_location`: `{"query": "DS"}` → `location_code: 2840` (United States)
- `test_returns_empty_on_http_error`: `requests.post` raises `requests.HTTPError` → `[]`
- `test_returns_empty_on_json_error`: response body not JSON → `[]`
- `test_collects_task_ids_from_20100_tasks`: response with 2 tasks both status 20100 → 2 IDs
- `test_skips_tasks_with_non_20100_status`: 1 success + 1 error task → 1 ID

#### `TestGetReadyTaskIds`

Mock `requests.get`. Test:
- `test_returns_ready_ids`: response with 3 completed tasks → 3 IDs
- `test_returns_empty_on_http_error`: exception → `[]`
- `test_returns_empty_on_non_20000_status`: root status_code 40202 → `[]`

#### `TestFetchTaskResults`

Mock `requests.get`. Test:
- `test_returns_jobs_for_20000_task`: valid response with 2 items → 2 Job objects
- `test_returns_empty_for_40102_no_results`: status 40102 → `[]`
- `test_returns_empty_for_expired_task`: status 40403 → `[]`
- `test_age_filter_applied`: items older than max_age_days excluded

#### `TestFetchJobs` (integration-style, all HTTP mocked)

- `test_submits_then_polls_then_collects`: happy path with 2 queries → calls task_post once, polls tasks_ready, calls task_get for each ID
- `test_returns_empty_when_no_tasks_submitted`: task_post fails all → `[]`
- `test_partial_results_on_timeout`: 2 tasks submitted, only 1 completes before timeout → returns jobs from the 1 completed task
- `test_deduplicates_within_run`: two tasks return same job_id → only 1 Job (dedup happens at DB level via upsert, but we shouldn't double-add within a single fetch)

---

## 6. Cost Analysis

With 8 queries at depth=20, priority=1:

| Metric | Value |
|--------|-------|
| Cost per run | 8 × 2 × $0.0006 = **$0.0096** |
| Runs per day | 3 |
| Cost per day | **$0.029** |
| Cost per month | **$0.86** |
| Potential jobs/run | up to 8 × 20 = **160** (before age filter) |

If Google returns fewer than 20 results for a query, DataForSEO auto-refunds the difference.
Real monthly cost will be lower than $0.86 when queries yield sparse results.

For comparison:
- SerpAPI at same depth (10/query default): ~$15/month
- Thordata (current): 3 results max, comparable price range

---

## 7. Validation Steps

After implementation, verify before enabling in production:

1. **Unit tests pass:**
   ```
   uv run pytest tests/test_dataforseo_source.py -v
   ```

2. **Integration smoke test with real API (sandbox first):**
   Temporarily point `_BASE_URL` to `https://sandbox.dataforseo.com` and run a single query
   against `task_get/advanced/00000000-0000-0000-0000-000000000000`. Verify the response parses
   without error and returns Job objects.

3. **Live API test with production credentials:**
   Enable in config.yaml with a single query. Run `uv run python run.py` and trigger a manual
   ingestion from the web UI. Check `logs/app.log` for:
   ```
   DataForSEO: fetched N jobs
   ```

4. **Verify dedup works:** Run ingestion twice in succession. Second run should produce
   `jobs_new: 0` (all deduped) unless new listings appeared.

5. **Verify age filter:** Set `max_age_days: 1` temporarily. Confirm old jobs are logged as
   skipped and not persisted.

6. **Verify salary parsing:** Check DB for jobs with non-null `salary_min`/`salary_max`. Should
   match any salary strings in the DataForSEO response.

---

## 8. Files Changed Summary

| File | Change Type | Description |
|------|------------|-------------|
| `job_finder/sources/dataforseo_source.py` | **New** | DataForSEO source class |
| `job_finder/web/pipeline_runner.py` | **Modified** | Add summary keys, fetch function call, log_run call, and `_fetch_dataforseo()` function |
| `config.example.yaml` | **Modified** | Add `dataforseo:` section under `sources:` |
| `tests/test_dataforseo_source.py` | **New** | Unit tests for the new source |

No changes needed to:
- `scheduler.py` — already calls `run_ingestion()` which picks up the new source automatically
- `models.py` — Job dataclass is unchanged
- Any templates or blueprints — source is purely backend

---

## 9. Notes for the Implementing Session

- The `tasks_ready` response includes ALL completed tasks for the account, not just the ones
  submitted in this run. Filter by checking `if task_id in pending` — this is already shown in
  the `_collect_results` algorithm above.

- DataForSEO returns `timestamp` in format `"2026-03-23 23:06:53 +00:00"`. Python's
  `datetime.fromisoformat()` handles this correctly in Python 3.11+. This project is on 3.13 — 
  no compatibility shim needed.

- The `source_url` field from DataForSEO is either a direct job board URL (LinkedIn, Greenhouse,
  etc.) or a Google Jobs deep link. Either is valid for `Job.source_url`. The enrichment pipeline
  will attempt to extract descriptions from both.

- `job_id` from DataForSEO (e.g. `"gys8I-Zhk2IO1l5VAAAAAA=="`) is the same Google-stable ID
  used by SerpAPI. If both DataForSEO and SerpAPI return the same job, the second upsert will
  update the existing record (not duplicate it), because dedup is on `company|title`, not `source_id`.

- The existing `scaleserp_source.py` (thin subclass of SerpAPISource) remains in place but
  disabled. Do not remove it; no changes needed.
