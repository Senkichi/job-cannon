# Investigation: JD Quality, Salary Contamination, and Scorer Input

**Date:** 2026-04-02
**Status:** Investigation complete, awaiting remediation planning

## Context

User noticed "janky" job descriptions while browsing — JDs that contain the real job description but also random text from neighboring jobs on the same page. This investigation traces the full data pipeline to understand contamination scope, salary data reliability, and impact on AI scoring.

## Finding 1: 14% of JDs are contaminated

**231 of 1,572 JDs** (14%) contain at least one contamination marker:

| Marker | Count |
|--------|-------|
| Raw HTML tags (`<h2>`, `<p style>`) | 108 |
| "Skip to main content" | 88 |
| LinkedIn "Similar jobs" section | 71 |
| LinkedIn sign-in chrome | 57 |
| "Cookie Policy" text | 41 |
| "Get notified about new..." job alerts | 35 |
| "Be among the first applicants" | 20 |

**55 of the 71 "Similar jobs" JDs are exactly 8,000 chars** — they hit the `_MAX_JD_CHARS` truncation limit, meaning the full scraped page was even longer.

## Finding 2: Contamination severity varies by enrichment tier

| Tier | Total JDs | Contaminated | Rate |
|------|-----------|-------------|------|
| agentic | 50 | 30 | **60%** |
| exhausted | 359 | 130 | **36%** |
| free | 338 | 42 | 12% |
| ddg | 366 | 22 | 6% |
| serpapi | 414 | 1 | <1% |

SerpAPI enrichment is cleanest (structured Google Jobs API). The free and exhausted tiers are worst because they fetch LinkedIn guest pages that include the full page chrome and "Similar jobs" sidebar.

## Finding 3: The contamination source is LinkedIn page scraping

The primary path: `fetch_linkedin_jd()` (`enrichment_tiers.py:322-360`) targets the `div.show-more-less-html__markup` container. When it finds it, JDs are clean. When it fails (auth wall, changed HTML, etc.), it returns None.

BUT: other tiers (DDG, free URL fetch) can later fetch the same LinkedIn URL through `fetch_direct_jd()`, which grabs the **entire page** — job description, page chrome, "Similar jobs" with salary data from other companies, sign-in prompts, and cookie notices. This text gets stored as `jd_full` up to 8,000 chars.

Evidence: the STAFFWORXS job's `jd_full` starts with `"STAFFWORXS hiring Credit Risk Strategy ... | LinkedIn\nSkip to main content"` — a full page scrape, not the targeted container.

## Finding 4: Salary cross-contamination is real but limited

10 contaminated JDs also have salary data. The "Similar jobs" sections contain salary ranges from OTHER companies (e.g., "Data Analyst @ Rain: $70,000 - $90,000", "Data Analyst I @ Acorns: $121,000 - $140,000"). When AI extraction (`enrich_job`, `enrich_job_sonnet`) runs on these fragments, it can attribute a neighbor's salary to the target job.

Example: `Analytics Lead @ Subcraft.ai` — `jd_full` contains "Similar jobs" listing "$70K-$90K" and "$121K-$140K" from Rain and Acorns respectively.

However: most contaminated JDs (61 of 71 with "Similar jobs") have **NULL salary** — the AI models mostly did NOT extract salary from the neighbor data, likely because the prompt says "extract only what is explicitly stated" and the Similar jobs section is structurally separate.

## Finding 5: Haiku scorer is unaffected, Sonnet is affected

Critical architectural difference:

| Scorer | Input field | Source | Contaminated? |
|--------|------------|--------|---------------|
| **Haiku** | `description` (first 1,200 chars + requirements snippet, max 2,000) | Email parsers, SerpAPI highlights | **No** — 0 contaminated descriptions found |
| **Sonnet** | `jd_full` (full text, untruncated) | Enrichment pipeline | **Yes** — 231 affected JDs |

Haiku reads the `description` column (populated from email parser metadata and SerpAPI `job_highlights`). Sonnet reads `jd_full` (populated from web scraping and AI extraction). The contamination lives exclusively in `jd_full`.

## Finding 6: Validation functions exist but are inconsistently applied

| Validation | DDG tier | Free URL | SerpAPI | Haiku extract | Auto-promote |
|------------|----------|----------|---------|---------------|-------------|
| `has_jd_content()` | Yes | No | No | No | No |
| `company_name_in_text()` | Yes | No | No | No | No |
| `is_chrome_or_login_page()` | Yes | Yes | No | No | No |
| `is_stub_jd()` (<200 chars) | Yes | Yes | Yes | Yes | No |

The DDG tier has the best validation. The free-tier URL fetch and auto-promotion path (`description` > 200 chars -> `jd_full`) have minimal validation. No tier checks for multi-job contamination.

## Finding 7: Garbage job entries from parser edge cases

At least one job has a LinkedIn "See all jobs" URL as its title and "New jobs from your other alerts" as its company name — a parser artifact from LinkedIn email meta-links being treated as job postings.

## Impact Summary

| Impact Area | Severity | Scope |
|-------------|----------|-------|
| JD display quality | Medium | 231 jobs (14%) show chrome/neighbor text |
| Sonnet scoring accuracy | Medium | 231 jobs scored with contaminated context |
| Haiku scoring accuracy | **None** | Uses `description` field (clean) |
| Salary accuracy | Low-Medium | ~10 jobs may have cross-contaminated salary |
| Exclusion filter accuracy | Low | Salary-based exclusions could use wrong salary |

## Key Files

| Component | File | Lines | Function |
|-----------|------|-------|----------|
| LinkedIn targeted fetch | `web/enrichment_tiers.py` | 322-360 | `fetch_linkedin_jd()` |
| Generic URL fetch | `web/enrichment_tiers.py` | 363-414 | `fetch_direct_jd()` |
| DDG URL fetch (best validation) | `web/enrichment_tiers.py` | 864-928 | `fetch_ddg_jds()` |
| Haiku extraction prompt | `web/enrichment_tiers.py` | 931-1003 | `extract_with_haiku()` |
| Sonnet extraction prompt | `web/enrichment_tiers.py` | 554-640 | `extract_with_sonnet()` |
| Auto-promotion path | `web/data_enricher.py` | 152-163 | `enrich_job()` |
| Fragment resolution | `web/data_enricher.py` | 632-668 | `_resolve_from_fragments()` |
| JD content validation | `web/enrichment_tiers.py` | 219-234 | `has_jd_content()` |
| Company name validation | `web/enrichment_tiers.py` | 162-183 | `company_name_in_text()` |
| Chrome/login detection | `web/enrichment_tiers.py` | 237-270 | `is_chrome_or_login_page()` |
| Haiku scorer input | `web/haiku_scorer.py` | 175-258 | `score_job_haiku()` |
| Sonnet scorer input | `web/sonnet_evaluator.py` | 136-239 | `evaluate_job_sonnet()` |
| Description snippet builder | `web/haiku_scorer.py` | 112-172 | `build_description_snippet()` |

## Recommendations (awaiting remediation planning)

1. **Post-scrape sanitization** — strip LinkedIn page chrome, "Similar jobs" sections, and HTML tags from `jd_full` before storage
2. **Apply validation consistently** — `has_jd_content()` and `company_name_in_text()` should gate all tiers, not just DDG
3. **Retroactive cleanup** — scan existing `jd_full` values, truncate at contamination markers ("Similar jobs", "Cookie Policy", etc.)
4. **Separate JD quality from salary enrichment** — salary should only come from structured sources (SerpAPI `detected_extensions`, parser extraction), not from AI extraction on scraped page fragments

## Related: Batch Scoring Issue (same session)

Also diagnosed in this session: the "batch haiku" button scored 10 jobs then showed "skipped the rest" because:
- 244 of 257 unscored jobs have `salary_max` below the `$127,500` salary floor (`min_salary * 0.85`)
- The exclusion filter correctly rejects them, but the dashboard shows "267 unscored" as if they're all scoreable
- Progress reporting bug: excluded jobs `continue` past the DB progress flush, and `_finish_session` doesn't write final tallies
- Files: `web/blueprints/batch_scoring.py:294-359` (batch loop), `web/exclusion_filter.py:16-77` (salary floor check)
