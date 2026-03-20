# Job Expiry Detection & Resume Quality Upgrade

> Design spec for two features: (1) automated detection and archival of expired job listings, and (2) guideline-driven resume generation with post-generation validation.

---

## 1. Job Expiry Detection & Auto-Archive

### 1.1 Problem

Jobs in `discovered` and `reviewing` status may no longer be accepting applications. The existing stale detector archives based on `last_seen` age (30 days), but a job can close within days of posting. Users waste time reviewing dead listings.

### 1.2 Solution

New module `job_finder/web/expiry_checker.py` that actively checks whether job postings are still live, using a tiered signal cascade that short-circuits on the first definitive answer.

### 1.3 Signal Cascade

Signals are checked in order of cost and reliability. Each returns one of: `expired`, `live`, or `inconclusive`.

**Signal 1: ATS API Check (free, most reliable)**

For jobs with a linked company that has `ats_platform` set and whose `source_urls` contain an ATS URL, extract the posting ID from the URL path and query the ATS API directly.

Posting ID extraction patterns (new function `_extract_posting_id(url, ats_platform)`):
- Lever: URL format `jobs.lever.co/{slug}/{postingId}` — the UUID after the slug. Regex: `jobs\.lever\.co/[^/]+/([a-f0-9-]+)`
- Greenhouse: URL format `boards.greenhouse.io/{slug}/jobs/{jobId}` — the numeric ID after `/jobs/`. Regex: `boards\.greenhouse\.io/[^/]+/jobs/(\d+)`
- Ashby: URL format `jobs.ashbyhq.com/{slug}/{postingId}` — the UUID after the slug. Regex: `jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)`

API checks:
- Lever: `GET https://api.lever.co/v0/postings/{slug}/{postingId}` — 404 means expired
- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jobId}` — 404 means expired
- Ashby: `GET https://jobs.ashbyhq.com/api/non-user-graphql` with posting query — not found means expired

The existing `extract_ats_from_urls()` in `ats_scanner.py` extracts the slug; the new `_extract_posting_id()` extracts the individual posting identifier. Both are needed for the API call.

If the job has no ATS link, no extractable posting ID, or the company has no `ats_platform`, this signal returns `inconclusive`.

**Signal 2: Company Careers Page Check**

Uses existing `find_careers_url()` and `scrape_careers_page()` from `careers_scraper.py`. Requires the company to have a `homepage_url` in the `companies` table.

- Navigate from homepage to careers page using existing `_CAREERS_PATTERNS`
- Search for the job title using existing `_title_matches` from `ats_scanner.py`
- Title found = `live`; title not found = `inconclusive` (weaker signal — the careers page may not list all roles, or may be JS-rendered)
- Careers page unreachable = `inconclusive`

**Signal 3: SerpAPI Fallback (most expensive)**

Re-search using the `google_jobs` engine (same as existing `SerpAPISource`) with query `"{job_title}" "{company_name}"`. A result is considered a "match" if both the company name and a substantial substring of the job title appear in the result's title and company fields. If no matching result appears in the first batch (~10 results):

- Return `expired`
- Signal 3 is skipped (returns `inconclusive`) if `sources.serpapi.api_key` is empty OR `sources.serpapi.enabled` is false

**Short-circuit rules:**
- If any signal returns `expired`, stop and archive
- If any signal returns `live`, stop and mark as checked
- If all signals return `inconclusive`, skip the job (don't archive on uncertainty)

### 1.4 Target Jobs

Only jobs where `pipeline_status IN ('discovered', 'reviewing')`. Active applications (`applied`, `phone_screen`, `technical`, `onsite`, `offer`, `accepted`) are never touched by the expiry checker.

### 1.5 Actions

**On expiry confirmed:**
- Call existing `update_pipeline_status()` from `job_finder/db.py` which handles both the status UPDATE and the `pipeline_events` INSERT as a pair. Pass `source='expiry_check'`. The function must be extended with an optional `evidence: str = ""` parameter so it can write to the existing `evidence TEXT DEFAULT ''` column on `pipeline_events` (the column exists in the schema but is currently never populated by this function). Evidence string describes which signal triggered the expiry (e.g., `"lever_api 404"`, `"serpapi no_match"`).
- Update `expiry_checked_at` to current timestamp

**On live confirmed:**
- Update `expiry_checked_at` only (prevents re-checking within the configured interval)

**On all-inconclusive:**
- Do not update `expiry_checked_at` (will be retried next run)
- Log at DEBUG level

### 1.6 Database Changes

Migration 14 — two ALTER TABLE statements in a single migration (following the existing pattern in `db_migrate.py` where each migration is a list entry of SQL strings):

```sql
ALTER TABLE jobs ADD COLUMN expiry_checked_at TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_expiry_checked_at ON jobs(expiry_checked_at);
ALTER TABLE resume_generations ADD COLUMN validation_report TEXT DEFAULT NULL;
```

The index on `expiry_checked_at` supports the ordering query in Section 1.8 (`ORDER BY expiry_checked_at IS NULL DESC, expiry_checked_at ASC`).

### 1.7 Scheduling

APScheduler `CronTrigger` at 2:30 AM nightly (30 minutes after stale_detector at 2:00 AM). Registered in `scheduler.py` with:
- `max_instances=1`
- `coalesce=True`
- Activity tracker logging with `ACTION_SCHEDULED_EXPIRY_CHECK`

### 1.8 Rate Limiting & Batching

- Max 20 jobs per run (configurable: `expiry.batch_size`)
- 2-second delay between HTTP requests
- Jobs ordered by: `expiry_checked_at IS NULL` first, then oldest `expiry_checked_at`
- Jobs checked within `expiry.recheck_days` (default 3) are skipped
- Consecutive failures per company tracked in-memory (module-level dict); if a company's site fails 3 times in a row, skip for 7 days. This state intentionally resets on app restart — acceptable since the nightly batch is small and a fresh start after restart is harmless. Applies to Signal 2 (careers page) only; ATS APIs (Signal 1) are reliable enough to not need backoff, and SerpAPI (Signal 3) is a paid service with its own rate limiting

### 1.9 Configuration

New section in `config.yaml`:

```yaml
expiry:
  enabled: true
  batch_size: 20
  recheck_days: 3
```

The `expiry` section is optional — if absent, defaults to the values shown above. This avoids breaking existing `config.yaml` files on upgrade. SerpAPI availability for Signal 3 is determined by the existing `sources.serpapi.enabled` and `sources.serpapi.api_key` config (no separate toggle needed). The `config.py` loader does not need modification since the expiry checker reads these keys with `.get()` defaults.

### 1.10 Error Handling

- Network failures on individual jobs log a warning and skip to the next job (batch continues)
- If all 3 signals are inconclusive, the job is skipped without archiving
- Module follows `stale_detector.py` pattern: own `sqlite3.connect()`, no Flask `g.db`
- SerpAPI budget: Signal 3 is the fallback after two cheaper signals, so most jobs will short-circuit before reaching it. With a batch of 20 jobs/night, worst case is 20 SerpAPI calls/night (600/month). In practice, most jobs will have ATS links or careers pages, so actual SerpAPI usage should be well under 100 calls/month. No additional budget tracking beyond the existing batch_size limit

---

## 2. Resume Generation Quality Upgrade

### 2.1 Problem

The current resume generator uses a generic system prompt with a closed-world constraint but lacks the detailed resume-writing rules documented in `docs/resume_generation_guidelines.md`. This leads to:
- Soft skills appearing in the Skills section
- Bullets without quantified impact
- Inconsistent bullet counts across seniority levels
- Professional summaries that don't follow the prescribed formula
- Em dashes, excessive parentheses, and other typography issues

### 2.2 Solution

Three changes: (1) inject distilled guidelines into the generation prompt, (2) add a post-generation validation pass, and (3) migrate the style guide schema to incorporate the richer guidelines.

### 2.3 System Prompt Enrichment

A constant `_RESUME_GUIDELINES` (~1000-1500 tokens; measure after writing and adjust if needed) in `resume_generator.py` prepended to `_SYSTEM_PROMPT`. Contains distilled, actionable rules from the guidelines doc:

**Source Fidelity:**
- Never list a skill/tool the candidate hasn't used
- Gap mitigation via analog positioning only (describe real experience that addresses the same competency)

**Professional Summary:**
- 3-4 sentences max
- Formula: role archetype + years of experience, strongest achievement pattern with concrete example, JD-specific capabilities + forward-looking value prop
- Mirror JD's title/archetype language in opening
- Never use "seeking" — frame as practitioner bringing value

**Skills Section:**
- Hard skills and methodologies ONLY — never list soft skills
- Front-load to JD priority order
- 1-2 lines maximum, pipe-separated or category-labeled

**Bullet Writing:**
- Formula: Action Verb + What You Did + How/With What + Quantified Impact
- Rotate verbs — never start two consecutive bullets with the same verb
- 1-2 lines per bullet (3 lines absolute max, rare)
- Every bullet must pass the "so what?" test
- Anti-patterns: problem-identified openers (max once per role), methods-listing without outcome, redundant experimentation bullets, standalone soft-skill claims

**Bullet Count by Seniority:**
- Most recent role: 4-6 bullets
- Previous role at same company: 2-3
- Prior companies: 1-2 each
- Early career: 1 max

**Confidentiality:**
- Never include specific client names — use generic descriptors
- Omit specific team sizes unless JD explicitly requires it

**Typography:**
- No bold text within bullet content
- No em dashes anywhere — restructure with commas/semicolons
- Minimize parentheses — integrate details naturally
- Don't define well-known acronyms (ITT, DiD, RCT, ROI, KPI, etc.)

**JD Mirroring:**
- Use JD's exact terminology for tools/methodologies
- Never lift full phrases verbatim from JD requirements
- A JD phrase may appear at most once; reader should feel alignment, not pattern-matching

**Scope:** Both `generate_resume_single` and `_generate_single_variant` use the enriched prompt. The synthesis pass (`_synthesize_variants`) does NOT get the guidelines since it only selects/combines existing content.

### 2.4 Post-Generation Validator

New module: `job_finder/web/resume_validator.py`

**Phase 1: Sonnet Quality Audit**

A Sonnet call that checks the generated resume JSON against the Section 9 checklist from the guidelines doc. Returns structured output:

```python
VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
        },
    },
}
```

Categories: `content_integrity`, `structural`, `style`, `jd_alignment`, `readability`.

Severity levels: `error` (must fix) and `warning` (informational).

Inputs: generated resume JSON, the JD text, and the candidate profile (for source fidelity cross-check).

Checks:
- **Content integrity:** skills not in profile (fabrication), client names leaked, date mismatches
- **Structural:** summary exceeds 4 sentences, skills section exceeds 2 lines, bullet counts don't match seniority rules
- **Style:** consecutive same-verb bullets, bullets exceeding 2 lines, soft skills in skills section, em dashes present, bold in bullet content
- **JD alignment:** top 5 JD keywords each appear at least once, no verbatim JD phrase lifts
- **Readability:** "so what?" failures, vague language ("helped with", "assisted in"), passive voice

**Phase 2: Sonnet Auto-Fix (conditional)**

Only runs if Phase 1 found any `severity: "error"` violations. Sends the original resume JSON + the violation list to Sonnet with instructions to fix each violation while maintaining the closed-world constraint. Returns a corrected resume JSON matching `RESUME_SCHEMA`.

If Phase 1 returns only warnings or passes clean, Phase 2 is skipped.

**Re-validation guard:** After Phase 2 runs, there is NO re-validation. We trust Sonnet's fix pass. This avoids an infinite audit-fix loop. If the fix introduces new issues, they will be visible in the quality report but not automatically corrected. Max cost per resume: 2 additional Sonnet calls (1 audit + 1 fix).

**Quality report logging:**

The `validation_report` column (added in Migration 14, see Section 1.6) on `resume_generations` stores the Phase 1 JSON output. Visible in the resume section of the expanded job row.

### 2.5 Flow Change in `_generate_resume_background()`

Before (current):
```
generate → format .docx → upload to Drive
```

After:
```
generate → validate (Sonnet) → [fix if errors (Sonnet)] → format .docx → upload to Drive
```

### 2.6 Style Guide Migration

**Expanded `STYLE_GUIDE_SCHEMA`** — 9 new fields added to the existing 7:

| Field | Type | Source |
|-------|------|--------|
| `summary_formula` | string | Section 2 — the 3-sentence formula |
| `skills_format` | string | Section 3 — pipe-separated, 1-2 lines |
| `bullet_formula` | string | Section 4 — Action + What + How + Impact |
| `bullet_counts` | object | Section 4 — counts by seniority level |
| `confidentiality_rules` | string | Section 5 — no client names, no team sizes |
| `typography_rules` | string | Section 6 — no bold in bullets, no em dashes |
| `jd_mirroring_rules` | string | Section 7 — mirror strategy |
| `anti_patterns` | array[string] | Section 4 — patterns to avoid |
| `role_archetype` | string | Section 8 — IC-heavy, manager, analyst |

Existing fields (`bullet_style`, `verb_tense`, `section_order`, `tone`, `date_format`, `summary_style`) are preserved with their current values.

**Important:** The existing `STYLE_GUIDE_SCHEMA` in `resume_style_guide.py` has `"additionalProperties": False`. The schema object's `"properties"` dict MUST be extended with all 9 new field definitions, and `"required"` updated accordingly. Without this change, schema validation will reject any style guide containing the new fields. The `consistency_notes` field (existing, optional) is removed from the schema since its purpose is subsumed by the new structured fields.

**One-time migration function** `migrate_style_guide(config)`:
1. Load current `resume_style_guide.json`
2. Read `docs/resume_generation_guidelines.md`
3. Call Sonnet to merge: preserve existing preferences while populating new fields from the guidelines
4. Save expanded guide to `resume_style_guide.json`

Callable from a settings route or as a standalone function.

**`_build_style_guide_directives` updated** to emit richer directive strings from the new fields.

**Future `extract_style_guide` calls** (from PDF resume uploads) use the expanded schema.

### 2.7 Cost Impact

Per resume generation:
- **Before:** 1 Sonnet call (single) or 4 Sonnet + 1 Haiku (multi-version)
- **After:** +1 Sonnet audit call always; +1 Sonnet fix call only when errors found
- Expected: ~10-20% of resumes need the fix pass once the enriched prompt is in place

---

## 3. Files Changed

### New Files
| File | Purpose |
|------|---------|
| `job_finder/web/expiry_checker.py` | Job expiry detection module |
| `job_finder/web/resume_validator.py` | Post-generation quality audit + auto-fix |

### Modified Files
| File | Change |
|------|--------|
| `job_finder/web/scheduler.py` | Add expiry check job at 2:30 AM |
| `job_finder/web/db_migrate.py` | 2 new columns: `expiry_checked_at`, `validation_report` |
| `job_finder/web/resume_generator.py` | `_RESUME_GUIDELINES` constant + validator hook in background flow |
| `job_finder/web/resume_style_guide.py` | Expanded schema + migration function |
| `job_finder/web/activity_tracker.py` | New action constant `ACTION_SCHEDULED_EXPIRY_CHECK` |
| `job_finder/db.py` | Add optional `evidence` parameter to `update_pipeline_status()` |
| `config.yaml` | New `expiry:` section |

### Templates (minor)
| File | Change |
|------|---------|
| `jobs/_resume_section.html` | Show validation report badge/summary |

---

## 4. Out of Scope

- No UI for manual expiry checking (scheduled only)
- No changes to `pipeline_detector.py` (rejection emails already handled there)
- No changes to multi-version synthesis logic (inherits improved single-pass prompts)
- No changes to the `.docx` formatter or Drive upload flow
- No new tests for ATS API responses in initial implementation (follow-up: extend existing ATS scanner test patterns to cover individual posting lookups vs. listing all postings)
