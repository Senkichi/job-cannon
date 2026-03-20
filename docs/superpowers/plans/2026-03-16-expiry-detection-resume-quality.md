# Job Expiry Detection & Resume Quality Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automated job expiry detection that archives dead listings, and upgrade resume generation with guideline-driven prompts and post-generation validation.

**Architecture:** Two independent features sharing one DB migration. Feature 1 adds a new `expiry_checker.py` module with a tiered signal cascade (ATS API → careers page → SerpAPI) run nightly by APScheduler. Feature 2 enriches the resume generator's system prompt with distilled guidelines from `docs/resume_generation_guidelines.md`, adds a Sonnet-powered post-generation validator, and expands the style guide schema.

**Tech Stack:** Python 3.13, Flask, SQLite, APScheduler, requests, Anthropic API (Sonnet), SerpAPI

**Spec:** `docs/superpowers/specs/2026-03-16-expiry-detection-resume-quality-design.md`

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `job_finder/web/expiry_checker.py` | Signal cascade (ATS API, careers page, SerpAPI) to detect expired listings and auto-archive |
| `job_finder/web/resume_validator.py` | Sonnet quality audit + conditional auto-fix for generated resumes |
| `tests/test_expiry_checker.py` | Tests for expiry checker: signal functions, cascade logic, batch runner |
| `tests/test_resume_validator.py` | Tests for resume validator: audit pass, fix pass, integration with background flow |

### Modified Files

| File | Change |
|------|--------|
| `job_finder/db.py` | Add optional `evidence` param to `update_pipeline_status()` |
| `job_finder/web/db_migrate.py` | Migration 14: `expiry_checked_at` column + index, `validation_report` column |
| `job_finder/web/activity_tracker.py` | Add `ACTION_SCHEDULED_EXPIRY_CHECK` constant |
| `job_finder/web/scheduler.py` | Register nightly expiry check job at 2:30 AM |
| `job_finder/web/resume_generator.py` | `_RESUME_GUIDELINES` constant, validator hook in `_generate_resume_background()` |
| `job_finder/web/resume_style_guide.py` | Expanded schema (9 new fields), `migrate_style_guide()` function, updated directives builder |
| `job_finder/web/templates/jobs/_resume_section.html` | Show validation report badge on resume history entries |
| `tests/test_scheduler.py` | Add test for expiry_check job registration |
| `tests/test_resume_style_guide.py` | Tests for expanded schema and migration function |

---

## Chunk 1: Job Expiry Detection

### Task 1: Database Migration & `update_pipeline_status` Evidence Parameter

**Files:**
- Modify: `job_finder/db.py:440-477` (add `evidence` parameter)
- Modify: `job_finder/web/db_migrate.py` (add Migration 14)
- Modify: `tests/test_db.py` (add evidence test)
- Modify: `tests/test_migration.py` (add Migration 14 test)

- [ ] **Step 1: Write failing test for `update_pipeline_status` with evidence**

In `tests/test_db.py`, add a test in the existing test class that exercises `update_pipeline_status`:

```python
def test_update_pipeline_status_writes_evidence(self, migrated_db):
    """update_pipeline_status writes evidence string to pipeline_events."""
    path, conn = migrated_db
    from job_finder.db import update_pipeline_status

    # Insert a job to update
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, pipeline_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test|evidence|job", "Test", "TestCo", "Remote", "2026-03-01", "2026-03-01", "discovered"),
    )
    conn.commit()

    update_pipeline_status(conn, "test|evidence|job", "archived", source="expiry_check", evidence="lever_api 404")

    event = conn.execute(
        "SELECT evidence FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC LIMIT 1",
        ("test|evidence|job",),
    ).fetchone()
    assert event is not None
    assert event["evidence"] == "lever_api 404"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_update_pipeline_status_writes_evidence -v`
Expected: FAIL — `update_pipeline_status()` does not accept `evidence` keyword.

- [ ] **Step 3: Add `evidence` parameter to `update_pipeline_status`**

In `job_finder/db.py`, modify the function signature and INSERT:

```python
def update_pipeline_status(
    conn: sqlite3.Connection,
    dedup_key: str,
    new_status: str,
    source: str = "manual",
    evidence: str = "",
) -> None:
    """Update a job's pipeline_status and log a pipeline_events record.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        new_status: The target pipeline status to move the job to.
        source: Who triggered the move ('manual', 'email', 'ai', etc.).
        evidence: Optional evidence string for the pipeline_events record
                  (e.g., "lever_api 404", "serpapi no_match").
    """
    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return

    from_status = row["pipeline_status"]
    if from_status == new_status:
        return

    now = datetime.now().isoformat()

    conn.execute(
        "UPDATE jobs SET pipeline_status = ? WHERE dedup_key = ?",
        (new_status, dedup_key),
    )
    conn.execute(
        """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (dedup_key, from_status, new_status, now, source, evidence),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::test_update_pipeline_status_writes_evidence -v`
Expected: PASS

- [ ] **Step 5: Write failing test for Migration 14 schema changes**

In `tests/test_migration.py`, add:

```python
class TestMigration14:
    """Migration 14 adds expiry_checked_at to jobs and validation_report to resume_generations."""

    def test_jobs_has_expiry_checked_at_column(self, migrated_db_class):
        _, conn = migrated_db_class
        columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        assert "expiry_checked_at" in columns

    def test_expiry_checked_at_index_exists(self, migrated_db_class):
        _, conn = migrated_db_class
        indexes = [row[1] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()]
        assert "idx_jobs_expiry_checked_at" in indexes

    def test_resume_generations_has_validation_report_column(self, migrated_db_class):
        _, conn = migrated_db_class
        columns = [row[1] for row in conn.execute("PRAGMA table_info(resume_generations)").fetchall()]
        assert "validation_report" in columns
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_migration.py::TestMigration14 -v`
Expected: FAIL — columns don't exist yet.

- [ ] **Step 7: Add Migration 14 to `db_migrate.py`**

Append to the `MIGRATIONS` list in `job_finder/web/db_migrate.py`:

```python
    # Migration 14: Add expiry_checked_at to jobs, validation_report to resume_generations
    [
        "ALTER TABLE jobs ADD COLUMN expiry_checked_at TEXT DEFAULT NULL",
        "CREATE INDEX IF NOT EXISTS idx_jobs_expiry_checked_at ON jobs(expiry_checked_at)",
        "ALTER TABLE resume_generations ADD COLUMN validation_report TEXT DEFAULT NULL",
    ],
```

- [ ] **Step 8: Run migration tests to verify they pass**

Run: `pytest tests/test_migration.py::TestMigration14 -v`
Expected: PASS

- [ ] **Step 9: Update existing migration count assertion**

In `tests/test_migration.py:408`, the existing `test_migration_count_is_thirteen` asserts `len(MIGRATIONS) == 13`. Update it to `14`:

```python
def test_migration_count_is_thirteen():
    """MIGRATIONS list has the expected number of entries."""
    from job_finder.web.db_migrate import MIGRATIONS
    assert len(MIGRATIONS) == 14
```

- [ ] **Step 10: Run full test suite to verify no regressions**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 11: Commit**

```bash
git add job_finder/db.py job_finder/web/db_migrate.py tests/test_db.py tests/test_migration.py
git commit -m "feat: add Migration 14 (expiry_checked_at, validation_report) and evidence param to update_pipeline_status"
```

---

### Task 2: Expiry Checker — Signal Functions

**Files:**
- Create: `job_finder/web/expiry_checker.py`
- Create: `tests/test_expiry_checker.py`

- [ ] **Step 1: Write failing tests for posting ID extraction**

Create `tests/test_expiry_checker.py`:

```python
"""Tests for job expiry detection signal cascade."""

import json
import sqlite3
import tempfile
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pytest
import requests


class TestExtractPostingId:
    """_extract_posting_id extracts individual posting IDs from ATS URLs."""

    def test_lever_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.lever.co/acme-corp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "lever") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_greenhouse_numeric_id(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://boards.greenhouse.io/acme/jobs/4567890"
        assert _extract_posting_id(url, "greenhouse") == "4567890"

    def test_ashby_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.ashbyhq.com/AcmeCorp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "ashby") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_returns_none_for_non_matching_url(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://www.linkedin.com/jobs/view/12345/"
        assert _extract_posting_id(url, "lever") is None

    def test_returns_none_for_unknown_platform(self):
        from job_finder.web.expiry_checker import _extract_posting_id
        url = "https://jobs.lever.co/acme/abc123"
        assert _extract_posting_id(url, "unknown") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestExtractPostingId -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create `expiry_checker.py` with posting ID extraction**

Create `job_finder/web/expiry_checker.py`:

```python
"""Job expiry detection via tiered signal cascade.

Provides:
    _extract_posting_id   -- Extract individual posting ID from ATS URL
    _check_ats_api        -- Signal 1: ATS API liveness check
    _check_careers_page   -- Signal 2: Company careers page title search
    _check_serpapi         -- Signal 3: SerpAPI re-search fallback
    run_expiry_check      -- Nightly batch runner (APScheduler entry point)

Architecture:
- Thread-safe: creates own sqlite3 connection (same pattern as stale_detector.py)
- Signal cascade short-circuits on first definitive answer (expired/live)
- Only targets jobs in discovered/reviewing status
- Consecutive careers page failures tracked in-memory (resets on restart)
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEOUT = 10  # seconds for HTTP requests
_INTER_REQUEST_DELAY = 2  # seconds between HTTP requests

# Signal result constants
EXPIRED = "expired"
LIVE = "live"
INCONCLUSIVE = "inconclusive"

# ---------------------------------------------------------------------------
# Posting ID extraction (Signal 1 prerequisite)
# ---------------------------------------------------------------------------

_LEVER_POSTING_RE = re.compile(
    r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE
)
_GREENHOUSE_POSTING_RE = re.compile(
    r"boards\.greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE
)
_ASHBY_POSTING_RE = re.compile(
    r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"
    # No IGNORECASE — Ashby slugs are case-sensitive
)

_POSTING_PATTERNS = {
    "lever": _LEVER_POSTING_RE,
    "greenhouse": _GREENHOUSE_POSTING_RE,
    "ashby": _ASHBY_POSTING_RE,
}


def _extract_posting_id(url: str, ats_platform: str) -> Optional[str]:
    """Extract the individual posting ID from an ATS URL.

    Args:
        url: A job source URL string.
        ats_platform: One of 'lever', 'greenhouse', 'ashby'.

    Returns:
        The posting ID string, or None if the URL doesn't match the platform pattern.
    """
    pattern = _POSTING_PATTERNS.get(ats_platform)
    if pattern is None:
        return None
    match = pattern.search(url)
    return match.group(1) if match else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestExtractPostingId -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for ATS API signal**

Add to `tests/test_expiry_checker.py`:

```python
class TestCheckAtsApi:
    """Signal 1: ATS API liveness check."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, EXPIRED
        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == EXPIRED
        mock_get.assert_called_once()
        assert "api.lever.co" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_200_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, LIVE
        mock_get.return_value = MagicMock(status_code=200)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_greenhouse_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, EXPIRED
        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "12345", "greenhouse")
        assert result == EXPIRED
        assert "boards-api.greenhouse.io" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_network_error_returns_inconclusive(self, mock_get):
        from job_finder.web.expiry_checker import _check_ats_api, INCONCLUSIVE
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == INCONCLUSIVE

    def test_unknown_platform_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_ats_api, INCONCLUSIVE
        result = _check_ats_api("acme", "abc-123", "unknown")
        assert result == INCONCLUSIVE
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestCheckAtsApi -v`
Expected: FAIL — `_check_ats_api` does not exist.

- [ ] **Step 7: Implement `_check_ats_api`**

Add to `job_finder/web/expiry_checker.py`:

```python
# ---------------------------------------------------------------------------
# Signal 1: ATS API Check
# ---------------------------------------------------------------------------

def _check_ats_api(slug: str, posting_id: str, ats_platform: str) -> str:
    """Check if a specific job posting is still live via ATS API.

    Args:
        slug: Company's ATS slug (e.g., 'acme-corp').
        posting_id: Individual posting ID extracted from URL.
        ats_platform: One of 'lever', 'greenhouse', 'ashby'.

    Returns:
        EXPIRED if the posting returns 404/410.
        LIVE if the posting returns 200.
        INCONCLUSIVE on network error or unknown platform.
    """
    if ats_platform == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
    elif ats_platform == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
    elif ats_platform == "ashby":
        # Ashby's GraphQL API is complex; check the public job board URL instead
        url = f"https://jobs.ashbyhq.com/{slug}/{posting_id}"
    else:
        return INCONCLUSIVE

    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code in (404, 410):
            return EXPIRED
        if resp.status_code == 200:
            return LIVE
        # Other status codes (403, 500, etc.) are inconclusive
        return INCONCLUSIVE
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return INCONCLUSIVE
    except Exception as e:
        logger.warning("_check_ats_api: unexpected error for %s/%s: %s", slug, posting_id, e)
        return INCONCLUSIVE
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestCheckAtsApi -v`
Expected: PASS

- [ ] **Step 9: Write failing tests for careers page signal**

Add to `tests/test_expiry_checker.py`:

```python
class TestCheckCareersPage:
    """Signal 2: Company careers page title search."""

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_found_returns_live(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import _check_careers_page, LIVE
        mock_find.return_value = "https://acme.com/careers"
        # scrape_careers_page returns list of dicts with 'title' and 'url' keys
        mock_scrape.return_value = [{"title": "Senior Data Scientist", "url": "https://acme.com/careers/123"}]
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_not_found_returns_inconclusive(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        mock_find.return_value = "https://acme.com/careers"
        mock_scrape.return_value = [{"title": "Backend Engineer", "url": "https://acme.com/careers/456"}]
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_no_careers_url_returns_inconclusive(self, mock_find):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        mock_find.return_value = None
        result = _check_careers_page("https://acme.com", "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE

    def test_no_homepage_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_careers_page, INCONCLUSIVE
        result = _check_careers_page(None, "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestCheckCareersPage -v`
Expected: FAIL — `_check_careers_page` does not exist.

- [ ] **Step 11: Implement `_check_careers_page`**

Add to `job_finder/web/expiry_checker.py`:

```python
# ---------------------------------------------------------------------------
# Signal 2: Company Careers Page Check
# ---------------------------------------------------------------------------

# Lazy imports for careers scraper (may not be available in tests)
try:
    from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
except ImportError:
    find_careers_url = None  # type: ignore[assignment]
    scrape_careers_page = None  # type: ignore[assignment]

def _check_careers_page(
    homepage_url: Optional[str],
    job_title: str,
    target_titles: list[str],
    exclusions: list[str],
) -> str:
    """Check if a job title appears on the company's careers page.

    Args:
        homepage_url: Company homepage URL (from companies table). None if unknown.
        job_title: The job title to search for.
        target_titles: Title keywords for matching (from config).
        exclusions: Title exclusion keywords (from config).

    Returns:
        LIVE if the job title is found on the careers page.
        INCONCLUSIVE if no careers page, page unreachable, or title not found
        (title absence is a weak signal — the page may not list all roles).
    """
    if not homepage_url:
        return INCONCLUSIVE

    if find_careers_url is None or scrape_careers_page is None:
        logger.debug("_check_careers_page: careers_scraper not available")
        return INCONCLUSIVE

    try:
        careers_url = find_careers_url(homepage_url)
        if not careers_url:
            return INCONCLUSIVE

        results = scrape_careers_page(careers_url, target_titles, exclusions)
        # Check if any result title is a close match to our job title
        # scrape_careers_page returns list[dict] with 'title' and 'url' keys
        job_title_lower = job_title.lower()
        for item in results:
            result_title = item.get("title", "").lower()
            if job_title_lower in result_title or result_title in job_title_lower:
                return LIVE

        # Title not found — but this is a weak signal (JS-rendered pages, etc.)
        return INCONCLUSIVE

    except Exception as e:
        logger.debug("_check_careers_page: error checking %s: %s", homepage_url, e)
        # Track failure for backoff (caller reads _careers_failure_counts)
        return INCONCLUSIVE
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestCheckCareersPage -v`
Expected: PASS

- [ ] **Step 13: Write failing tests for SerpAPI signal**

Add to `tests/test_expiry_checker.py`:

```python
class TestCheckSerpapi:
    """Signal 3: SerpAPI re-search fallback."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_no_match_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import _check_serpapi, EXPIRED
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"jobs_results": [
                {"title": "Backend Engineer", "company_name": "OtherCo"},
            ]}),
        )
        config = {"sources": {"serpapi": {"enabled": True, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_match_found_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import _check_serpapi, LIVE
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"jobs_results": [
                {"title": "Senior Data Scientist", "company_name": "Acme Corp"},
            ]}),
        )
        config = {"sources": {"serpapi": {"enabled": True, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == LIVE

    def test_serpapi_disabled_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_serpapi, INCONCLUSIVE
        config = {"sources": {"serpapi": {"enabled": False, "api_key": "test-key"}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == INCONCLUSIVE

    def test_serpapi_no_key_returns_inconclusive(self):
        from job_finder.web.expiry_checker import _check_serpapi, INCONCLUSIVE
        config = {"sources": {"serpapi": {"enabled": True, "api_key": ""}}}
        result = _check_serpapi("Senior Data Scientist", "Acme Corp", config)
        assert result == INCONCLUSIVE
```

- [ ] **Step 14: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestCheckSerpapi -v`
Expected: FAIL — `_check_serpapi` does not exist.

- [ ] **Step 15: Implement `_check_serpapi`**

Add to `job_finder/web/expiry_checker.py`:

```python
# ---------------------------------------------------------------------------
# Signal 3: SerpAPI Fallback
# ---------------------------------------------------------------------------

_SERPAPI_BASE_URL = "https://serpapi.com/search.json"


def _check_serpapi(job_title: str, company_name: str, config: dict) -> str:
    """Re-search for a job via SerpAPI google_jobs engine.

    Args:
        job_title: The job title to search for.
        company_name: The company name.
        config: Application config dict (reads sources.serpapi.enabled and api_key).

    Returns:
        LIVE if a matching result is found.
        EXPIRED if no matching result in the first batch.
        INCONCLUSIVE if SerpAPI is disabled, has no key, or network error.
    """
    serpapi_config = config.get("sources", {}).get("serpapi", {})
    if not serpapi_config.get("enabled", False):
        return INCONCLUSIVE
    api_key = serpapi_config.get("api_key", "")
    if not api_key:
        return INCONCLUSIVE

    try:
        params = {
            "engine": "google_jobs",
            "q": f'"{job_title}" "{company_name}"',
            "api_key": api_key,
            "hl": "en",
        }
        resp = requests.get(_SERPAPI_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        title_lower = job_title.lower()
        company_lower = company_name.lower()

        for result in data.get("jobs_results", []):
            result_title = result.get("title", "").lower()
            result_company = result.get("company_name", "").lower()
            # Match: company name appears in result AND substantial title overlap
            if company_lower in result_company and (
                title_lower in result_title or result_title in title_lower
            ):
                return LIVE

        return EXPIRED

    except Exception as e:
        logger.warning("_check_serpapi: error searching for '%s' at '%s': %s", job_title, company_name, e)
        return INCONCLUSIVE
```

- [ ] **Step 16: Run tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestCheckSerpapi -v`
Expected: PASS

- [ ] **Step 17: Commit signal functions**

```bash
git add job_finder/web/expiry_checker.py tests/test_expiry_checker.py
git commit -m "feat: add expiry checker signal functions (ATS API, careers page, SerpAPI)"
```

---

### Task 3: Expiry Checker — Batch Runner & Cascade Orchestration

**Files:**
- Modify: `job_finder/web/expiry_checker.py` (add `run_expiry_check`)
- Modify: `tests/test_expiry_checker.py` (add cascade and batch tests)

- [ ] **Step 1: Write failing test for signal cascade short-circuit logic**

Add to `tests/test_expiry_checker.py`:

```python
class TestSignalCascade:
    """_check_job_expiry runs signals in order and short-circuits."""

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_ats_expired_short_circuits(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, EXPIRED
        mock_ats.return_value = EXPIRED
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == EXPIRED
        assert "lever" in evidence.lower() or "ats" in evidence.lower()
        mock_careers.assert_not_called()
        mock_serpapi.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_ats_inconclusive_falls_through_to_careers(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, LIVE
        mock_ats.return_value = "inconclusive"
        mock_careers.return_value = LIVE
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == LIVE
        mock_careers.assert_called_once()
        mock_serpapi.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_serpapi")
    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_all_inconclusive_returns_inconclusive(self, mock_ats, mock_careers, mock_serpapi):
        from job_finder.web.expiry_checker import _check_job_expiry, INCONCLUSIVE
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = INCONCLUSIVE
        mock_serpapi.return_value = INCONCLUSIVE
        job = {"dedup_key": "test", "title": "DS", "company": "Acme",
               "source_urls": '["https://jobs.lever.co/acme/abc-123"]'}
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == INCONCLUSIVE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestSignalCascade -v`
Expected: FAIL — `_check_job_expiry` does not exist.

- [ ] **Step 3: Implement `_check_job_expiry` cascade function**

Add to `job_finder/web/expiry_checker.py`:

```python
# ---------------------------------------------------------------------------
# Signal cascade orchestrator
# ---------------------------------------------------------------------------

def _check_job_expiry(
    job: dict,
    company: Optional[dict],
    config: dict,
    skip_careers: bool = False,
) -> tuple[str, str]:
    """Run the signal cascade for a single job.

    Signals checked in order: ATS API → careers page → SerpAPI.
    Short-circuits on first definitive answer (EXPIRED or LIVE).

    Args:
        job: Job row dict (must include dedup_key, title, company, source_urls).
        company: Company row dict or None (from companies table join).
        config: Application config dict.
        skip_careers: If True, skip Signal 2 (careers page) due to backoff.

    Returns:
        Tuple of (result, evidence):
            result: EXPIRED, LIVE, or INCONCLUSIVE.
            evidence: Human-readable string describing which signal fired.
    """
    title = job.get("title", "")
    company_name = job.get("company", "")

    # Parse source_urls JSON
    source_urls_raw = job.get("source_urls", "[]")
    if isinstance(source_urls_raw, str):
        try:
            source_urls = json.loads(source_urls_raw)
        except (json.JSONDecodeError, TypeError):
            source_urls = []
    else:
        source_urls = source_urls_raw or []

    # --- Signal 1: ATS API Check ---
    if company and company.get("ats_platform") and company.get("ats_slug"):
        platform = company["ats_platform"]
        slug = company["ats_slug"]
        # Try to extract posting ID from source URLs
        posting_id = None
        for url in source_urls:
            posting_id = _extract_posting_id(url, platform)
            if posting_id:
                break

        if posting_id:
            result = _check_ats_api(slug, posting_id, platform)
            if result == EXPIRED:
                return EXPIRED, f"{platform}_api 404"
            if result == LIVE:
                return LIVE, f"{platform}_api 200"

    # --- Signal 2: Careers Page Check ---
    if not skip_careers:
        homepage_url = company.get("homepage_url") if company else None
        target_titles = config.get("profile", {}).get("target_titles", [])
        exclusions = config.get("profile", {}).get("exclusions", {}).get("title_keywords", [])
        careers_result = _check_careers_page(homepage_url, title, target_titles, exclusions)
        if careers_result == LIVE:
            return LIVE, "careers_page title_found"
        # Note: careers_page returning INCONCLUSIVE falls through to Signal 3

    # --- Signal 3: SerpAPI Fallback ---
    serpapi_result = _check_serpapi(title, company_name, config)
    if serpapi_result == EXPIRED:
        return EXPIRED, "serpapi no_match"
    if serpapi_result == LIVE:
        return LIVE, "serpapi match_found"

    return INCONCLUSIVE, ""
```

- [ ] **Step 4: Run cascade tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestSignalCascade -v`
Expected: PASS

- [ ] **Step 5: Write failing test for `run_expiry_check` batch runner**

Add to `tests/test_expiry_checker.py`:

```python
class TestRunExpiryCheck:
    """run_expiry_check batch runner queries DB and processes jobs."""

    def _setup_db(self, path):
        """Create a migrated DB with test jobs and companies."""
        from job_finder.web.db_migrate import run_migrations
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row

        # Insert a company with ATS info
        conn.execute(
            "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, ats_slug, "
            "ats_probe_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme", "Acme Corp", "https://acme.com", "lever", "acme-corp",
             "hit", "2026-03-01", "2026-03-01"),
        )
        company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert a discovered job linked to company
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id, source_urls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme|ds|remote", "Data Scientist", "Acme Corp", "Remote",
             "2026-03-01", "2026-03-10", "discovered", company_id,
             '["https://jobs.lever.co/acme-corp/abc-123-def"]'),
        )

        # Insert an applied job (should NOT be checked)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("acme|sde|remote", "Software Engineer", "Acme Corp", "Remote",
             "2026-03-01", "2026-03-10", "applied", company_id),
        )
        conn.commit()
        conn.close()
        return path

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_archives_expired_job(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result = run_expiry_check(tmp_db_path, config)

        assert result["archived"] >= 1

        # Verify the job was actually archived
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)).fetchone()
        assert row["pipeline_status"] == "archived"
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_does_not_touch_applied_jobs(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        run_expiry_check(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|sde|remote",)).fetchone()
        assert row["pipeline_status"] == "applied"
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_updates_expiry_checked_at_on_live(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)
        mock_check.return_value = ("live", "lever_api 200")

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        run_expiry_check(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT expiry_checked_at FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)).fetchone()
        assert row["expiry_checked_at"] is not None
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_skips_recently_checked_jobs(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_expiry_check
        self._setup_db(tmp_db_path)

        # Set expiry_checked_at to now (recently checked)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
            (datetime.now(timezone.utc).isoformat(), "acme|ds|remote"),
        )
        conn.commit()
        conn.close()

        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}, "expiry": {"recheck_days": 3}}
        run_expiry_check(tmp_db_path, config)

        mock_check.assert_not_called()
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `pytest tests/test_expiry_checker.py::TestRunExpiryCheck -v`
Expected: FAIL — `run_expiry_check` does not exist.

- [ ] **Step 7: Implement `run_expiry_check` batch runner**

Add to `job_finder/web/expiry_checker.py`:

```python
# ---------------------------------------------------------------------------
# In-memory failure tracker (Signal 2 backoff)
# ---------------------------------------------------------------------------

# Maps company_id -> consecutive failure count. Resets on app restart.
_careers_failure_counts: dict[int, int] = {}
_careers_skip_until: dict[int, datetime] = {}

_MAX_CAREERS_FAILURES = 3
_CAREERS_SKIP_DAYS = 7


def _record_careers_outcome(company_id: Optional[int], success: bool) -> None:
    """Track careers page check outcome for backoff logic.

    On success: reset failure count. On failure: increment count and
    set skip-until timestamp if threshold reached.

    Args:
        company_id: Company row ID (None if no company linked).
        success: True if careers page was reachable and returned results.
    """
    if company_id is None:
        return
    if success:
        _careers_failure_counts.pop(company_id, None)
        _careers_skip_until.pop(company_id, None)
    else:
        count = _careers_failure_counts.get(company_id, 0) + 1
        _careers_failure_counts[company_id] = count
        if count >= _MAX_CAREERS_FAILURES:
            _careers_skip_until[company_id] = datetime.now(timezone.utc) + timedelta(days=_CAREERS_SKIP_DAYS)
            logger.info(
                "_record_careers_outcome: company %d hit %d failures, skipping for %d days",
                company_id, count, _CAREERS_SKIP_DAYS,
            )


# ---------------------------------------------------------------------------
# Public API: Nightly batch runner
# ---------------------------------------------------------------------------

def run_expiry_check(db_path: str, config: dict) -> dict:
    """Run expiry detection on discovered/reviewing jobs.

    Creates its own SQLite connection (thread-safe for APScheduler).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict.

    Returns:
        Dict with keys: checked (int), archived (int), live (int), inconclusive (int).
    """
    expiry_config = config.get("expiry", {})
    if not expiry_config.get("enabled", True):
        return {"checked": 0, "archived": 0, "live": 0, "inconclusive": 0}

    batch_size = expiry_config.get("batch_size", 20)
    recheck_days = expiry_config.get("recheck_days", 3)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Query candidate jobs: discovered/reviewing, not recently checked
        recheck_cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).isoformat()
        rows = conn.execute(
            """SELECT j.*, c.ats_platform, c.ats_slug, c.homepage_url, c.id as company_row_id
               FROM jobs j
               LEFT JOIN companies c ON j.company_id = c.id
               WHERE j.pipeline_status IN ('discovered', 'reviewing')
                 AND (j.expiry_checked_at IS NULL OR j.expiry_checked_at < ?)
               ORDER BY j.expiry_checked_at IS NULL DESC, j.expiry_checked_at ASC
               LIMIT ?""",
            (recheck_cutoff, batch_size),
        ).fetchall()

        archived = 0
        live = 0
        inconclusive = 0

        for row in rows:
            job = dict(row)
            company = None
            if job.get("ats_platform"):
                company = {
                    "ats_platform": job["ats_platform"],
                    "ats_slug": job["ats_slug"],
                    "homepage_url": job["homepage_url"],
                    "id": job.get("company_row_id"),
                }
            elif job.get("homepage_url"):
                company = {"homepage_url": job["homepage_url"], "ats_platform": None, "ats_slug": None}

            # Check careers page failure backoff (Signal 2 only)
            company_id = job.get("company_row_id")
            skip_careers = False
            if company_id and company_id in _careers_skip_until:
                if datetime.now(timezone.utc) < _careers_skip_until[company_id]:
                    skip_careers = True
                    logger.debug("run_expiry_check: skipping careers check for company %s (backoff)", company_id)

            try:
                result, evidence = _check_job_expiry(job, company, config, skip_careers=skip_careers)
            except Exception as e:
                logger.warning("run_expiry_check: error checking %s: %s", job["dedup_key"], e)
                inconclusive += 1
                continue

            now = datetime.now(timezone.utc).isoformat()

            # Track careers page outcome for backoff
            if not skip_careers and company_id:
                if "careers_page" in evidence:
                    _record_careers_outcome(company_id, success=True)
                elif result == INCONCLUSIVE and company and company.get("homepage_url"):
                    # Careers page was attempted but inconclusive (possible failure)
                    _record_careers_outcome(company_id, success=False)

            if result == EXPIRED:
                from job_finder.db import update_pipeline_status
                update_pipeline_status(
                    conn, job["dedup_key"], "archived",
                    source="expiry_check", evidence=evidence,
                )
                conn.execute(
                    "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
                    (now, job["dedup_key"]),
                )
                conn.commit()
                archived += 1
                logger.info("run_expiry_check: archived %s (%s)", job["dedup_key"], evidence)

            elif result == LIVE:
                conn.execute(
                    "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
                    (now, job["dedup_key"]),
                )
                conn.commit()
                live += 1

            else:
                inconclusive += 1
                logger.debug("run_expiry_check: inconclusive for %s", job["dedup_key"])

            # Rate limit between jobs
            time.sleep(_INTER_REQUEST_DELAY)

        result_summary = {
            "checked": len(rows),
            "archived": archived,
            "live": live,
            "inconclusive": inconclusive,
        }
        logger.info("run_expiry_check complete: %s", result_summary)
        return result_summary

    except Exception:
        conn.rollback()
        logger.exception("run_expiry_check failed")
        raise
    finally:
        conn.close()
```

- [ ] **Step 8: Run batch runner tests to verify they pass**

Run: `pytest tests/test_expiry_checker.py::TestRunExpiryCheck -v`
Expected: PASS

- [ ] **Step 9: Run full test suite**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 10: Commit**

```bash
git add job_finder/web/expiry_checker.py tests/test_expiry_checker.py
git commit -m "feat: add expiry checker batch runner with signal cascade orchestration"
```

---

### Task 4: Scheduler Integration & Activity Tracker

**Files:**
- Modify: `job_finder/web/activity_tracker.py` (add constant)
- Modify: `job_finder/web/scheduler.py` (add expiry check job)
- Modify: `tests/test_scheduler.py` (add test)

- [ ] **Step 1: Add `ACTION_SCHEDULED_EXPIRY_CHECK` to activity tracker**

In `job_finder/web/activity_tracker.py`, add after `ACTION_EXTRACT_STYLE`:

```python
ACTION_SCHEDULED_EXPIRY_CHECK = "scheduled_expiry_check"
```

- [ ] **Step 2: Write failing test for scheduler registration**

Add to `tests/test_scheduler.py`:

```python
class TestSchedulerExpiryCheck:
    """Verify expiry check job is registered in APScheduler."""

    def test_scheduler_registers_expiry_check_job(self):
        """init_scheduler registers 'expiry_check' CronTrigger job (hour=2, minute=30)."""
        from job_finder.web.scheduler import reset_scheduler
        from unittest.mock import MagicMock, patch

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config.get.side_effect = lambda key, default=None: {
            "TESTING": False,
        }.get(key, default)

        with patch("job_finder.web.scheduler.os.environ.get", return_value=None):
            with patch("job_finder.web.scheduler.BackgroundScheduler") as mock_scheduler_cls:
                mock_sched = MagicMock()
                mock_scheduler_cls.return_value = mock_sched

                from job_finder.web.scheduler import init_scheduler
                init_scheduler(mock_app)

        job_ids_kw = [call[1].get("id") for call in mock_sched.add_job.call_args_list]
        assert "expiry_check" in job_ids_kw
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py::TestSchedulerExpiryCheck -v`
Expected: FAIL — no `expiry_check` job registered.

- [ ] **Step 4: Add expiry check job to `scheduler.py`**

In `job_finder/web/scheduler.py`, add before `scheduler.start()` (after the slug probe job block):

```python
        def run_expiry_check_job():
            """Nightly expiry check job executed by APScheduler."""
            import time as _time
            with app.app_context():
                from job_finder.web.activity_tracker import (
                    log_activity, ACTION_SCHEDULED_EXPIRY_CHECK
                )
                from job_finder.web.expiry_checker import run_expiry_check
                config = app.config.get("JF_CONFIG", {})
                db_path = app.config.get("DB_PATH", "jobs.db")

                if not config.get("expiry", {}).get("enabled", True):
                    return

                t0 = _time.time()
                try:
                    result = run_expiry_check(db_path, config)
                    logger.info("Expiry check: %s", result)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_EXPIRY_CHECK,
                        metadata={
                            "checked": result.get("checked", 0),
                            "archived": result.get("archived", 0),
                            "live": result.get("live", 0),
                            "inconclusive": result.get("inconclusive", 0),
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception as e:
                    logger.error("Expiry check failed: %s", e)
                    log_activity(
                        db_path,
                        ACTION_SCHEDULED_EXPIRY_CHECK,
                        metadata={
                            "status": "failed",
                            "error": type(e).__name__,
                            "duration_seconds": round(_time.time() - t0, 2),
                        },
                    )

        scheduler.add_job(
            run_expiry_check_job,
            trigger=CronTrigger(hour=2, minute=30),
            id="expiry_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
```

- [ ] **Step 5: Run scheduler test to verify it passes**

Run: `pytest tests/test_scheduler.py::TestSchedulerExpiryCheck -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/activity_tracker.py job_finder/web/scheduler.py tests/test_scheduler.py
git commit -m "feat: register nightly expiry check job in APScheduler at 2:30 AM"
```

---

## Chunk 2: Resume Generation Quality Upgrade

### Task 5: System Prompt Enrichment with Distilled Guidelines

**Files:**
- Modify: `job_finder/web/resume_generator.py:136-143` (replace `_SYSTEM_PROMPT`)

- [ ] **Step 1: Write test that verifies guidelines are in the system prompt**

Add to `tests/test_resume.py`:

```python
class TestResumeGuidelines:
    """Verify _RESUME_GUIDELINES is integrated into resume generation prompts."""

    def test_guidelines_constant_exists(self):
        from job_finder.web.resume_generator import _RESUME_GUIDELINES
        assert isinstance(_RESUME_GUIDELINES, str)
        assert len(_RESUME_GUIDELINES) > 500  # Should be substantial

    def test_guidelines_includes_source_fidelity(self):
        from job_finder.web.resume_generator import _RESUME_GUIDELINES
        assert "fabricat" in _RESUME_GUIDELINES.lower() or "never list a skill" in _RESUME_GUIDELINES.lower()

    def test_guidelines_includes_bullet_formula(self):
        from job_finder.web.resume_generator import _RESUME_GUIDELINES
        assert "action verb" in _RESUME_GUIDELINES.lower()

    def test_system_prompt_includes_guidelines(self):
        from job_finder.web.resume_generator import _SYSTEM_PROMPT, _RESUME_GUIDELINES
        assert _RESUME_GUIDELINES in _SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resume.py::TestResumeGuidelines -v`
Expected: FAIL — `_RESUME_GUIDELINES` does not exist.

- [ ] **Step 3: Add `_RESUME_GUIDELINES` constant and update `_SYSTEM_PROMPT`**

In `job_finder/web/resume_generator.py`, replace the `_SYSTEM_PROMPT` block (lines 136-143) with:

```python
# ---------------------------------------------------------------------------
# Distilled resume guidelines (from docs/resume_generation_guidelines.md)
# ---------------------------------------------------------------------------

_RESUME_GUIDELINES = (
    "\n\nRESUME WRITING GUIDELINES (follow strictly):\n\n"

    "SOURCE FIDELITY (MOST IMPORTANT RULE):\n"
    "- NEVER list a skill, tool, or technology the candidate has not used. If the JD asks for "
    "a tool the candidate lacks, list the closest real analog and let bullet context bridge the gap.\n"
    "- Every bullet must trace directly to the candidate's profile data.\n\n"

    "PROFESSIONAL SUMMARY:\n"
    "- 3-4 sentences maximum.\n"
    "- Formula: (1) Role archetype with X+ years doing what, in what context. "
    "(2) Proven track record with one concrete numbered example. "
    "(3) JD-specific capabilities and forward-looking value prop.\n"
    "- Mirror the JD's title/archetype language in the opening.\n"
    "- Never use 'seeking' -- frame as a practitioner bringing value.\n\n"

    "SKILLS SECTION:\n"
    "- Hard skills and methodologies ONLY. Never list soft skills (Cross-Functional Collaboration, "
    "Stakeholder Communication, Team Leadership, etc.) -- demonstrate these through bullets.\n"
    "- Front-load to match JD priority order. Pipe-separated or category-labeled.\n"
    "- 1-2 lines maximum.\n\n"

    "BULLET WRITING:\n"
    "- Formula: Action Verb + What You Did + How/With What + Quantified Impact.\n"
    "- Rotate verbs: never start two consecutive bullets with the same verb.\n"
    "- 1-2 lines per bullet (3 lines absolute maximum, rare).\n"
    "- Every bullet must pass the 'so what?' test -- the hiring manager must immediately understand "
    "why it matters. If there's no business outcome or quantified result, cut or rework.\n"
    "- Anti-patterns to AVOID: problem-identified openers ('Identified lack of...'), "
    "methods-listing without business outcome, redundant experimentation bullets, "
    "standalone soft-skill claims.\n\n"

    "BULLET COUNT BY SENIORITY:\n"
    "- Most recent/current role: 4-6 bullets.\n"
    "- Previous role at same company: 2-3 bullets.\n"
    "- Prior companies: 1-2 bullets each.\n"
    "- Early career: 1 bullet maximum.\n\n"

    "CONFIDENTIALITY:\n"
    "- Never include specific client names -- use generic descriptors "
    "('a major enterprise client', 'Fortune 500 financial services client').\n"
    "- Omit specific team sizes unless the JD explicitly requires it.\n\n"

    "TYPOGRAPHY:\n"
    "- No bold text within bullet point content.\n"
    "- No em dashes anywhere -- restructure with commas, semicolons, or separate clauses.\n"
    "- Minimize parentheses -- integrate details naturally.\n"
    "- Do not define well-known acronyms (ITT, DiD, RCT, ROI, KPI, ETL, etc.).\n\n"

    "JD MIRRORING:\n"
    "- Use the JD's exact terminology for tools and methodologies.\n"
    "- Never lift full phrases verbatim from the JD requirements.\n"
    "- A JD phrase may appear at most once across the resume.\n"
    "- The reader should feel alignment, not pattern-matching.\n"
)

_SYSTEM_PROMPT = (
    "You are a professional resume writer. Generate a tailored resume for the candidate "
    "applying to this specific job. "
    "CRITICAL CONSTRAINT: You must ONLY use information from the candidate's profile below. "
    "You may rephrase, reframe, and reorder content, but you must NEVER invent, infer, or add "
    "achievements, skills, companies, or experiences not present in the profile. "
    "Every bullet point must trace back to the profile data."
    + _RESUME_GUIDELINES
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resume.py::TestResumeGuidelines -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/resume_generator.py tests/test_resume.py
git commit -m "feat: inject distilled resume guidelines into generation system prompt"
```

---

### Task 6: Resume Validator Module

**Files:**
- Create: `job_finder/web/resume_validator.py`
- Create: `tests/test_resume_validator.py`

- [ ] **Step 1: Write failing tests for validator audit function**

Create `tests/test_resume_validator.py`:

```python
"""Tests for resume_validator.py — Sonnet quality audit and auto-fix."""

import json
import sqlite3
import tempfile
import os
from unittest.mock import patch, MagicMock

import pytest


class TestValidateResume:
    """validate_resume runs a Sonnet quality audit and returns violations."""

    @patch("job_finder.web.resume_validator.call_claude")
    def test_returns_validation_result(self, mock_claude):
        from job_finder.web.resume_validator import validate_resume

        mock_claude.return_value = (
            {"passed": True, "violations": []},
            0.01,
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}}}

        result = validate_resume(
            resume_data={"name": "Jane", "summary": "Test", "skills": ["Python"], "positions": []},
            jd_text="We need a Python developer",
            profile={"skills": ["Python"]},
            conn=conn,
            config=config,
        )

        assert result["passed"] is True
        assert result["violations"] == []
        conn.close()

    @patch("job_finder.web.resume_validator.call_claude")
    def test_returns_violations_on_failure(self, mock_claude):
        from job_finder.web.resume_validator import validate_resume

        mock_claude.return_value = (
            {
                "passed": False,
                "violations": [
                    {"category": "content_integrity", "description": "Fabricated skill: Kubernetes", "severity": "error"},
                ],
            },
            0.01,
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}}}

        result = validate_resume(
            resume_data={"name": "Jane", "summary": "Test", "skills": ["Python", "Kubernetes"], "positions": []},
            jd_text="We need Kubernetes experience",
            profile={"skills": ["Python"]},
            conn=conn,
            config=config,
        )

        assert result["passed"] is False
        assert len(result["violations"]) == 1
        assert result["violations"][0]["severity"] == "error"
        conn.close()


class TestValidateResumeFailOpen:
    """validate_resume returns pass on exception (fail-open design)."""

    @patch("job_finder.web.resume_validator.call_claude")
    def test_returns_pass_on_exception(self, mock_claude):
        from job_finder.web.resume_validator import validate_resume

        mock_claude.side_effect = Exception("API error")

        conn = sqlite3.connect(":memory:")
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}}}

        result = validate_resume(
            resume_data={"name": "Jane", "summary": "Test", "skills": [], "positions": []},
            jd_text="Any JD",
            profile={"skills": []},
            conn=conn,
            config=config,
        )

        assert result["passed"] is True
        assert result["violations"] == []
        conn.close()


class TestFixResumeViolations:
    """fix_resume_violations runs a Sonnet fix pass on error violations."""

    @patch("job_finder.web.resume_validator.call_claude")
    def test_returns_fixed_resume(self, mock_claude):
        from job_finder.web.resume_validator import fix_resume_violations

        fixed_resume = {"name": "Jane", "summary": "Test", "skills": ["Python"], "positions": []}
        mock_claude.return_value = (fixed_resume, 0.02)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}}}

        violations = [
            {"category": "content_integrity", "description": "Fabricated skill: Kubernetes", "severity": "error"},
        ]

        result = fix_resume_violations(
            resume_data={"name": "Jane", "summary": "Test", "skills": ["Python", "Kubernetes"], "positions": []},
            violations=violations,
            profile={"skills": ["Python"]},
            conn=conn,
            config=config,
        )

        assert "Kubernetes" not in result["skills"]
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resume_validator.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create `resume_validator.py`**

Create `job_finder/web/resume_validator.py`:

```python
"""Post-generation resume quality validator.

Provides:
    VALIDATION_SCHEMA   -- JSON schema for Sonnet quality audit output
    validate_resume     -- Sonnet audit pass checking against guideline checklist
    fix_resume_violations -- Sonnet fix pass for error-severity violations

Phase 1 (audit) always runs after resume generation.
Phase 2 (fix) only runs if Phase 1 found error-severity violations.
No re-validation after fix (by design -- avoids infinite loop).
"""

import json
import logging
import sqlite3
from typing import Any

from job_finder.config import DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import call_claude

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema for structured validation output
# ---------------------------------------------------------------------------

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {
            "type": "boolean",
            "description": "True if no error-severity violations found",
        },
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "One of: content_integrity, structural, style, jd_alignment, readability",
                    },
                    "description": {
                        "type": "string",
                        "description": "Specific description of the violation",
                    },
                    "severity": {
                        "type": "string",
                        "description": "error (must fix) or warning (informational)",
                    },
                },
                "required": ["category", "description", "severity"],
            },
        },
    },
    "required": ["passed", "violations"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Validation system prompt
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM = (
    "You are a resume quality auditor. Given a generated resume, the job description it targets, "
    "and the candidate's source profile, check for violations against these quality standards:\n\n"

    "CONTENT INTEGRITY (severity: error):\n"
    "- Skills listed that do NOT appear in the candidate profile (fabrication)\n"
    "- Specific client names appearing in the document\n"
    "- Employment dates that don't match the profile\n\n"

    "STRUCTURAL (severity: error if egregious, warning if minor):\n"
    "- Professional summary exceeds 4 sentences\n"
    "- Skills section exceeds 2 lines worth of items\n"
    "- Most recent role has fewer than 4 or more than 6 bullets\n"
    "- Earlier roles have too many bullets relative to seniority\n\n"

    "STYLE (severity: warning):\n"
    "- Two consecutive bullets start with the same verb\n"
    "- Any bullet exceeds 2 lines (3+ lines)\n"
    "- Soft skills listed in the Skills section\n"
    "- Em dashes present anywhere in the document\n"
    "- Bold formatting indicated within bullet text\n\n"

    "JD ALIGNMENT (severity: warning):\n"
    "- Top 5 JD keywords not each appearing at least once\n"
    "- Full phrases lifted verbatim from JD requirements\n\n"

    "READABILITY (severity: warning):\n"
    "- Bullets that fail the 'so what?' test (no business outcome)\n"
    "- Vague language ('helped with', 'assisted in', 'was responsible for')\n"
    "- Passive voice constructions\n\n"

    "Return passed=true only if there are ZERO error-severity violations. "
    "Warnings alone do not cause passed=false."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_resume(
    resume_data: dict,
    jd_text: str,
    profile: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Run Sonnet quality audit on a generated resume.

    Args:
        resume_data: Generated resume dict (matching RESUME_SCHEMA).
        jd_text: Full job description text.
        profile: Candidate experience profile dict.
        conn: Open SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Dict matching VALIDATION_SCHEMA with passed bool and violations list.
        On error, returns {"passed": True, "violations": []} (fail-open).
    """
    try:
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        profile_skills = profile.get("skills", [])
        # Build profile context with positions and dates for date mismatch checking
        positions_summary = ""
        for pos in profile.get("positions", []):
            start = pos.get("start_date", "")
            end = pos.get("end_date", "Present") or "Present"
            positions_summary += f"- {pos.get('title', '')} at {pos.get('company', '')} ({start} - {end})\n"

        user_message = (
            f"## Generated Resume\n\n"
            f"```json\n{json.dumps(resume_data, indent=2)}\n```\n\n"
            f"---\n\n"
            f"## Job Description\n\n{jd_text}\n\n"
            f"---\n\n"
            f"## Candidate Profile (source of truth)\n\n"
            f"**Skills:** {', '.join(profile_skills)}\n\n"
            f"**Positions:**\n{positions_summary}\n\n"
            f"Check this resume against all quality standards and report violations."
        )

        import anthropic
        client = anthropic.Anthropic()

        result, _cost = call_claude(
            client=client,
            model=model,
            system=_AUDIT_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            output_schema=VALIDATION_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_validation",
            config=config,
            max_tokens=2048,
        )
        return result

    except Exception as e:
        logger.warning("validate_resume: audit failed, returning pass: %s", e)
        return {"passed": True, "violations": []}


def fix_resume_violations(
    resume_data: dict,
    violations: list[dict],
    profile: dict,
    conn: sqlite3.Connection,
    config: dict,
) -> dict:
    """Run Sonnet fix pass to correct error-severity violations.

    Args:
        resume_data: Original generated resume dict.
        violations: List of violation dicts from validate_resume.
        profile: Candidate experience profile dict (for closed-world constraint).
        conn: Open SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Fixed resume dict matching RESUME_SCHEMA.
        On error, returns the original resume_data unchanged.
    """
    from job_finder.web.resume_generator import RESUME_SCHEMA

    try:
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        error_violations = [v for v in violations if v.get("severity") == "error"]
        violations_text = "\n".join(
            f"- [{v['category']}] {v['description']}" for v in error_violations
        )

        system = (
            "You are a resume editor. Fix the listed violations in this resume while "
            "maintaining the closed-world constraint: do not add any information not present "
            "in the candidate's profile. Only fix the specific violations listed. "
            "Preserve all content that is not affected by a violation."
        )

        user_message = (
            f"## Resume to Fix\n\n"
            f"```json\n{json.dumps(resume_data, indent=2)}\n```\n\n"
            f"---\n\n"
            f"## Violations to Fix\n\n{violations_text}\n\n"
            f"---\n\n"
            f"## Candidate Profile (source of truth)\n\n"
            f"**Skills:** {', '.join(profile.get('skills', []))}\n\n"
            f"Fix each violation and return the corrected resume."
        )

        import anthropic
        client = anthropic.Anthropic()

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=RESUME_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="resume_fix",
            config=config,
            max_tokens=4096,
        )
        return result

    except Exception as e:
        logger.warning("fix_resume_violations: fix pass failed, returning original: %s", e)
        return resume_data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resume_validator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/resume_validator.py tests/test_resume_validator.py
git commit -m "feat: add resume validator module (Sonnet audit + auto-fix)"
```

---

### Task 7: Integrate Validator into Resume Background Flow

**Files:**
- Modify: `job_finder/web/resume_generator.py:779-810` (insert validator between generate and docx)

- [ ] **Step 1: Write test that validator is called during background generation**

Add to `tests/test_resume_validator.py`:

```python
class TestValidatorIntegration:
    """Validator is called in _generate_resume_background flow."""

    def _setup_gen_row(self, db_path):
        """Create a pending resume_generations row and return its ID."""
        from job_finder.web.db_migrate import run_migrations
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO resume_generations (job_id, generated_at, model, status) "
            "VALUES (?, ?, ?, ?)",
            ("test-key", "2026-03-16", "sonnet", "pending"),
        )
        conn.commit()
        gen_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return gen_id

    @patch("job_finder.web.resume_generator.upload_to_drive")
    @patch("job_finder.web.resume_generator.get_drive_service")
    @patch("job_finder.web.resume_generator.build_resume_docx")
    @patch("job_finder.web.resume_validator.call_claude")
    @patch("job_finder.web.resume_generator.anthropic.Anthropic")
    @patch("job_finder.web.resume_generator.generate_resume_single")
    def test_validator_called_after_generation(
        self, mock_gen, mock_anthropic, mock_validate_claude, mock_docx, mock_drive_svc, mock_upload, tmp_path
    ):
        from job_finder.web.resume_generator import _generate_resume_background

        db_path = str(tmp_path / "test.db")
        gen_id = self._setup_gen_row(db_path)

        resume_data = {"name": "Jane", "summary": "Test", "skills": ["Python"], "positions": []}
        mock_gen.return_value = resume_data
        mock_validate_claude.return_value = ({"passed": True, "violations": []}, 0.01)
        mock_docx.return_value = b"fake-docx"
        mock_upload.return_value = "https://docs.google.com/doc/123"

        job_row = {"dedup_key": "test-key", "title": "DS", "company": "Acme",
                   "jd_full": "Need Python", "sonnet_score": 70}
        profile = {"skills": ["Python"], "positions": []}
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}, "multi_version_threshold": 80},
                  "drive": {"folder_id": "abc", "convert_to_gdoc": True}}

        _generate_resume_background(db_path, gen_id, job_row, profile, config)

        # Validator was called
        mock_validate_claude.assert_called_once()

        # Check validation_report was saved
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT validation_report FROM resume_generations WHERE id = ?", (gen_id,)).fetchone()
        assert row["validation_report"] is not None
        conn.close()

    @patch("job_finder.web.resume_generator.upload_to_drive")
    @patch("job_finder.web.resume_generator.get_drive_service")
    @patch("job_finder.web.resume_generator.build_resume_docx")
    @patch("job_finder.web.resume_validator.call_claude")
    @patch("job_finder.web.resume_generator.anthropic.Anthropic")
    @patch("job_finder.web.resume_generator.generate_resume_single")
    def test_autofix_branch_runs_when_errors_found(
        self, mock_gen, mock_anthropic, mock_validate_claude, mock_docx, mock_drive_svc, mock_upload, tmp_path
    ):
        """When validation finds errors, fix_resume_violations is called and its output is used."""
        from job_finder.web.resume_generator import _generate_resume_background

        db_path = str(tmp_path / "test.db")
        gen_id = self._setup_gen_row(db_path)

        original_resume = {"name": "Jane", "summary": "Test", "skills": ["Python", "Kubernetes"], "positions": []}
        fixed_resume = {"name": "Jane", "summary": "Test", "skills": ["Python"], "positions": []}

        mock_gen.return_value = original_resume
        # First call: audit returns errors. Second call: fix returns fixed resume.
        mock_validate_claude.side_effect = [
            ({"passed": False, "violations": [
                {"category": "content_integrity", "description": "Fabricated: Kubernetes", "severity": "error"},
            ]}, 0.01),
            (fixed_resume, 0.02),
        ]
        mock_docx.return_value = b"fake-docx"
        mock_upload.return_value = "https://docs.google.com/doc/123"

        job_row = {"dedup_key": "test-key", "title": "DS", "company": "Acme",
                   "jd_full": "Need Python", "sonnet_score": 70}
        profile = {"skills": ["Python"], "positions": []}
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}, "multi_version_threshold": 80},
                  "drive": {"folder_id": "abc", "convert_to_gdoc": True}}

        _generate_resume_background(db_path, gen_id, job_row, profile, config)

        # Validator was called twice: audit + fix
        assert mock_validate_claude.call_count == 2
        # Docx was built with the FIXED resume, not the original
        mock_docx.assert_called_once_with(fixed_resume)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_resume_validator.py::TestValidatorIntegration -v`
Expected: FAIL — `_generate_resume_background` doesn't call the validator yet.

- [ ] **Step 3: Insert validator into `_generate_resume_background`**

In `job_finder/web/resume_generator.py`, add the validator call between the generation and the `.docx` formatting. Replace the section after `generation_type = "single"` / `generation_type = "multi"` (lines 778-779, just before `# Format as .docx`) with:

```python
        # --- Validate generated resume ---
        from job_finder.web.resume_validator import validate_resume, fix_resume_violations
        import json as _json

        validation_report = None
        try:
            jd_text = job_row.get("jd_full", "")
            validation_report = validate_resume(resume_data, jd_text, profile, conn, config)

            # Save validation report to DB
            conn.execute(
                "UPDATE resume_generations SET validation_report = ? WHERE id = ?",
                (_json.dumps(validation_report), gen_id),
            )
            conn.commit()

            # Auto-fix if error-severity violations found
            has_errors = any(
                v.get("severity") == "error"
                for v in validation_report.get("violations", [])
            )
            if has_errors:
                logger.info(
                    "_generate_resume_background: %d error violations, running fix pass for gen_id=%s",
                    sum(1 for v in validation_report["violations"] if v.get("severity") == "error"),
                    gen_id,
                )
                resume_data = fix_resume_violations(
                    resume_data,
                    validation_report["violations"],
                    profile,
                    conn,
                    config,
                )
        except Exception as e:
            logger.warning("_generate_resume_background: validation failed for gen_id=%s: %s", gen_id, e)

        # Format as .docx
```

- [ ] **Step 4: Run integration test to verify it passes**

Run: `pytest tests/test_resume_validator.py::TestValidatorIntegration -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/resume_generator.py tests/test_resume_validator.py
git commit -m "feat: integrate resume validator into background generation flow"
```

---

### Task 8: Style Guide Schema Expansion & Migration

**Files:**
- Modify: `job_finder/web/resume_style_guide.py` (expand schema, add migration function, update directives)
- Modify: `tests/test_resume_style_guide.py` (add tests for expanded schema)

- [ ] **Step 1: Write failing tests for expanded schema and migration**

Add to `tests/test_resume_style_guide.py`:

```python
class TestExpandedSchema:
    """Expanded STYLE_GUIDE_SCHEMA includes resume guideline fields."""

    def test_schema_has_new_fields(self):
        from job_finder.web.resume_style_guide import STYLE_GUIDE_SCHEMA
        props = STYLE_GUIDE_SCHEMA["properties"]
        new_fields = [
            "summary_formula", "skills_format", "bullet_formula",
            "bullet_counts", "confidentiality_rules", "typography_rules",
            "jd_mirroring_rules", "anti_patterns", "role_archetype",
        ]
        for field in new_fields:
            assert field in props, f"Missing field: {field}"

    def test_schema_removes_consistency_notes(self):
        from job_finder.web.resume_style_guide import STYLE_GUIDE_SCHEMA
        assert "consistency_notes" not in STYLE_GUIDE_SCHEMA["properties"]


class TestExpandedDirectives:
    """_build_style_guide_directives handles new fields."""

    def test_directives_include_bullet_formula(self):
        from job_finder.web.resume_style_guide import _build_style_guide_directives
        guide = {
            "bullet_style": "filled circles",
            "verb_tense": "past",
            "bullet_formula": "Action Verb + What + How + Impact",
        }
        directives = _build_style_guide_directives(guide)
        assert any("bullet formula" in d.lower() or "action verb" in d.lower() for d in directives)

    def test_directives_include_anti_patterns(self):
        from job_finder.web.resume_style_guide import _build_style_guide_directives
        guide = {
            "anti_patterns": ["problem-identified openers", "methods-listing without outcome"],
        }
        directives = _build_style_guide_directives(guide)
        assert any("anti-pattern" in d.lower() or "avoid" in d.lower() for d in directives)


class TestMigrateStyleGuide:
    """migrate_style_guide merges existing guide with guidelines doc."""

    @patch("job_finder.web.resume_style_guide.call_claude")
    def test_migrate_preserves_existing_fields(self, mock_claude, tmp_path):
        from job_finder.web.resume_style_guide import migrate_style_guide, save_style_guide, load_style_guide
        import sqlite3

        guide_path = str(tmp_path / "style_guide.json")
        existing = {
            "bullet_style": "filled circles",
            "verb_tense": "past tense",
            "section_order": ["Summary", "Skills", "Experience", "Education"],
            "tone": "professional",
            "date_format": "MM/YYYY",
        }
        save_style_guide(existing, guide_path)

        # Mock Sonnet to return a merged guide with new fields
        merged = {
            **existing,
            "summary_formula": "archetype + years, achievement, JD capabilities",
            "skills_format": "pipe-separated, 1-2 lines",
            "bullet_formula": "Action Verb + What + How + Impact",
            "bullet_counts": {"current": "4-6", "previous": "2-3", "prior": "1-2", "early": "1"},
            "confidentiality_rules": "no client names, no team sizes",
            "typography_rules": "no bold in bullets, no em dashes",
            "jd_mirroring_rules": "exact terminology, no verbatim lifts",
            "anti_patterns": ["problem-identified openers", "methods-listing without outcome"],
            "role_archetype": "Senior IC Data Scientist",
        }
        mock_claude.return_value = (merged, 0.05)

        conn = sqlite3.connect(":memory:")
        config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-20250514"}}}

        migrate_style_guide(config, conn, guide_path=guide_path)

        loaded = load_style_guide(guide_path)
        assert loaded["bullet_style"] == "filled circles"  # preserved
        assert loaded["bullet_formula"] == "Action Verb + What + How + Impact"  # new
        assert "anti_patterns" in loaded
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resume_style_guide.py::TestExpandedSchema tests/test_resume_style_guide.py::TestExpandedDirectives tests/test_resume_style_guide.py::TestMigrateStyleGuide -v`
Expected: FAIL — schema not expanded yet.

- [ ] **Step 3: Expand `STYLE_GUIDE_SCHEMA` and update directives builder**

In `job_finder/web/resume_style_guide.py`, replace the `STYLE_GUIDE_SCHEMA` and `_FIELD_LABELS` with:

```python
STYLE_GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "bullet_style": {"type": "string"},
        "verb_tense": {"type": "string"},
        "section_order": {"type": "array", "items": {"type": "string"}},
        "tone": {"type": "string"},
        "date_format": {"type": "string"},
        "summary_style": {"type": "string"},
        # New fields from resume_generation_guidelines.md
        "summary_formula": {"type": "string", "description": "Professional summary formula (3-sentence structure)"},
        "skills_format": {"type": "string", "description": "Skills section format (pipe-separated, 1-2 lines)"},
        "bullet_formula": {"type": "string", "description": "Bullet writing formula (Action + What + How + Impact)"},
        "bullet_counts": {
            "type": "object",
            "description": "Bullet counts by seniority (current, previous, prior, early)",
        },
        "confidentiality_rules": {"type": "string", "description": "Client name and team size rules"},
        "typography_rules": {"type": "string", "description": "No bold in bullets, no em dashes, etc."},
        "jd_mirroring_rules": {"type": "string", "description": "JD keyword mirroring strategy"},
        "anti_patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bullet writing anti-patterns to avoid",
        },
        "role_archetype": {"type": "string", "description": "IC-heavy, manager, analyst, etc."},
    },
    "required": ["bullet_style", "verb_tense", "section_order", "tone", "date_format"],
    "additionalProperties": False,
}

# Human-readable labels for each field
_FIELD_LABELS = {
    "bullet_style": "Bullet style",
    "verb_tense": "Verb tense",
    "section_order": "Section order",
    "tone": "Tone",
    "date_format": "Date format",
    "summary_style": "Summary style",
    "summary_formula": "Summary formula",
    "skills_format": "Skills format",
    "bullet_formula": "Bullet formula",
    "bullet_counts": "Bullet counts by seniority",
    "confidentiality_rules": "Confidentiality rules",
    "typography_rules": "Typography rules",
    "jd_mirroring_rules": "JD mirroring rules",
    "anti_patterns": "Anti-patterns to avoid",
    "role_archetype": "Role archetype",
}
```

Update `_build_style_guide_directives` to handle the new field types (the existing implementation already handles lists and strings, but add explicit handling for dicts):

```python
def _build_style_guide_directives(guide: dict) -> list[str]:
    """Convert a style guide dict to a list of formatted prompt directive strings."""
    if not guide:
        return []

    directives = []
    for field, label in _FIELD_LABELS.items():
        value = guide.get(field)
        if not value:
            continue
        if isinstance(value, list):
            if value:
                directives.append(f"{label}: {', '.join(str(v) for v in value)}")
        elif isinstance(value, dict):
            parts = [f"{k}: {v}" for k, v in value.items() if v]
            if parts:
                directives.append(f"{label}: {'; '.join(parts)}")
        else:
            if str(value).strip():
                directives.append(f"{label}: {value}")

    return directives
```

- [ ] **Step 4: Add `migrate_style_guide` function**

Add to `job_finder/web/resume_style_guide.py`:

```python
def migrate_style_guide(
    config: dict,
    conn: sqlite3.Connection,
    guide_path: str = _STYLE_GUIDE_PATH,
    guidelines_path: str = "docs/resume_generation_guidelines.md",
) -> dict:
    """One-time migration: merge existing style guide with resume generation guidelines.

    Preserves existing preferences while populating new fields from the guidelines doc.

    Args:
        config: Application config dict.
        conn: Open SQLite connection for cost recording.
        guide_path: Path to resume_style_guide.json.
        guidelines_path: Path to resume_generation_guidelines.md.

    Returns:
        The merged style guide dict (also saved to guide_path).
    """
    existing = load_style_guide(guide_path)

    # Read guidelines document
    try:
        with open(guidelines_path, "r", encoding="utf-8") as f:
            guidelines_text = f.read()
    except FileNotFoundError:
        logger.warning("migrate_style_guide: guidelines file not found at %s", guidelines_path)
        return existing

    try:
        client = anthropic.Anthropic()
        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        system = (
            "You are a resume style analyst. Merge the candidate's existing style preferences "
            "with the rules from the resume generation guidelines document. "
            "PRESERVE all existing preference values (bullet_style, verb_tense, tone, etc.) — "
            "these reflect the candidate's personal style. "
            "POPULATE the new fields (summary_formula, skills_format, bullet_formula, "
            "bullet_counts, confidentiality_rules, typography_rules, jd_mirroring_rules, "
            "anti_patterns, role_archetype) using the guidelines document's rules. "
            "Return a unified style guide."
        )

        user_message = (
            f"## Existing Style Guide\n\n"
            f"```json\n{json.dumps(existing, indent=2)}\n```\n\n"
            f"---\n\n"
            f"## Resume Generation Guidelines\n\n"
            f"{guidelines_text}\n\n"
            f"Merge these into a unified style guide."
        )

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            output_schema=STYLE_GUIDE_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="style_guide_migration",
            config=config,
            max_tokens=2048,
        )

        save_style_guide(result, guide_path)
        logger.info("migrate_style_guide: merged style guide saved to %s", guide_path)
        return result

    except Exception as e:
        logger.warning("migrate_style_guide: failed: %s", e)
        return existing
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_resume_style_guide.py::TestExpandedSchema tests/test_resume_style_guide.py::TestExpandedDirectives tests/test_resume_style_guide.py::TestMigrateStyleGuide -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -x --timeout=60`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/resume_style_guide.py tests/test_resume_style_guide.py
git commit -m "feat: expand style guide schema with 9 guideline fields and add migration function"
```

---

### Task 9: Template Update — Validation Report Badge

**Files:**
- Modify: `job_finder/web/templates/jobs/_resume_section.html`
- Modify: `job_finder/web/blueprints/jobs.py` (pass validation_report in context)

- [ ] **Step 1: Add `validation_report` to the resume history query**

In `job_finder/db.py:291`, the resume history query has an explicit column list that does NOT include `validation_report`. Add it:

```python
    resume_history = conn.execute(
        "SELECT id, job_id, status, doc_url, error_msg, generated_at, model, generation_type, validation_report "
        "FROM resume_generations WHERE job_id = ? ORDER BY generated_at DESC",
        (dedup_key,),
    ).fetchall()
```

- [ ] **Step 3: Add validation badge to resume history entries**

In `_resume_section.html`, inside the `{% for gen in resume_history %}` loop, after the existing status display, add:

```html
    {# Validation report badge #}
    {% if gen.validation_report %}
    {% set report = gen.validation_report | from_json %}
    {% if report and report.violations %}
    {% set error_count = report.violations | selectattr('severity', 'equalto', 'error') | list | length %}
    {% set warn_count = report.violations | selectattr('severity', 'equalto', 'warning') | list | length %}
    {% if error_count > 0 %}
    <span class="text-xs text-amber-400" title="{{ error_count }} error(s) fixed, {{ warn_count }} warning(s)">
      ({{ error_count }} fixed, {{ warn_count }}w)
    </span>
    {% elif warn_count > 0 %}
    <span class="text-xs text-slate-500" title="{{ warn_count }} warning(s)">
      ({{ warn_count }}w)
    </span>
    {% endif %}
    {% endif %}
    {% endif %}
```

- [ ] **Step 4: Test the template renders without errors**

Run the Flask test client to fetch an expand route and verify no Jinja2 errors:

Run: `pytest tests/test_views.py -v -k expand`
Expected: PASS (existing tests should still work; new column defaults to NULL).

- [ ] **Step 5: Commit**

```bash
git add job_finder/db.py job_finder/web/templates/jobs/_resume_section.html
git commit -m "feat: show validation report badge on resume history entries"
```

---

### Task 10: Final Integration Test & Cleanup

**Files:**
- All test files

- [ ] **Step 1: Run complete test suite**

Run: `pytest tests/ -v --timeout=120`
Expected: All tests pass. Note the total count — it should be the previous count (266) plus new tests.

- [ ] **Step 2: Run the app and verify startup**

Run: `python run.py` (briefly, then Ctrl+C)
Expected: No import errors, scheduler starts, all jobs registered including `expiry_check`.

- [ ] **Step 3: Commit any final fixes**

If any tests needed adjustment:
```bash
git add -A
git commit -m "fix: test adjustments for expiry detection and resume quality integration"
```
