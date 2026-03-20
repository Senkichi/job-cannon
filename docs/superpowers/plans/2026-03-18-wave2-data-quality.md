# Wave 2: Data Quality Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three data quality issues: LinkedIn login pages stored as JD text, descriptions not promoted to jd_full, and meta-email parse failures.

**Architecture:** Auth-wall guard in the enrichment pipeline's direct URL fetch, description-to-jd_full promotion in both the enricher and ATS scanner, LinkedIn parser hardening, and a DB migration for cleanup.

**Tech Stack:** Python, SQLite, BeautifulSoup, Anthropic API (existing patterns)

**Spec:** `docs/superpowers/specs/2026-03-18-wave2-data-quality-design.md`

---

## Chunk 1: Auth-Wall Guard & Tests

### Task 1: Write test for auth-wall detection in _fetch_direct_jd

**Files:**
- Test: `tests/test_data_enricher.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_data_enricher.py` (or create if needed):

```python
def test_fetch_direct_jd_rejects_linkedin_login_page(monkeypatch):
    """_fetch_direct_jd should return None when the page is a login wall."""
    import requests
    from unittest.mock import Mock
    from job_finder.web.data_enricher import _fetch_direct_jd

    login_html = """<html><body>
    <h1>Sign in</h1>
    <p>We're signing you in</p>
    <p>Discover people, jobs, and more.</p>
    </body></html>"""

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.text = login_html
    mock_resp.raise_for_status = Mock()

    monkeypatch.setattr(requests, "get", lambda *a, **kw: mock_resp)

    result = _fetch_direct_jd("https://www.linkedin.com/jobs/view/12345/")
    assert result is None


def test_fetch_direct_jd_rejects_captcha_page(monkeypatch):
    """_fetch_direct_jd should return None for CAPTCHA/bot detection pages."""
    import requests
    from unittest.mock import Mock
    from job_finder.web.data_enricher import _fetch_direct_jd

    captcha_html = "<html><body><p>Please verify you are a human</p></body></html>"

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.text = captcha_html
    mock_resp.raise_for_status = Mock()

    monkeypatch.setattr(requests, "get", lambda *a, **kw: mock_resp)

    result = _fetch_direct_jd("https://example.com/job/123")
    assert result is None


def test_fetch_direct_jd_accepts_real_jd(monkeypatch):
    """_fetch_direct_jd should return text for a real job description page."""
    import requests
    from unittest.mock import Mock
    from job_finder.web.data_enricher import _fetch_direct_jd

    real_html = "<html><body><h1>Senior Data Scientist</h1><p>We are looking for a data scientist with 5+ years of experience in machine learning and statistical modeling. " + "x" * 300 + "</p></body></html>"

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.text = real_html
    mock_resp.raise_for_status = Mock()

    monkeypatch.setattr(requests, "get", lambda *a, **kw: mock_resp)

    result = _fetch_direct_jd("https://example.com/job/123")
    assert result is not None
    assert "data scientist" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_data_enricher.py -k "fetch_direct_jd" -v 2>&1 | tail -10`

Expected: FAIL (no auth-wall guard yet)

- [ ] **Step 3: Implement the auth-wall guard**

In `job_finder/web/data_enricher.py`, add the signatures constant near the top (after the existing constants):

```python
# Auth-wall / login page signatures — if any appear in fetched text, the page
# is not a real JD. Checked case-insensitively against extracted text.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]
```

Then in `_fetch_direct_jd()`, after `text = soup.get_text(separator="\n", strip=True)` and before the return, add:

```python
        # Reject login walls, CAPTCHAs, and access-denied pages
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth wall detected for '%s', skipping", url)
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_data_enricher.py -k "fetch_direct_jd" -v`

Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/data_enricher.py tests/test_data_enricher.py
git commit -m "fix: reject LinkedIn login pages and auth walls in JD fetcher"
```

### Task 2: Description → jd_full promotion in enricher

**Files:**
- Modify: `job_finder/web/data_enricher.py:120-132` (in `enrich_job()`)

- [ ] **Step 1: Write the failing test**

```python
def test_enrich_job_promotes_long_description_to_jd_full():
    """enrich_job should copy description to jd_full when description > 200 chars and jd_full is empty."""
    import sqlite3
    from job_finder.web.data_enricher import enrich_job

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY, jd_full TEXT, description TEXT, enrichment_tier TEXT, salary_min INTEGER)")
    long_desc = "A" * 250
    conn.execute("INSERT INTO jobs VALUES (?, NULL, ?, NULL, NULL)", ("test-key", long_desc))
    conn.commit()

    job_row = {"dedup_key": "test-key", "description": long_desc, "jd_full": None, "title": "Test", "company": "Test Co", "enrichment_tier": None, "salary_min": None}
    result = enrich_job(job_row, conn=conn)

    # Check DB was updated
    row = conn.execute("SELECT jd_full FROM jobs WHERE dedup_key = 'test-key'").fetchone()
    assert row[0] == long_desc
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_enricher.py -k "promotes_long_description" -v`

Expected: FAIL

- [ ] **Step 3: Implement description promotion**

In `job_finder/web/data_enricher.py`, in `enrich_job()`, after the `if current_tier == "exhausted": return {}` check and before `missing = _find_missing_fields(job_row)`, add:

```python
        # Promote long description to jd_full if jd_full is missing.
        # ATS scanner and SerpAPI store full JDs in 'description' but
        # Sonnet evaluator reads only 'jd_full'. Copy if substantive.
        if not job_row.get("jd_full") and job_row.get("description") and len(job_row["description"]) > 200:
            job_row["jd_full"] = job_row["description"]
            if conn is not None:
                dedup_key = job_row.get("dedup_key")
                if dedup_key:
                    try:
                        conn.execute(
                            "UPDATE jobs SET jd_full = ? WHERE dedup_key = ? AND jd_full IS NULL",
                            (job_row["description"][:_MAX_JD_CHARS], dedup_key),
                        )
                        conn.commit()
                        logger.debug("Promoted description to jd_full for '%s'", job_row.get("title"))
                    except Exception as e:
                        logger.debug("Failed to promote description to jd_full: %s", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_data_enricher.py -k "promotes_long_description" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/data_enricher.py tests/test_data_enricher.py
git commit -m "fix: promote long descriptions to jd_full in enrichment pipeline"
```

### Task 3: jd_full population in ATS scanner

**Files:**
- Modify: `job_finder/web/ats_scanner.py:1091-1094` (after `db.upsert_job(job)`)

- [ ] **Step 1: Add jd_full write after upsert**

In `run_ats_scan()`, after `is_new = db.upsert_job(job)` (around line 1091), add:

```python
                            is_new = db.upsert_job(job)

                            # ATS APIs return full JDs — store in jd_full for AI scoring.
                            # description column holds the same text, but Sonnet evaluator
                            # reads jd_full exclusively. Only write if substantive (>200 chars).
                            raw_desc = job_dict.get("description") or ""
                            if len(raw_desc) > 200:
                                try:
                                    conn.execute(
                                        "UPDATE jobs SET jd_full = COALESCE(jd_full, ?) WHERE dedup_key = ?",
                                        (raw_desc[:8000], job.dedup_key),
                                    )
                                    conn.commit()
                                except Exception as jd_err:
                                    logger.debug("Failed to store jd_full for %s: %s", job.dedup_key, jd_err)

                            if is_new:
```

Note: the `if is_new:` block that follows should remain unchanged.

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/ats_scanner.py
git commit -m "fix: store ATS full JDs in jd_full column for Sonnet scoring"
```

## Chunk 2: Parser Fix & DB Migration

### Task 4: Harden LinkedIn parser meta-email detection

**Files:**
- Modify: `job_finder/parsers/linkedin_parser.py:33-38`
- Test: `tests/test_linkedin_parser.py` (or existing test file)

- [ ] **Step 1: Write the failing test**

```python
def test_notification_email_rejected():
    """LinkedIn parser should reject 'you'll receive notifications' emails."""
    from job_finder.parsers.linkedin_parser import parse_linkedin_alert

    notification_body = "You'll receive notifications when new jobs match your search criteria.\n\nManage your job alerts at https://www.linkedin.com/jobs/alerts/"
    result = parse_linkedin_alert(notification_body, None)
    assert result == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ -k "notification_email_rejected" -v`

Expected: FAIL (parser doesn't detect this pattern yet)

- [ ] **Step 3: Add the meta-email pattern**

In `job_finder/parsers/linkedin_parser.py`, add to the `_META_PATTERNS` list:

```python
_META_PATTERNS = [
    re.compile(r"^\d+\+?\s+new\s+jobs?\s+match", re.IGNORECASE | re.MULTILINE),
    re.compile(r"job alert digest|weekly digest", re.IGNORECASE),
    re.compile(r"you have \d+ new jobs?", re.IGNORECASE),
    re.compile(r"^\d+ jobs? found", re.IGNORECASE | re.MULTILINE),
    re.compile(r"you.ll receive notifications", re.IGNORECASE),  # notification leak fix
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ -k "notification_email_rejected" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/parsers/linkedin_parser.py tests/test_linkedin_parser.py
git commit -m "fix: reject LinkedIn notification emails in parser meta-email filter"
```

### Task 5: DB migration for poison data cleanup

**Files:**
- Modify: `job_finder/web/db_migrate.py` (add migration 15)

- [ ] **Step 1: Add migration 15 to the MIGRATIONS list**

At the end of the `MIGRATIONS` list in `db_migrate.py`, add:

```python
    # Migration 15: Clean up poison jd_full data and garbage parse failures.
    # - Null out LinkedIn login page text stored as jd_full, reset enrichment_tier
    #   to 'ddg' so re-enrichment resumes from Haiku (skipping free+DDG tiers).
    # - Delete garbage rows where meta-email text became the job title.
    # - Promote long descriptions to jd_full where jd_full is missing.
    [
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = 'ddg'
           WHERE jd_full LIKE '%signing you in%'
              OR jd_full LIKE '%sign in or join%'""",

        "DELETE FROM jobs WHERE title LIKE '%receive notifications%'",

        """UPDATE jobs
           SET jd_full = description
           WHERE jd_full IS NULL
             AND description IS NOT NULL
             AND length(description) > 200""",
    ],
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/db_migrate.py
git commit -m "fix: add migration 15 — clean poison JDs, garbage rows, promote descriptions"
```

### Task 6: Verify migration against live DB

- [ ] **Step 1: Start the app to trigger migration**

Run: `python run.py` (migration runs on startup)

- [ ] **Step 2: Verify cleanup results**

```python
python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
poison = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE jd_full LIKE '%signing you in%'\").fetchone()[0]
garbage = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE title LIKE '%receive notifications%'\").fetchone()[0]
promoted = conn.execute(\"SELECT COUNT(*) FROM jobs WHERE jd_full IS NOT NULL AND length(jd_full) > 200\").fetchone()[0]
version = conn.execute('PRAGMA user_version').fetchone()[0]
print(f'Poison JDs remaining: {poison}')
print(f'Garbage rows remaining: {garbage}')
print(f'Jobs with full JD: {promoted}')
print(f'DB version: {version}')
conn.close()
"
```

Expected: Poison JDs = 0, Garbage rows = 0, DB version = 15
