# Wave 2: Data Quality — Truncated/Poison JD Cleanup

## Summary

Three data quality issues affecting AI scoring accuracy:
1. LinkedIn login pages stored as jd_full (poison data, ~dozens of jobs)
2. Long descriptions never promoted to jd_full (99 jobs with >200 char descriptions but empty jd_full)
3. Meta-email text parsed as job titles (3 garbage rows)

## Fix A: LinkedIn Login Page Guard + Cleanup

### Problem

`data_enricher._fetch_direct_jd()` fetches the source URL and extracts text. LinkedIn URLs (e.g., `linkedin.com/jobs/view/12345/`) require authentication and return a login page instead of the JD. The enricher stores this login page text (152 chars: "Sign in\nWe're signing you in\nDiscover people, jobs, and more...") as `jd_full`, poisoning the scoring data.

### Guard (prevent future occurrences)

**File:** `job_finder/web/data_enricher.py`, in `_fetch_direct_jd()`

After `soup.get_text(separator="\n", strip=True)`, before returning, check for login/auth page signatures. Return `None` if detected.

Signatures to check (case-insensitive, against the extracted text):
- `"we're signing you in"` — LinkedIn login
- `"sign in or join"` — LinkedIn alt login
- `"please verify you are a human"` — CAPTCHA/bot detection
- `"access denied"` — WAF block

Implementation: a simple `any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES)` check. Short list, easy to extend.

### Cleanup (fix existing poison data)

**One-time data migration** (add to `db_migrate.py` migration list or run as standalone script):

```sql
-- Null out LinkedIn login page jd_full values
UPDATE jobs
SET jd_full = NULL, enrichment_tier = 'ddg'
WHERE jd_full LIKE '%signing you in%'
   OR jd_full LIKE '%sign in or join%';
```

Setting `enrichment_tier = 'ddg'` causes `_start_tier_index()` to return the index *after* ddg, so enrichment resumes from Haiku → SerpAPI → Sonnet. This intentionally skips both the free tier (which would hit the login wall again) and the DDG tier (which is unlikely to return a full JD for these jobs). This is migration 15 in `db_migrate.py`.

## Fix B: Description → jd_full Promotion

### Problem

ATS-scanned jobs and SerpAPI jobs store full JDs in the `description` column. The Sonnet evaluator reads only `jd_full`. These 99 jobs have substantive descriptions that were never copied to `jd_full`.

### Code change

**File:** `job_finder/web/data_enricher.py`, in `enrich_job()`, early in the function (before `_find_missing_fields` check).

If `jd_full` is empty/NULL but `description` has >200 chars, copy `description` to `jd_full` and persist to DB:

```python
if not job_row.get("jd_full") and job_row.get("description") and len(job_row["description"]) > 200:
    job_row["jd_full"] = job_row["description"]
    if conn is not None and job_row.get("dedup_key"):
        conn.execute(
            "UPDATE jobs SET jd_full = ? WHERE dedup_key = ? AND jd_full IS NULL",
            (job_row["description"][:8000], job_row.get("dedup_key")),
        )
        conn.commit()
```

The `AND jd_full IS NULL` guard prevents overwriting a real JD if one was populated between the read and write.

### ATS scanner also needs this

**File:** `job_finder/web/ats_scanner.py`, in `run_ats_scan()`, after `db.upsert_job(job)`.

ATS APIs (Lever, Greenhouse, Ashby) return full JDs. The scanner stores them in `Job.description` → DB `description` column. But Sonnet reads `jd_full`. After upsert, also write to `jd_full` when the description is substantive (>200 chars):

```python
raw_desc = job_dict.get("description") or ""
if len(raw_desc) > 200:
    conn.execute(
        "UPDATE jobs SET jd_full = COALESCE(jd_full, ?) WHERE dedup_key = ?",
        (raw_desc[:8000], job.dedup_key),
    )
    conn.commit()
```

`COALESCE(jd_full, ?)` ensures we don't overwrite an existing jd_full.

## Fix C: Meta-Email Parse Failures

### Problem

3 jobs have titles like "You'll receive notifications when new jobs match..." — these are LinkedIn notification emails that leaked through the meta-email filter.

### Cleanup

Delete the garbage rows:

```sql
DELETE FROM jobs WHERE title LIKE '%receive notifications%';
```

### Parser hardening

**File:** `job_finder/parsers/linkedin_parser.py`, in `_META_PATTERNS` list.

Add a pattern to catch this notification format:

```python
re.compile(r"you.ll receive notifications", re.IGNORECASE),
```

This catches the specific leak pattern. The existing patterns check the first 200 chars of the body preamble; this new pattern will match the notification text that slipped through.

## Testing

- Run the cleanup SQL against the live DB
- Verify affected jobs have NULL jd_full and enrichment_tier = 'ddg'
- Run enrichment backfill to test that cleaned jobs get re-enriched
- Test `_fetch_direct_jd()` with a LinkedIn URL — should return None
- Test LinkedIn parser with a notification email body — should return []
- Run `pytest tests/` for regression check

## Files Modified

| File | Change |
|------|--------|
| `data_enricher.py` | Add auth-wall guard in `_fetch_direct_jd()`; add description→jd_full promotion in `enrich_job()` |
| `ats_scanner.py` | Write jd_full after ATS job upsert when description is substantive |
| `linkedin_parser.py` | Add meta-email pattern for notification text |
| `db_migrate.py` | Add cleanup migration for poison jd_full values and garbage rows |
