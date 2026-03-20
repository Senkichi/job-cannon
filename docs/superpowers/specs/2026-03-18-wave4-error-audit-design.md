# Wave 4: Error & Failure Audit

## Summary

Comprehensive audit of all error handling, failure modes, and silent failures across the app's background processes and their supporting modules (excludes blueprint request handlers — those return HTTP errors to the user and are not prone to silent failures). Deliverable: categorized findings report with concrete fix specs for each issue.

## Audit Scope

### Files to audit

All modules that run background jobs or handle errors in critical paths:

| File | Why |
|------|-----|
| `web/scheduler.py` | APScheduler job wrappers, error recovery, missed run handling |
| `web/pipeline_runner.py` | Gmail/SerpAPI ingestion, Haiku/Sonnet scoring, budget alerts |
| `web/data_enricher.py` | Multi-tier enrichment, external API calls, cost gating |
| `web/ats_scanner.py` | ATS API probing/scanning, retry state machine, company upsert |
| `web/claude_client.py` | Anthropic API wrapper, cost tracking, budget gating |
| `web/haiku_scorer.py` | Haiku scoring, notification dispatch |
| `web/sonnet_evaluator.py` | Sonnet evaluation, fit analysis extraction |
| `web/stale_detector.py` | Nightly stale job detection (own DB connection) |
| `web/pipeline_detector.py` | Email-based pipeline state detection |
| `web/careers_scraper.py` | HTML careers page scraping |
| `web/expiry_checker.py` | Scheduled background job for expiry detection |
| `web/resume_generator.py` | Claude API calls for resume generation |
| `web/interview_prep.py` | Claude API calls for interview prep |
| `web/resume_feedback.py` | Claude API calls for resume feedback |
| `web/description_reformatter.py` | Claude API calls for description reformatting |
| `web/rejection_analyzer.py` | Claude API calls for rejection analysis |
| `parsers/*.py` | Email parsers (LinkedIn, Glassdoor, ZipRecruiter, Indeed) |
| `sources/gmail_source.py` | Gmail API auth, message fetching, body extraction |
| `sources/serpapi_source.py` | SerpAPI external calls |

### What to look for

| Category | Pattern | Severity |
|----------|---------|----------|
| **Swallowed exceptions** | `except Exception: pass` or `except: pass` with no logging | High |
| **Missing error logging** | Except blocks that catch but don't log | Medium |
| **Silent data corruption** | Partial writes without rollback; poison data persisted | High |
| **Resource leaks** | DB connections not closed in error paths; file handles left open | Medium |
| **Thread safety** | Shared mutable state between Flask request thread and APScheduler thread without locks | Medium |
| **Cost tracking gaps** | Claude API calls that bypass `call_claude()` cost recording | High |
| **Notification failures** | Notification dispatch that silently fails with no audit trail | Low |
| **Activity feed gaps** | Background jobs that don't log to runs table or user_activity on error | Medium |
| **Retry storms** | Error paths that could cause rapid retry loops | Medium |
| **Dead code / unreachable** | Import guards that mask real import errors | Low |

### What NOT to audit

- Code style or formatting issues
- Performance optimization opportunities (unless they cause failures)
- Feature gaps or missing functionality
- Test coverage (separate concern)

## Audit Methodology

1. **Static analysis:** Read each file and categorize every `try/except` block by what it catches, what it logs, and what it does on error
2. **Data analysis:** Query the live DB for evidence of past failures (parse_failures dir, runs table errors, enrichment_tier distributions, cost tracking anomalies)
3. **Cross-reference:** Check if error paths in one module are handled by callers (e.g., does `_fetch_direct_jd` returning None propagate correctly through `enrich_job`?)

## Deliverable Format

The audit produces a single findings document with this structure:

```markdown
## Findings

### [Category]: [Short description]

**File:** `path/to/file.py`, lines X-Y
**Severity:** High | Medium | Low
**Evidence:** What the code does wrong / what data shows
**Fix:** Concrete code change (pseudocode or exact diff)
```

Each finding includes its fix. Findings are grouped by severity (High first), then by file.

## Implementation

The fixes from the audit are collected into this spec and implemented as a single wave. Fixes are categorized as:

- **Quick fixes:** Add logging to bare `except: pass` blocks (mechanical, low risk)
- **Data fixes:** One-time SQL cleanup for corrupted data (run as migration)
- **Code fixes:** Structural changes to error handling (require tests)

## Files Modified

Determined by audit findings — this spec defines the audit process, not the specific fixes. The findings document will list exact files and changes.

## Testing

- `pytest tests/` after all fixes to ensure no regressions
- Manual verification that fixed error paths now produce log output
- DB query to verify any data cleanup migrations ran correctly
