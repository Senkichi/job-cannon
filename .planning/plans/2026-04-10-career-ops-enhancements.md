# Career-Ops Enhancement Plan

> 5 recommendations derived from comparative analysis of [santifer/career-ops](https://github.com/santifer/career-ops).
> Execute in order. Each section is self-contained with exact file paths, function signatures, and insertion points.

---

## Recommendation 1: Portal-Targeted Discovery Queries

### Goal

Expand job discovery surface by running `site:` searches against 15 niche job portals via our existing SERP infrastructure. Google Jobs doesn't index every portal. `site:` queries against Wellfound, RemoteOK, Remotive, etc. catch listings our current sources miss.

### Implementation

#### 1A. New source module: `job_finder/sources/portal_search_source.py`

Create a new source class that runs `site:{portal}` queries through either SerpAPI or Thordata (whichever is enabled). This is NOT a new API integration — it reuses existing SERP infrastructure with targeted queries.

```python
"""Portal-targeted job discovery via site:-scoped SERP queries."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from job_finder.models import Job
from job_finder.web.ats_company import classify_company_name

logger = logging.getLogger(__name__)

# Portals to search. Each has a domain and a display name for source tracking.
PORTALS: list[dict[str, str]] = [
    {"domain": "wellfound.com", "name": "wellfound"},
    {"domain": "weworkremotely.com", "name": "weworkremotely"},
    {"domain": "remoteok.com", "name": "remoteok"},
    {"domain": "remotive.com", "name": "remotive"},
    {"domain": "himalayas.app", "name": "himalayas"},
    {"domain": "trueup.io", "name": "trueup"},
    {"domain": "builtin.com/jobs", "name": "builtin"},
    {"domain": "ycombinator.com/jobs", "name": "yc_jobs"},
    {"domain": "jobs.workable.com", "name": "workable"},
    {"domain": "job-boards.greenhouse.io", "name": "greenhouse_boards"},
    {"domain": "jobs.ashbyhq.com", "name": "ashby_boards"},
    {"domain": "jobs.lever.co", "name": "lever_boards"},
    {"domain": "ai-jobs.net", "name": "ai_jobs"},
    {"domain": "startup.jobs", "name": "startup_jobs"},
    {"domain": "remotefrontendjobs.com", "name": "remote_frontend"},
]

# Regex to extract company from SERP result titles like "Senior PM at EverAI" or "Role - Company"
_COMPANY_FROM_TITLE_RE = re.compile(
    r"(.+?)(?:\s+at\s+|\s*[-|@—–]\s*)(.+?)(?:\s*[-|].*)?$"
)


@dataclass
class PortalSearchSource:
    """Runs site:-scoped SERP queries across niche job portals."""

    serp_fetcher: object  # SerpAPISource or ThordataSource instance
    delay: float = 1.5  # seconds between queries (polite + rate limit safe)

    def fetch_jobs(self, keywords: list[str], config: dict | None = None) -> list[Job]:
        """Run site: queries for each keyword across all portals.

        Args:
            keywords: Search terms like ["Staff Engineer", "ML Platform"].
            config: App config dict for company classification.

        Returns:
            Deduplicated list of Job objects.
        """
        seen_urls: set[str] = set()
        all_jobs: list[Job] = []

        for keyword in keywords:
            for portal in PORTALS:
                query = f'site:{portal["domain"]} {keyword}'
                try:
                    # Both SerpAPISource and ThordataSource accept queries as
                    # list[dict] with "query" and "location" keys.
                    # For portal searches, location is empty (portals handle it).
                    raw_jobs = self.serp_fetcher.fetch_jobs(
                        [{"query": query, "location": ""}]
                    )
                    for job in raw_jobs:
                        if job.source_url in seen_urls:
                            continue
                        seen_urls.add(job.source_url)

                        # Override source to track portal origin
                        job_with_source = Job(
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            source=f"portal_{portal['name']}",
                            source_url=job.source_url,
                            source_id=job.source_id,
                            salary_min=job.salary_min,
                            salary_max=job.salary_max,
                            description=job.description,
                            posted_date=job.posted_date,
                        )
                        all_jobs.append(job_with_source)

                except Exception:
                    logger.warning(
                        "Portal search failed: %s on %s",
                        keyword,
                        portal["domain"],
                        exc_info=True,
                    )

                time.sleep(self.delay)

        logger.info(
            "Portal search: %d keywords x %d portals -> %d jobs",
            len(keywords),
            len(PORTALS),
            len(all_jobs),
        )
        return all_jobs
```

**Key design decisions:**
- Reuses existing `SerpAPISource` or `ThordataSource` as the SERP backend — no new API integration.
- `source` field is `portal_{name}` so we can track discovery channel in the DB.
- Intra-batch URL dedup via `seen_urls` set. Cross-batch dedup via existing `dedup_key` in `upsert_job`.
- 1.5s delay between queries (15 portals x N keywords = significant API volume; must be polite).
- Portal list is a module-level constant, not config — these are stable URLs that don't change.

#### 1B. Config addition: `config.example.yaml`

Add under `sources:`:

```yaml
  portal_search:
    enabled: false
    keywords:
      - Staff Engineer
      - ML Platform
      - Data Infrastructure
    # Uses serpapi or thordata as backend (whichever is enabled).
    # Set max_portals to limit how many portals are searched per run.
    max_portals: 15
```

#### 1C. Fetch function: `job_finder/web/ingestion_runner.py`

Add a new fetch function following the existing pattern:

```python
def _fetch_portal_search(config: dict, summary: dict) -> list[Job]:
    """Run portal-targeted site: searches using the best available SERP backend."""
    portal_cfg = config.get("sources", {}).get("portal_search", {})
    if not portal_cfg.get("enabled", False):
        return []

    keywords = portal_cfg.get("keywords", [])
    if not keywords:
        logger.info("Portal search: no keywords configured, skipping")
        return []

    max_portals = portal_cfg.get("max_portals", 15)

    # Pick the best available SERP backend
    serp_source = None
    serpapi_cfg = config.get("sources", {}).get("serpapi", {})
    thordata_cfg = config.get("sources", {}).get("thordata", {})

    if serpapi_cfg.get("enabled") and serpapi_cfg.get("api_key"):
        from job_finder.sources.serpapi_source import SerpAPISource
        serp_source = SerpAPISource(serpapi_cfg["api_key"])
    elif thordata_cfg.get("enabled") and thordata_cfg.get("api_key"):
        from job_finder.sources.thordata_source import ThordataSource
        serp_source = ThordataSource(thordata_cfg["api_key"])

    if serp_source is None:
        logger.info("Portal search: no SERP backend available, skipping")
        return []

    from job_finder.sources.portal_search_source import PortalSearchSource
    source = PortalSearchSource(serp_fetcher=serp_source)

    # Respect max_portals config
    import job_finder.sources.portal_search_source as pss
    original_portals = pss.PORTALS
    pss.PORTALS = original_portals[:max_portals]
    try:
        jobs = source.fetch_jobs(keywords, config)
    finally:
        pss.PORTALS = original_portals

    summary["portal_search_fetched"] = len(jobs)
    return jobs
```

#### 1D. Pipeline integration: `job_finder/web/pipeline_runner.py`

In `run_ingestion()`:

1. Add `"portal_search_fetched": 0, "portal_search_errors": 0` to the initial `summary` dict.
2. Add `portal_jobs = _fetch_portal_search(config, summary)` in Phase 2 (after the other fetches — it's independent).
3. Add `portal_jobs` to the `all_jobs` combination line.
4. Add a `log_run(conn, "portal_search", ...)` block alongside the existing source logging.

#### 1E. Scheduling

No changes needed. Portal search runs as part of the existing `ingestion_poll` job (3x/day). The 1.5s delay per query x 15 portals x N keywords will add ~90s per keyword to ingestion time, which is acceptable for a 3x/day cadence.

#### 1F. Tests: `tests/test_portal_search_source.py`

- Test `PortalSearchSource.fetch_jobs()` with a mocked SERP backend returning canned results.
- Test URL dedup within a single batch.
- Test that `source` field is correctly set to `portal_{name}`.
- Test graceful handling of SERP backend failures (individual portal failure doesn't stop the batch).
- Test that `classify_company_name` rejection is respected (verify no rejected companies in output).

---

## Recommendation 2: Ghost Job Legitimacy Scoring

### Goal

Add a lightweight legitimacy signal to the Haiku scoring prompt so ghost/phantom job postings (reposted perpetually with no intent to hire) are penalized before Sonnet spends tokens on deep evaluation.

### Implementation

#### 2A. Legitimacy signals to compute

These signals are computed BEFORE the Haiku call, from data already in the DB:

| Signal | Source | How to compute | Weight |
|--------|--------|----------------|--------|
| Posting age | `jobs.first_seen_at` vs now | `(now - first_seen_at).days` | High age = suspicious |
| Repost frequency | Count of `dedup_key` appearances in `jobs` table over time | `SELECT COUNT(*) FROM jobs WHERE dedup_key = ? AND created_at > datetime('now', '-90 days')` — but we don't track repost events. Instead: check `email_parse_log` for the same job appearing in multiple email batches. Simpler: count distinct `source` values for the same `dedup_key` — if a job appears across 4+ sources over weeks, it's likely a perpetual repost. | Medium |
| JD specificity | `jobs.description` or `jd_full` text analysis | Ratio of concrete terms (specific tech, team names, project names) to generic filler ("fast-paced environment", "team player"). A simple heuristic: count of unique proper nouns / total word count. Or: description length < 200 chars is suspicious. | Medium |
| Salary transparency | `salary_min IS NOT NULL` | Boolean: has salary range vs. doesn't. Legitimate employers increasingly post salary. Absence is a weak negative signal. | Low |

#### 2B. New helper: `job_finder/web/legitimacy_signals.py`

Create a new module that computes legitimacy signals for a job row:

```python
"""Compute legitimacy signals for ghost job detection."""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Generic filler phrases that signal low-effort / perpetual postings.
_FILLER_PHRASES = frozenset([
    "fast-paced environment",
    "team player",
    "self-starter",
    "wear many hats",
    "other duties as assigned",
    "competitive salary",
    "great benefits",
    "dynamic team",
    "exciting opportunity",
    "rock star",
    "ninja",
    "guru",
])

_WORD_RE = re.compile(r"\b\w+\b")


def compute_legitimacy_signals(job_row: dict, conn) -> dict:
    """Compute legitimacy signals from DB state.

    Args:
        job_row: Row dict from jobs table (must have id, dedup_key,
                 first_seen_at, description/jd_full, salary_min).
        conn: SQLite connection.

    Returns:
        Dict with keys: posting_age_days, source_count, has_salary,
        description_length, filler_ratio, legitimacy_note.
    """
    signals = {}

    # 1. Posting age
    first_seen = job_row.get("first_seen_at") or job_row.get("created_at")
    if first_seen:
        if isinstance(first_seen, str):
            # Parse ISO format, handle both with and without tz
            first_seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - first_seen).days
        signals["posting_age_days"] = age
    else:
        signals["posting_age_days"] = None

    # 2. Source diversity (proxy for repost frequency)
    dedup_key = job_row.get("dedup_key", "")
    if dedup_key and conn:
        row = conn.execute(
            """SELECT sources FROM jobs WHERE dedup_key = ?""",
            (dedup_key,),
        ).fetchone()
        if row and row["sources"]:
            import json
            try:
                sources = json.loads(row["sources"])
                signals["source_count"] = len(sources) if isinstance(sources, list) else 1
            except (json.JSONDecodeError, TypeError):
                signals["source_count"] = 1
        else:
            signals["source_count"] = 1
    else:
        signals["source_count"] = 1

    # 3. Salary transparency
    signals["has_salary"] = job_row.get("salary_min") is not None

    # 4. JD specificity
    text = job_row.get("jd_full") or job_row.get("description") or ""
    signals["description_length"] = len(text)

    if text and len(text) > 100:
        words = _WORD_RE.findall(text.lower())
        total_words = len(words)
        filler_count = sum(
            1 for phrase in _FILLER_PHRASES
            if phrase in text.lower()
        )
        signals["filler_ratio"] = round(filler_count / max(total_words / 50, 1), 2)
    else:
        signals["filler_ratio"] = 0.0

    # 5. Build human-readable note for the Haiku prompt
    notes = []
    age = signals["posting_age_days"]
    if age is not None and age > 60:
        notes.append(f"WARNING: Posting is {age} days old")
    elif age is not None and age > 30:
        notes.append(f"Note: Posting is {age} days old")

    if signals["source_count"] >= 4:
        notes.append(
            f"Appears across {signals['source_count']} sources (possible perpetual repost)"
        )

    if signals["description_length"] < 200 and signals["description_length"] > 0:
        notes.append("Very short job description (possible placeholder)")

    if signals["filler_ratio"] > 3.0:
        notes.append("High ratio of generic filler language")

    if not signals["has_salary"]:
        notes.append("No salary information posted")

    signals["legitimacy_note"] = "; ".join(notes) if notes else ""

    return signals
```

#### 2C. Haiku prompt modification: `job_finder/web/haiku_scorer.py`

In `score_job_haiku()`, after building the user prompt and before calling `call_model()`:

1. Import and call `compute_legitimacy_signals(job_row_as_dict, conn)`.
2. If `signals["legitimacy_note"]` is non-empty, append a new section to the user prompt:

```
## Legitimacy Signals
{signals["legitimacy_note"]}

Factor these signals into your score. A job with multiple red flags (very old posting, no salary, vague description, appears on many sources) is likely a ghost posting and should be scored lower.
```

3. Add `legitimacy_flags` to the `HAIKU_SCHEMA` output:
   - `"legitimacy_flags"`: `{"type": "array", "items": {"type": "string"}}` — e.g., `["old_posting", "no_salary"]`

The existing `score` integer (0-100) already encodes fit. Legitimacy signals will naturally lower the score for ghost postings because the model is instructed to factor them in. No separate legitimacy score field is needed.

#### 2D. DB column (optional, for analytics)

Add migration 32 to `db_migrate.py`:

```python
# Migration 32: legitimacy signals for ghost job detection
[
    "ALTER TABLE jobs ADD COLUMN legitimacy_note TEXT DEFAULT NULL",
],
```

Store the computed `legitimacy_note` in `_score_and_persist` after Haiku scoring so we can track which jobs were flagged. This is analytics-only; it doesn't affect the scoring pipeline.

#### 2E. Tests: `tests/test_legitimacy_signals.py`

- Test age computation from ISO timestamps.
- Test source count extraction from JSON `sources` column.
- Test filler ratio calculation with known-filler JDs.
- Test that legitimacy_note is empty for healthy jobs.
- Test that old + no-salary + short-description triggers all flags.
- Test integration with `score_job_haiku` — mock the model call and verify the prompt includes legitimacy signals when flags are present.

---

## Recommendation 3: URL Liveness Checker

### Goal

Nightly scheduled job that verifies stored job URLs are still live. Marks expired listings to prevent wasted review time and Sonnet evaluation tokens.

### Implementation

#### 3A. New module: `job_finder/web/liveness_checker.py`

```python
"""URL liveness checker for stored job listings."""

from __future__ import annotations

import logging
import re
import time
from enum import Enum

import requests

logger = logging.getLogger(__name__)


class LivenessStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"
    ERROR = "error"


# Regex patterns for expired/closed job pages (ported from career-ops check-liveness.mjs).
# Case-insensitive. Checked against the first 5000 chars of the response body.
_EXPIRED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"this job is no longer available",
        r"this position has been filled",
        r"this job has been closed",
        r"this listing has expired",
        r"no longer accepting applications",
        r"position is no longer open",
        r"this job posting has been removed",
        r"sorry,? this job has already been filled",
        r"this role has been filled",
        r"this job has expired",
        r"the position you are looking for is no longer available",
        r"this requisition is no longer active",
        r"job not found",
        r"posting not found",
        r"this opening has been closed",
        # Greenhouse-specific
        r"There are no jobs matching your search",
        # Lever-specific
        r"This position is no longer available",
        # German
        r"Diese Stelle ist nicht mehr verf[uü]gbar",
        r"Diese Position wurde bereits besetzt",
        # French
        r"Cette offre n['']est plus disponible",
    ]
]

# Apply button patterns (presence = likely active)
_APPLY_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"apply\s+(now|for this|to this)",
        r'type="submit"[^>]*>.*?apply',
        r"submit.{0,20}application",
    ]
]

# Greenhouse error redirect pattern
_GREENHOUSE_ERROR_RE = re.compile(r"[?&]error=true")

# Minimum body length — pages with < 300 chars are likely redirects/stubs
_MIN_BODY_LENGTH = 300

_REQUEST_TIMEOUT = 15
_BATCH_DELAY = 0.5  # seconds between requests


def check_url_liveness(url: str) -> tuple[LivenessStatus, str]:
    """Check if a single job URL is still live.

    Returns:
        Tuple of (status, reason).
    """
    if _GREENHOUSE_ERROR_RE.search(url):
        return LivenessStatus.EXPIRED, "greenhouse_error_redirect"

    try:
        resp = requests.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobCannon/1.0)"},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return LivenessStatus.ERROR, str(e)[:200]

    # Hard 404/410
    if resp.status_code in (404, 410):
        return LivenessStatus.EXPIRED, f"http_{resp.status_code}"

    if resp.status_code == 403:
        return LivenessStatus.UNCERTAIN, "http_403_blocked"

    if resp.status_code >= 500:
        return LivenessStatus.ERROR, f"http_{resp.status_code}"

    body = resp.text[:5000]

    # Check body length
    if len(body.strip()) < _MIN_BODY_LENGTH:
        return LivenessStatus.EXPIRED, "empty_page"

    # Check expired patterns
    for pattern in _EXPIRED_PATTERNS:
        if pattern.search(body):
            return LivenessStatus.EXPIRED, f"pattern:{pattern.pattern[:50]}"

    # Check for apply button (positive signal)
    has_apply = any(p.search(body) for p in _APPLY_PATTERNS)
    if has_apply:
        return LivenessStatus.ACTIVE, "apply_button_found"

    # If we got a 200 with substantial content but no apply button and no
    # expired message, it's uncertain (could be a careers page redirect).
    if resp.status_code == 200 and len(body.strip()) > _MIN_BODY_LENGTH:
        return LivenessStatus.ACTIVE, "page_ok"

    return LivenessStatus.UNCERTAIN, "no_clear_signal"


def run_liveness_check(db_path: str, config: dict | None = None) -> dict:
    """Check liveness of active job URLs. Nightly scheduled job.

    Checks jobs that are:
    - pipeline_status in ('discovered', 'reviewing', 'applied')
    - Not already marked stale or archived
    - Have a source_url
    - Haven't been checked in the last 3 days

    Returns:
        Summary dict with counts.
    """
    from job_finder.web.db_helpers import standalone_connection

    cfg = (config or {}).get("liveness", {})
    batch_limit = cfg.get("batch_limit", 200)
    check_interval_days = cfg.get("check_interval_days", 3)

    summary = {"checked": 0, "active": 0, "expired": 0, "uncertain": 0, "errors": 0}

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, source_url, dedup_key
            FROM jobs
            WHERE pipeline_status IN ('discovered', 'reviewing', 'applied')
              AND is_stale = 0
              AND source_url IS NOT NULL
              AND source_url != ''
              AND (liveness_checked_at IS NULL
                   OR liveness_checked_at < datetime('now', ?))
            ORDER BY liveness_checked_at ASC NULLS FIRST
            LIMIT ?
            """,
            (f"-{check_interval_days} days", batch_limit),
        ).fetchall()

        logger.info("Liveness check: %d URLs to verify", len(rows))

        for row in rows:
            job_id = row["id"]
            url = row["source_url"]

            status, reason = check_url_liveness(url)
            summary["checked"] += 1
            summary[status.value] = summary.get(status.value, 0) + 1

            # Update job record
            conn.execute(
                """
                UPDATE jobs
                SET liveness_checked_at = datetime('now'),
                    liveness_status = ?,
                    liveness_reason = ?
                WHERE id = ?
                """,
                (status.value, reason, job_id),
            )

            # If expired, mark stale and create pipeline event
            if status == LivenessStatus.EXPIRED:
                conn.execute(
                    "UPDATE jobs SET is_stale = 1 WHERE id = ?",
                    (job_id,),
                )
                conn.execute(
                    """
                    INSERT INTO pipeline_events
                        (job_id, old_status, new_status, source, evidence, created_at)
                    VALUES (?, 'active', 'expired', 'liveness_checker', ?, datetime('now'))
                    """,
                    (job_id, reason),
                )
                logger.info(
                    "Expired: %s (%s)", row["dedup_key"], reason
                )

            conn.commit()
            time.sleep(_BATCH_DELAY)

    logger.info("Liveness check complete: %s", summary)
    return summary
```

#### 3B. DB migration 33: `job_finder/web/db_migrate.py`

Append to `MIGRATIONS` list:

```python
# Migration 33: liveness checker columns
[
    "ALTER TABLE jobs ADD COLUMN liveness_checked_at TEXT DEFAULT NULL",
    "ALTER TABLE jobs ADD COLUMN liveness_status TEXT DEFAULT NULL",
    "ALTER TABLE jobs ADD COLUMN liveness_reason TEXT DEFAULT NULL",
    "CREATE INDEX IF NOT EXISTS idx_jobs_liveness ON jobs(liveness_checked_at, pipeline_status)",
],
```

Note: if recommendation 2's migration is 32, this is 33. If they share a migration, adjust accordingly. Each recommendation's migration must be a separate entry in the `MIGRATIONS` list.

#### 3C. Scheduler registration: `job_finder/web/scheduler.py`

Add a new job using the `_make_tracked_job` pattern:

```python
def _import_liveness_check():
    from job_finder.web.liveness_checker import run_liveness_check
    return run_liveness_check

def _import_liveness_action():
    from job_finder.web.activity_tracker import ACTION_LIVENESS_CHECK
    return ACTION_LIVENESS_CHECK

_liveness_job = _make_tracked_job(
    app, "liveness_check",
    _import_liveness_check,
    _import_liveness_action,
    lambda r: {"checked": r.get("checked", 0), "expired": r.get("expired", 0)},
)
scheduler.add_job(
    _liveness_job,
    CronTrigger(hour=3, minute=0),
    id="liveness_check",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)
```

Schedule at 3:00 AM — after stale detection (2:00) and expiry check (2:30), before agentic backfill (3:30).

#### 3D. Activity tracker constant: `job_finder/web/activity_tracker.py`

Add `ACTION_LIVENESS_CHECK = "liveness_check"` alongside the existing action constants.

#### 3E. Config addition: `config.example.yaml`

```yaml
liveness:
  batch_limit: 200
  check_interval_days: 3
```

#### 3F. Tests: `tests/test_liveness_checker.py`

- Test `check_url_liveness()` with mocked responses for: 200 + apply button, 200 + expired text, 404, 410, 500, timeout, empty page, Greenhouse error redirect.
- Test `run_liveness_check()` with a test DB containing jobs in various states, verify only eligible jobs are checked.
- Test that expired jobs get `is_stale = 1` and a `pipeline_events` entry.
- Test batch limit is respected.
- Test `check_interval_days` prevents re-checking recent URLs.

---

## Recommendation 4: ATS Text Normalization in Resume Output

### Goal

Ensure generated resumes contain only ATS-safe ASCII characters. ATS parsers (Workday, Taleo, iCIMS) choke on Unicode smart quotes, em-dashes, and special whitespace, causing keyword matching failures.

### Implementation

#### 4A. Normalization function: add to `job_finder/web/docx_formatter.py`

This is the single serialization bottleneck — all text passes through `build_resume_docx()` regardless of generation path (single, multi, quick-apply). Insert at the top of the module:

```python
# ATS-safe character normalization map.
# ATS parsers (Workday, Taleo, iCIMS) choke on these Unicode characters
# during keyword extraction. Replace with ASCII equivalents.
_ATS_NORMALIZE_MAP = str.maketrans({
    "\u2018": "'",     # left single quote
    "\u2019": "'",     # right single quote (apostrophe)
    "\u201A": "'",     # single low-9 quote
    "\u201B": "'",     # single high-reversed-9 quote
    "\u201C": '"',     # left double quote
    "\u201D": '"',     # right double quote
    "\u201E": '"',     # double low-9 quote
    "\u201F": '"',     # double high-reversed-9 quote
    "\u2014": " - ",   # em dash -> space-hyphen-space
    "\u2013": "-",     # en dash -> hyphen
    "\u2026": "...",   # ellipsis
    "\u00A0": " ",     # non-breaking space
    "\u200B": "",      # zero-width space (remove)
    "\u200C": "",      # zero-width non-joiner (remove)
    "\u200D": "",      # zero-width joiner (remove)
    "\uFEFF": "",      # BOM / zero-width no-break space (remove)
    "\u2022": "-",     # bullet -> hyphen (for inline lists)
    "\u25CF": "-",     # black circle bullet
    "\u25CB": "-",     # white circle bullet
    "\u00B7": "-",     # middle dot
    "\u2023": "-",     # triangular bullet
    "\u00AB": '"',     # left guillemet
    "\u00BB": '"',     # right guillemet
    "\u2039": "'",     # single left angle quote
    "\u203A": "'",     # single right angle quote
})


def _normalize_for_ats(text: str) -> str:
    """Replace Unicode characters that break ATS keyword matching."""
    if not text:
        return text
    return text.translate(_ATS_NORMALIZE_MAP)
```

#### 4B. Apply normalization in `build_resume_docx()`

Wrap every string extraction from `resume_data` through `_normalize_for_ats()`. The function is called in `build_resume_docx()` at every point where text from the `resume_data` dict is written into the document.

Specifically, find every call to `doc.add_paragraph(...)` and `para.add_run(...)` where the argument comes from `resume_data` (not from literal strings like "EXPERIENCE" headings). Wrap the argument:

```python
# Before:
doc.add_paragraph(resume_data["contact"]["name"], style="Heading 1")

# After:
doc.add_paragraph(_normalize_for_ats(resume_data["contact"]["name"]), style="Heading 1")
```

The safest approach is to normalize the entire `resume_data` dict recursively before processing:

```python
def _normalize_resume_data(data):
    """Recursively normalize all strings in resume_data for ATS compatibility."""
    if isinstance(data, str):
        return _normalize_for_ats(data)
    if isinstance(data, list):
        return [_normalize_resume_data(item) for item in data]
    if isinstance(data, dict):
        return {k: _normalize_resume_data(v) for k, v in data.items()}
    return data
```

Call this at the TOP of `build_resume_docx()`, before any field access:

```python
def build_resume_docx(resume_data: dict) -> io.BytesIO:
    resume_data = _normalize_resume_data(resume_data)
    # ... rest of function unchanged
```

This is a single insertion point with zero risk of missing a field.

#### 4C. Promote em-dash validation to error severity: `job_finder/web/resume_validator.py`

The current validator flags em dashes as `severity="warning"` which means they're never auto-fixed. Since we now normalize at the DOCX level, this is belt-and-suspenders — but change the severity to `"error"` and add smart quotes:

In `validate_resume()`, find the em-dash check and:
1. Change severity from `"warning"` to `"error"`.
2. Add checks for smart quotes (`\u201C`, `\u201D`, `\u2018`, `\u2019`).

In `fix_resume_violations()`, add a fix handler that replaces these characters using the same `_ATS_NORMALIZE_MAP`. This catches the issue even before DOCX formatting.

#### 4D. Tests: `tests/test_ats_normalization.py`

- Test `_normalize_for_ats()` with every character in `_ATS_NORMALIZE_MAP`.
- Test `_normalize_resume_data()` recursively normalizes nested dicts, lists, and strings.
- Test that `build_resume_docx()` output contains no Unicode from the map (read the DOCX XML to verify).
- Test round-trip: feed a `resume_data` dict with smart quotes, em-dashes, and zero-width chars through `build_resume_docx()`, then read the DOCX and verify only ASCII equivalents appear.
- Test that non-Latin characters (accented names like "José García") are NOT stripped — only the ATS-hostile characters in the map.

---

## Recommendation 5: Structured Rejection Pattern Analysis

### Goal

Build a structured analyzer that categorizes rejections by archetype, gap type, and company tier to surface systematic targeting blind spots. This creates a feedback loop: past rejection patterns inform future job targeting and resume strategy.

### Implementation

#### 5A. Current state

`rejection_analyzer.py` already runs a weekly Opus batch that produces cross-rejection pattern analysis stored in `rejection_reports` as JSON blobs. The current output has four factors: `profile_match`, `resume_tailoring`, `competitiveness`, `timing`.

The enhancement adds **mechanical pattern extraction** (no LLM needed) that runs on every rejection, producing structured data for trend analysis.

#### 5B. New module: `job_finder/web/rejection_patterns.py`

```python
"""Mechanical rejection pattern extraction and trend analysis.

Extracts structured patterns from rejected jobs without LLM calls.
Complements the existing Opus-based rejection_analyzer.py with
zero-cost mechanical analysis.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class RejectionPattern:
    """Structured rejection analysis for a single job."""
    job_id: int
    dedup_key: str
    company: str
    title: str
    haiku_score: int | None
    sonnet_score: int | None

    # Classification dimensions
    seniority: str = ""          # junior/mid/senior/staff/principal/exec
    domain: str = ""             # eng/data/ml/product/design/other
    has_salary: bool = False
    salary_meets_floor: bool = False
    location_match: str = ""     # remote/target/other
    title_fit: str = ""          # strong/partial/weak (from haiku)
    rejection_stage: str = ""    # discovered/applied/interviewing (where it died)
    days_in_pipeline: int = 0
    company_size: str = ""       # startup/small/mid-size/large
    ats_platform: str = ""       # lever/greenhouse/ashby/unknown


@dataclass
class PatternReport:
    """Aggregate rejection pattern analysis."""
    period_days: int
    total_rejections: int
    patterns: list[RejectionPattern] = field(default_factory=list)

    # Aggregate stats
    rejection_by_stage: dict[str, int] = field(default_factory=dict)
    rejection_by_seniority: dict[str, int] = field(default_factory=dict)
    rejection_by_domain: dict[str, int] = field(default_factory=dict)
    rejection_by_company_size: dict[str, int] = field(default_factory=dict)
    rejection_by_title_fit: dict[str, int] = field(default_factory=dict)
    rejection_by_location: dict[str, int] = field(default_factory=dict)
    avg_days_in_pipeline: float = 0.0
    salary_floor_miss_rate: float = 0.0
    score_distribution: dict[str, int] = field(default_factory=dict)
    top_rejected_companies: list[tuple[str, int]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("patterns")  # Don't serialize individual patterns
        return d


# Seniority detection from title keywords
_SENIORITY_KEYWORDS = {
    "intern": "intern",
    "junior": "junior", "jr": "junior", "entry": "junior",
    "mid": "mid", "intermediate": "mid",
    "senior": "senior", "sr": "senior", "lead": "senior",
    "staff": "staff",
    "principal": "principal",
    "director": "exec", "vp": "exec", "head of": "exec",
    "chief": "exec", "cto": "exec", "ceo": "exec",
}

_DOMAIN_KEYWORDS = {
    "engineer": "eng", "developer": "eng", "swe": "eng", "backend": "eng",
    "frontend": "eng", "fullstack": "eng", "devops": "eng", "sre": "eng",
    "data scientist": "data", "data analyst": "data", "analytics": "data",
    "machine learning": "ml", "ml ": "ml", "ai ": "ml", "nlp": "ml",
    "product manager": "product", "product owner": "product",
    "designer": "design", "ux": "design", "ui": "design",
}


def _detect_seniority(title: str) -> str:
    title_lower = title.lower()
    for keyword, level in _SENIORITY_KEYWORDS.items():
        if keyword in title_lower:
            return level
    return "unknown"


def _detect_domain(title: str) -> str:
    title_lower = title.lower()
    for keyword, domain in _DOMAIN_KEYWORDS.items():
        if keyword in title_lower:
            return domain
    return "other"


def extract_rejection_patterns(db_path: str, config: dict | None = None) -> PatternReport:
    """Extract structured patterns from all rejections in the last N days.

    This is a zero-LLM-cost analysis that runs mechanically.
    """
    from job_finder.web.db_helpers import standalone_connection

    cfg = (config or {}).get("rejection_patterns", {})
    period_days = cfg.get("period_days", 90)

    report = PatternReport(period_days=period_days, total_rejections=0)

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.dedup_key, j.company, j.title,
                   j.haiku_score, j.sonnet_score,
                   j.salary_min, j.pipeline_status,
                   j.first_seen_at, j.created_at,
                   j.haiku_title_fit, j.haiku_location_fit,
                   j.haiku_salary_meets_floor,
                   c.company_size, c.ats_platform
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            WHERE j.pipeline_status IN ('rejected', 'archived')
              AND j.created_at > datetime('now', ?)
            ORDER BY j.created_at DESC
            """,
            (f"-{period_days} days",),
        ).fetchall()

        report.total_rejections = len(rows)
        if not rows:
            return report

        stage_counter: Counter = Counter()
        seniority_counter: Counter = Counter()
        domain_counter: Counter = Counter()
        size_counter: Counter = Counter()
        fit_counter: Counter = Counter()
        location_counter: Counter = Counter()
        company_counter: Counter = Counter()
        score_buckets: Counter = Counter()
        total_days = 0
        salary_checks = 0
        salary_misses = 0

        min_salary = (config or {}).get("profile", {}).get("min_salary", 0)

        for row in rows:
            pattern = RejectionPattern(
                job_id=row["id"],
                dedup_key=row["dedup_key"],
                company=row["company"],
                title=row["title"],
                haiku_score=row["haiku_score"],
                sonnet_score=row["sonnet_score"],
                seniority=_detect_seniority(row["title"]),
                domain=_detect_domain(row["title"]),
                has_salary=row["salary_min"] is not None,
                salary_meets_floor=(
                    row["salary_min"] is not None
                    and min_salary > 0
                    and row["salary_min"] >= min_salary
                ),
                location_match=row["haiku_location_fit"] or "unknown",
                title_fit=row["haiku_title_fit"] or "unknown",
                rejection_stage=row["pipeline_status"],
                company_size=row["company_size"] or "unknown",
                ats_platform=row["ats_platform"] or "unknown",
            )

            # Pipeline duration
            if row["first_seen_at"] and row["created_at"]:
                from datetime import datetime
                try:
                    first = datetime.fromisoformat(
                        row["first_seen_at"].replace("Z", "+00:00")
                    )
                    created = datetime.fromisoformat(
                        row["created_at"].replace("Z", "+00:00")
                    )
                    pattern.days_in_pipeline = max(0, (created - first).days)
                except (ValueError, TypeError):
                    pass

            report.patterns.append(pattern)

            # Aggregate
            stage_counter[pattern.rejection_stage] += 1
            seniority_counter[pattern.seniority] += 1
            domain_counter[pattern.domain] += 1
            size_counter[pattern.company_size] += 1
            fit_counter[pattern.title_fit] += 1
            location_counter[pattern.location_match] += 1
            company_counter[pattern.company] += 1
            total_days += pattern.days_in_pipeline

            if pattern.has_salary:
                salary_checks += 1
                if not pattern.salary_meets_floor:
                    salary_misses += 1

            # Score buckets
            score = pattern.sonnet_score or pattern.haiku_score or 0
            if score >= 80:
                score_buckets["80-100"] += 1
            elif score >= 60:
                score_buckets["60-79"] += 1
            elif score >= 40:
                score_buckets["40-59"] += 1
            else:
                score_buckets["0-39"] += 1

        report.rejection_by_stage = dict(stage_counter)
        report.rejection_by_seniority = dict(seniority_counter)
        report.rejection_by_domain = dict(domain_counter)
        report.rejection_by_company_size = dict(size_counter)
        report.rejection_by_title_fit = dict(fit_counter)
        report.rejection_by_location = dict(location_counter)
        report.avg_days_in_pipeline = round(total_days / len(rows), 1)
        report.salary_floor_miss_rate = (
            round(salary_misses / salary_checks, 2) if salary_checks else 0.0
        )
        report.score_distribution = dict(score_buckets)
        report.top_rejected_companies = company_counter.most_common(10)

        # Identify blockers (systematic issues)
        blockers = []
        if seniority_counter.get("junior", 0) > report.total_rejections * 0.3:
            blockers.append("Over 30% of rejections are junior-level roles — consider raising seniority filter")
        if location_counter.get("other", 0) > report.total_rejections * 0.3:
            blockers.append("Over 30% of rejections are non-target locations — tighten location filter")
        if fit_counter.get("weak", 0) > report.total_rejections * 0.3:
            blockers.append("Over 30% of rejections have weak title fit — review target title keywords")
        if report.salary_floor_miss_rate > 0.4:
            blockers.append(f"Salary floor miss rate is {report.salary_floor_miss_rate:.0%} — many jobs below minimum")
        report.blockers = blockers

    return report


def run_rejection_pattern_analysis(db_path: str, config: dict | None = None) -> dict:
    """Scheduled entry point. Computes patterns and stores result."""
    from job_finder.web.db_helpers import standalone_connection

    report = extract_rejection_patterns(db_path, config)

    # Store the report in the DB for dashboard access
    with standalone_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rejection_pattern_reports
                (report_json, period_days, total_rejections, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (json.dumps(report.to_dict()), report.period_days, report.total_rejections),
        )
        conn.commit()

    logger.info(
        "Rejection pattern analysis: %d rejections, %d blockers identified",
        report.total_rejections,
        len(report.blockers),
    )
    return report.to_dict()
```

#### 5C. DB migration 34: `job_finder/web/db_migrate.py`

```python
# Migration 34: rejection pattern reports table
[
    """CREATE TABLE IF NOT EXISTS rejection_pattern_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_json TEXT NOT NULL,
        period_days INTEGER NOT NULL,
        total_rejections INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
],
```

Note: If this is combined with earlier migrations in a single implementation pass, adjust the migration index. The migration list is 0-indexed and `PRAGMA user_version` is 1-indexed.

#### 5D. Scheduler registration: `job_finder/web/scheduler.py`

Add alongside the existing `rejection_analysis` job (which runs Monday 3:00 AM):

```python
def _import_rejection_patterns():
    from job_finder.web.rejection_patterns import run_rejection_pattern_analysis
    return run_rejection_pattern_analysis

# Run weekly on Tuesday 3:00 AM (day after Opus rejection analysis)
# so it can incorporate new rejection data from the Opus pass.
_rejection_patterns_job = _make_simple_job(
    app, "rejection_patterns", _import_rejection_patterns
)
scheduler.add_job(
    _rejection_patterns_job,
    CronTrigger(day_of_week="tue", hour=3, minute=0),
    id="rejection_patterns",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)
```

#### 5E. Dashboard integration (optional, lower priority)

Add a route in `job_finder/web/blueprints/dashboard.py` or a new blueprint that:
1. Fetches the latest `rejection_pattern_reports` row.
2. Renders a template showing: blocker alerts, score distribution chart, rejection-by-stage funnel, top rejected companies, seniority breakdown.

This is presentation-only and can be deferred to a separate session.

#### 5F. Tests: `tests/test_rejection_patterns.py`

- Test `_detect_seniority()` with known titles: "Junior Engineer" -> "junior", "Staff ML Engineer" -> "staff", "VP Engineering" -> "exec".
- Test `_detect_domain()` with known titles: "Data Scientist" -> "data", "Backend Engineer" -> "eng", "Product Manager" -> "product".
- Test `extract_rejection_patterns()` with a test DB containing rejected jobs with various characteristics. Verify aggregate counters match manual counts.
- Test blocker detection thresholds (>30% junior triggers blocker).
- Test `run_rejection_pattern_analysis()` stores report in DB.
- Test with empty rejection set (no crash, zero counts).

---

## Execution Order

1. **Recommendation 4 (ATS Normalization)** — smallest scope, zero new infrastructure, immediate value. ~30 min.
2. **Recommendation 2 (Ghost Job Legitimacy)** — one new module + prompt modification. ~1 hr.
3. **Recommendation 3 (URL Liveness)** — one new module + migration + scheduler. ~1.5 hr.
4. **Recommendation 1 (Portal Search)** — new source module + pipeline integration. ~2 hr.
5. **Recommendation 5 (Rejection Patterns)** — new module + migration + scheduler. ~1.5 hr.

All migrations must be added as separate entries at the end of the `MIGRATIONS` list in `db_migrate.py`, in the order they're implemented. Each gets the next sequential version number after the current last migration (currently 31, so new ones are 32, 33, 34 depending on how many are needed).

## Dependency Graph

```
Rec 4 (ATS Normalization)  ─── independent
Rec 2 (Ghost Job)           ─── independent (migration 32)
Rec 3 (URL Liveness)        ─── independent (migration 33)
Rec 1 (Portal Search)       ─── depends on working SerpAPI/Thordata (already exists)
Rec 5 (Rejection Patterns)  ─── independent (migration 34), benefits from Rec 2 data
```

No recommendation blocks another. They can be implemented in any order, but the execution order above optimizes for risk (smallest changes first) and incremental value.
