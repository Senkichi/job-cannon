# Comprehensive Post-Merge Remediation Plan

**Date**: 2026-04-10
**Supersedes**: `2026-04-10-post-merge-fixes.md` (which covered 6 of these issues)
**Context**: Exhaustive review of commit `909c453` (141-file merge of CLI oneshot refactor + career-ops enhancements). Full test suite: **2249 pass, 154 fail, 1 collection error** across 17 test files. All e2e failures (13) cascade from P0 root causes.

**Verification command**: `uv run --active pytest tests/ -q --tb=short --ignore=tests/test_gemini_provider.py`

---

## Phase 0: Runtime Crashers (App Won't Start / Pages 500)

These must be fixed first. The app is non-functional in its current state for core workflows.

### P0-1: `get_filtered_jobs()` Signature Mismatch — JOBS PAGE DOWN

**Failures**: ~36 in test_views.py + 10 e2e + cascading failures in other test files
**Symptom**: `TypeError: get_filtered_jobs() got an unexpected keyword argument 'min_score'`

**Root cause**: `jobs.py:57-77` `_get_filter_kwargs()` returns 6 keys that `db.py:447` `get_filtered_jobs()` doesn't accept:
- `min_score` (float) — line 67
- `max_score` (float) — line 68
- `salary_min` (int) — line 69
- `source` (str) — line 70
- `date_from` (str, ISO date) — line 71
- `date_to` (str, ISO date) — line 72

**Fix in `job_finder/db.py`** — add 6 parameters to `get_filtered_jobs()` signature (after `show_hidden`):
```python
def get_filtered_jobs(
    conn: sqlite3.Connection,
    status: str | list[str] | None = None,
    location: str | None = None,
    posted_within: str | None = None,
    freshness: str | None = None,
    sort_by: str = "score",
    sort_dir: str = "DESC",
    limit: int = 100,
    hide_stale: bool = False,
    show_hidden: bool = False,
    # --- New filter parameters (from filter bar overhaul) ---
    min_score: float | None = None,
    max_score: float | None = None,
    salary_min: int | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
```

Then add SQL WHERE clauses in the query-building section (after the existing filters):
- `min_score`: `AND COALESCE(sonnet_score, haiku_score) >= ?`
- `max_score`: `AND COALESCE(sonnet_score, haiku_score) <= ?`
- `salary_min`: `AND salary_min >= ?`
- `source`: `AND sources LIKE ?` (with `f'%"{source}"%'` as param value — JSON array match)
- `date_from`: `AND first_seen >= ?`
- `date_to`: `AND first_seen <= ? || ' 23:59:59'` (end-of-day inclusive)

All values are parameterized (appended to `params` list), not interpolated. No SQL injection risk.

**Validation**: The existing `sort_by` allowlist pattern remains unchanged. New params are all `WHERE` value comparisons.

---

### P0-2: `DEFAULT_DAILY_BUDGET_USD` Missing Jinja2 Global — SETTINGS PAGE DOWN

**Failures**: 5 in test_views.py + 3 in test_settings.py + 2 in test_resume.py + 3 e2e
**Symptom**: `jinja2.exceptions.UndefinedError: 'DEFAULT_DAILY_BUDGET_USD' is undefined`

**Root cause**: `settings/index.html:330,332` references `DEFAULT_DAILY_BUDGET_USD`. The constant exists at `claude_client.py:179` and is imported by `costs.py:14` and `dashboard.py:16`, but was never registered as a Jinja2 global in `__init__.py`.

**Fix in `job_finder/web/__init__.py`**:

1. Add to imports at top of file (after line 31):
```python
from job_finder.web.claude_client import DEFAULT_DAILY_BUDGET_USD
```

2. Add to Jinja2 globals block (after line 127, the `DEFAULT_MODEL_SONNET` line):
```python
app.jinja_env.globals["DEFAULT_DAILY_BUDGET_USD"] = DEFAULT_DAILY_BUDGET_USD
```

---

### P0-3: Missing `skipped` Template Variable — BATCH SCORING PROGRESS DOWN

**Failures**: 6 in test_views.py (TestBatchScoreHaikuStart, TestBatchScoreStatus, TestBatchScoreCancel)
**Symptom**: `jinja2.exceptions.UndefinedError: 'skipped' is undefined`

**Root cause**: `batch_scoring.py:197-204` renders `_batch_score_progress.html` passing `label`, `session_id`, `total`, `scored`, `cancelling` — but NOT `skipped`. The template at line 14 uses `scored + skipped` and line 27 uses `(scored + skipped) / total`.

**Fix in `job_finder/web/blueprints/batch_scoring.py`** — add `skipped` to the render call at line 197-204:
```python
return render_template(
    "dashboard/_batch_score_progress.html",
    label=label,
    session_id=session_id,
    total=session["total"],
    scored=session["scored"],
    skipped=session["skipped"],       # <-- ADD THIS
    cancelling=(status == "cancelling"),
)
```

The `session` row already has a `skipped` column (it's in batch_score_sessions table). It's just not being passed to the template.

---

## Phase 1: Missing/Renamed Functions (ImportError Crashes)

Functions removed or renamed during merge but still referenced by tests and/or production code.

### P1-1: `is_short_auth_page` Missing from `enrichment_tiers.py`

**Failures**: 13 in test_enrichment_tiers.py (TestIsShortAuthPage)
**Also breaks**: `agentic_enricher.py:174` imports it at runtime — would crash when agentic enrichment runs

**Root cause**: Function was planned (exists in `ENRICHMENT_FIX_DIFF.txt:1068`) but never landed in `enrichment_tiers.py`.

**Fix**: Implement `is_short_auth_page()` in `job_finder/web/enrichment_tiers.py`. The function detects auth-wall/CAPTCHA pages that return login HTML instead of JDs:

```python
def is_short_auth_page(text: str) -> bool:
    """Return True if text looks like a short auth-wall or CAPTCHA page.

    Detection: page is under 2000 chars AND the first 500 chars contain
    an auth/bot signal keyword.
    """
    if not text or len(text) >= 2000:
        return False
    prefix = text[:500].lower()
    signals = [
        "sign in", "log in", "login", "captcha", "just a moment",
        "access denied", "verify you are human",
    ]
    return any(s in prefix for s in signals)
```

Place it near the top of the file (after the imports, before `fetch_direct_jd`), since `fetch_direct_jd` already references "auth-wall" detection at line 85 and this function should be used there.

**Test expectation verification** (from `test_enrichment_tiers.py:34-130`):
- `len < 2000` + signal in first 500 chars → True
- `len == 1999` + signal → True
- `len == 2000` + signal → False
- `len == 2001` + signal → False
- Short page, no signal → False
- Empty string → False
- Signal is case-insensitive
- Signal beyond 500 chars → False (only checks prefix)
- "access denied" and "verify you are human" are signals

---

### P1-2: `search_ddg_web` and `fetch_ddg_jds` Missing from `enrichment_tiers.py`

**Failures**: 6 in TestSearchDdgWeb + 7 in TestFetchDdgJds = 13 in test_enrichment_tiers.py + 5 in test_agentic_enricher.py

**Root cause**: These functions were planned (exist in `ENRICHMENT_FIX_DIFF.txt:1293,1355`) but never landed.

**Decision required**: These are substantial functions (DDG web search + JD fetching pipeline). Two options:

**Option A — Implement the functions** (if the enrichment pipeline needs DDG web search):
- `search_ddg_web(title, company)` → `dict` with `urls` and `snippets` keys
- `fetch_ddg_jds(urls)` → `tuple[Optional[str], Optional[str]]` (jd_text, source_url)
- Both need blocked domain filtering and priority sorting per test expectations
- See `ENRICHMENT_FIX_DIFF.txt` lines 1293-1410 for the intended implementation

**Option B — Delete the tests** (if DDG web search is dead scope):
- Delete `TestSearchDdgWeb` class (6 tests, lines ~330-470 in test_enrichment_tiers.py)
- Delete `TestFetchDdgJds` class (7 tests, lines ~478-575 in test_enrichment_tiers.py)
- Check if `agentic_enricher.py` references these and remove those imports too

**Recommendation**: Option A. The `agentic_enricher.py:174` import path suggests this is live functionality, not dead scope. Also `ENRICHMENT_FIX_DIFF.txt` has the full implementation ready to port.

---

### P1-3: `cleanup_invalid_company_data`, `run_registry_hygiene`, `run_scheduled_enrichment` Missing from `backfill_companies.py`

**Failures**: 18 in test_backfill_companies.py
**Also breaks**: `scheduler.py:508,526` imports `run_scheduled_enrichment` and `run_registry_hygiene` — scheduler would crash when these jobs fire

**Root cause**: Functions were in the stashed branch but didn't survive the merge into `backfill_companies.py`.

**Current `backfill_companies.py` public functions** (from grep):
- `fuzzy_match_company` (line 57)
- `cleanup_denylist_companies` (line 100)
- `find_duplicate_companies` (line 155)
- `find_fuzzy_false_positives` (line 187)
- `verify_homepage_urls` (line 242)
- `verify_all_linkable_jobs_linked` (line 293)
- `link_jobs_to_companies` (line 342)
- `run_ats_probing` (line 439)
- `run_ddg_enrichment` (line 465)

**Missing functions to implement**:

1. **`cleanup_invalid_company_data(conn, config=None)`** — Nulls `company_id` for jobs linked to rejected/denylist companies; re-normalizes linkable ones. Test expectations:
   - Rejected company → nulls `company_id` but preserves `company` (raw name)
   - Normalizable company → links to correct existing record
   - Never mutates `jobs.company` column (only `company_id`)
   - Idempotent

2. **`run_registry_hygiene(db_path, config)`** — Orchestrator that runs `cleanup_denylist_companies()` + `run_orphan_cleanup()` in sequence. Test expectations:
   - Deletes denylist-matching companies
   - Returns dict with all expected summary keys

3. **`run_scheduled_enrichment(db_path, config)`** — Enriches company metadata with retry-aware backoff. Test expectations:
   - Skips companies in backoff window
   - Called by scheduler at weekly interval

**Implementation note**: Read the test expectations in `test_backfill_companies.py` lines 1021-1172 carefully before implementing. The tests define the exact contract.

---

### P1-4: `ScoringResult` vs dict Mismatch in `backfill_enrichment.py`

**Failures**: 6 in test_backfill_enrichment.py
**Symptom**: `AttributeError: 'ScoringResult' object has no attribute 'get'`

**Root cause**: `backfill_enrichment.py:281` calls `evaluate_job_sonnet()` which returns `ScoringResult(data=dict, status=str)` (a NamedTuple, `scoring_types.py:60`). But lines 287-289 call `.get()` on the result as if it were a dict.

**Fix in `job_finder/web/backfill_enrichment.py`** — unwrap the ScoringResult:
```python
# BEFORE (line 281-289):
result = evaluate_job_sonnet(job_row, profile, conn, config)
if result is None:
    logger.debug("Sonnet eval returned None for '%s'", dedup_key)
    continue
score = result.get("score")
summary = result.get("summary")
fit_analysis = result.get("fit_analysis")

# AFTER:
scoring_result = evaluate_job_sonnet(job_row, profile, conn, config)
if scoring_result is None or scoring_result.status != "success" or scoring_result.data is None:
    logger.debug("Sonnet eval returned %s for '%s'",
                 scoring_result.status if scoring_result else "None", dedup_key)
    continue
result = scoring_result.data
score = result.get("score")
summary = result.get("summary")
fit_analysis = result.get("fit_analysis")
```

Also check if `score_job_haiku` has the same issue anywhere in `backfill_enrichment.py` — search for `.get(` on haiku results too.

---

### P1-5: `company_enricher` Tests Mock Removed `DDGS` Class

**Failures**: 4 in test_company_enricher.py
**Symptom**: `AttributeError: <module 'job_finder.web.company_enricher'> does not have the attribute 'DDGS'`

**Root cause**: `company_enricher.py` now imports `search_duckduckgo` from `enrichment_tiers` (line 10) instead of using `DDGS` directly. Tests still mock the old path.

**Fix in `tests/test_company_enricher.py`** — change mock targets:
```python
# BEFORE:
with patch("job_finder.web.company_enricher.DDGS") as MockDDGS:
    MockDDGS.return_value.__enter__.return_value.text.return_value = [
        {"body": "Acme has 5000 employees in the software industry"}
    ]

# AFTER:
with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_search:
    mock_search.return_value = "Acme has 5000 employees in the software industry"
```

Apply this pattern to all 4 failing tests. For "no results" test, return `None` or `""`. For "exception" test, set `mock_search.side_effect = Exception(...)`.

Read `company_enricher.py:85` to confirm the actual call pattern before writing the mock — the function calls `search_duckduckgo(query)` and expects a string return.

---

## Phase 2: Behavioral Regressions (Tests Fail With Wrong Results)

### P2-1: `careers_scraper.py` Subdomain/Redirect Detection Broken

**Failures**: 6 in test_careers_scraper.py (TestCareersSubdomainDetection + TestMetaRefreshDetection)
**Symptom**: `assert None == 'https://careers.example.com/'`

**Root cause**: `find_careers_url()` was modified during the merge and no longer detects:
- HTTP redirects to `careers.*`, `jobs.*`, `work.*` subdomains
- Absolute `<a href>` links to careers subdomains
- `<meta http-equiv="refresh">` redirects to careers paths/subdomains

**Investigation needed**: Read `careers_scraper.py:262-348` (`find_careers_url`) and compare the redirect/subdomain detection logic against the test expectations at `test_careers_scraper.py:505-625`. The tests use `_mock_response()` which sets `resp.url` to the final URL after redirect — verify the function checks `resp.url` for subdomain detection.

Key test expectations:
1. `resp.url == "https://careers.example.com/"` → should return that URL directly (redirect detection)
2. `resp.url == "https://jobs.example.com/"` → same
3. `<a href="https://careers.company.com/">` in body → should return that URL
4. `<meta http-equiv="refresh" content="0;url=https://careers.example.com/">` → should follow and return

**Fix approach**: Add subdomain detection logic early in `find_careers_url()`:
```python
# After fetching the URL and getting resp:
parsed_final = urlparse(resp.url)
parsed_orig = urlparse(homepage_url)
# If we were redirected to a careers/jobs/work subdomain, return directly
if parsed_final.netloc != parsed_orig.netloc:
    subdomain = parsed_final.netloc.split(".")[0].lower()
    if subdomain in ("careers", "jobs", "work", "apply"):
        return resp.url
```

Also add meta-refresh detection and absolute careers-subdomain href detection in the link scanning loop.

---

### P2-2: `agentic_enricher` Test Failures

**Failures**: 5 in test_agentic_enricher.py
**Root cause**: Multiple — the agentic_enricher imports `is_short_auth_page` (P1-1 dependency) and also has LinkedIn routing test expectations that may have changed.

**Fix dependency**: Fix P1-1 first (`is_short_auth_page`), then re-run. Remaining failures need investigation of the actual behavior vs test expectations for LinkedIn URL routing in `agentic_enricher.py:150-190`.

---

### P2-3: `test_companies.py` Failures

**Failures**: 13 (TestIndexPagination: 6, TestIndexHealthCard: 3, TestCompanyResearchRoutes: 4)
**Root cause**: The companies blueprint was heavily modified (-205 lines). Need to investigate:

1. **Pagination tests** (6): The route may have changed how pagination params are passed or how the template renders page controls. Read `companies.py` index route and compare against test expectations.

2. **Health card tests** (3): New health metrics UI was likely added or changed. Check if template variables match.

3. **Company research routes** (4): These test the on-demand company research workflow (`company_research.py`). The route or response format may have changed.

**Investigation**: Read `test_companies.py` failing test classes, then read the corresponding blueprint routes in `companies.py`.

---

### P2-4: `test_ats_scanner.py` HTML Fallback Failures

**Failures**: 5 (TestRunAtsScanHtmlFallback: 2, TestHTMLJobsScoring: 2, TestHtmlFallbackDescriptionPassthrough: 1)
**Root cause**: The ATS scanner's HTML fallback flow was modified. Tests mock `find_careers_url` and `scrape_careers_page` at `job_finder.web.ats_scanner.*` — verify these are still imported/re-exported from ats_scanner. If the import path changed, update mock targets.

---

### P2-5: Remaining test_views.py, test_settings.py, test_parsers.py Failures

After fixing P0-1 through P0-3, re-run the test suite. Many of the remaining test_views.py failures should resolve (they cascade from the jobs page 500). The residual failures will be:

- **test_settings.py** (3): `test_settings_index_has_resume_quality_section`, `test_settings_migrate_shows_spinner_indicator`, `TestGuidelinesImport` — investigate if settings template changed structure
- **test_parsers.py** (4): `TestLinkedInMetaEmailFilter` — LinkedIn parser filtering logic changed
- **test_interview_prep.py** (2): Investigate dedup and content test expectations
- **test_rejection_analyzer.py** (2): Investigate batch analysis and on-demand trigger expectations
- **test_resume.py** (2): `TestSettingsResumeFormat` — settings template drive section rendering
- **test_job_visibility.py** (1): `test_auto_dismiss_excluded_job_sets_dismissed_status`

**Approach**: Fix P0 and P1 first, re-run, then investigate each residual failure individually. Many will likely resolve transitively.

---

## Phase 3: Production Code Correctness (No Test Failures, But Wrong Behavior)

### P3-1: `pipeline_runner.py` Double-Definition Shadowing

**Failures**: 0 (tests pass but wrong code runs)
**Root cause**: `pipeline_runner.py` imports functions from `ingestion_runner.py` at the top, but also defines local versions of the same functions further down. Python name resolution means the local definitions shadow the imports.

**Impact**:
- Local `_fetch_gmail()` lacks per-message dedup via `email_parse_log`
- Local `_score_and_persist()` lacks company upsert logic

**Fix**: Delete the local function definitions in `pipeline_runner.py`:
- `_fetch_gmail()` (local copy)
- `_fetch_serpapi()` (local copy)
- `_score_and_persist()` (local copy)
- `_log_to_email_parse_log()` (local copy)

The imported versions from `ingestion_runner` will then be used. Verify the re-export block at the top makes them available for test patch paths.

**Verification**: `uv run --active pytest tests/test_ingestion.py tests/test_scoring.py tests/test_scheduler.py -q --tb=short`

---

### P3-2: Scheduler References Missing Functions

**Root cause**: `scheduler.py:508` imports `run_scheduled_enrichment` and `scheduler.py:526` imports `run_registry_hygiene` from `backfill_companies` — but these don't exist (see P1-3). The scheduler would crash when these jobs fire.

**Fix**: Implement the functions per P1-3. After that, verify scheduler can import them:
```bash
uv run --active python -c "from job_finder.web.backfill_companies import run_scheduled_enrichment, run_registry_hygiene; print('OK')"
```

---

### P3-3: `gemini_provider.py` Top-Level Import Crash

**Failures**: 1 collection error (test_gemini_provider.py uncollectable)
**Runtime risk**: If Gemini routing fires, the import crashes the scoring pipeline

**Root cause**: `gemini_provider.py:16` does `import google.generativeai as genai` at module level, but `google-generativeai` is not in `requirements.txt` or the project venv.

**Fix — two options**:

**Option A** (if Gemini is actively used): Add to requirements.txt:
```
google-generativeai~=0.8.5
```
Then `uv pip install -r requirements.txt`.

**Option B** (if Gemini is optional/future): Guard the import:
```python
# gemini_provider.py top
try:
    import google.generativeai as genai
    from google.generativeai import types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

class GeminiProvider(BaseProvider):
    def __init__(self, *args, **kwargs):
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "google-generativeai is required for Gemini provider. "
                "Install with: pip install google-generativeai~=0.8.5"
            )
        super().__init__(*args, **kwargs)
```

Also guard the test file similarly:
```python
# test_gemini_provider.py top
pytest.importorskip("google.generativeai")
```

**Recommendation**: Option B. The provider is optional and the app should not hard-depend on it.

---

## Phase 4: SDK Migration Cleanup (Dead Code Removal)

### P4-1: Remove Vestigial `anthropic.Anthropic()` Instantiations

These 4 files create SDK client objects that are passed through the call chain but explicitly ignored by `call_claude()` (see `claude_client.py:501-502` docstring).

**Files and lines**:
1. `job_finder/web/company_research.py:151` — `client = anthropic.Anthropic()`
2. `job_finder/web/blueprints/dashboard.py:117` — `client = _anthropic.Anthropic()`
3. `job_finder/web/careers_crawler.py:598` — `_scoring_client = anthropic.Anthropic()`
4. `scoring_evaluator.py:850` — `client = anthropic.Anthropic()`

**Fix approach**: For each file:
1. Remove the `import anthropic` line
2. Change `client=client` to `client=None` in `call_model()` / `call_claude()` calls
3. Verify `call_model()` in `model_provider.py` handles `client=None` gracefully for the anthropic provider (check `_make_adapter()` at line 437 — it currently raises ValueError if client is None for anthropic provider)

**Important**: Before removing, check if `tier_has_configured_provider()` (model_provider.py) also requires the client for validation. If it does, refactor validation to check for API key in env instead of requiring a client object.

**Alternative approach**: If `tier_has_configured_provider()` depends on the client, create a lightweight shim:
```python
# In model_provider.py or claude_client.py
def get_anthropic_client():
    """Return a minimal Anthropic client for provider validation, or None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except ImportError:
        return None
```

**After all 4 files are cleaned up**: Check if `anthropic` can be removed from `requirements.txt`. Grep for any remaining `import anthropic` in production code.

---

### P4-2: Remove Dead `anthropic` Import in `ingestion_runner.py`

**File**: `job_finder/web/ingestion_runner.py:17-20`
```python
try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]
```

The variable `anthropic` is never used anywhere in this file. Delete these 4 lines.

---

### P4-3: Remove Dead Test Fixtures

**conftest.py**:
- Delete `mock_anthropic_client` fixture (lines 376-378) — returns None, used by 0 tests functionally
- Delete `app_config` fixture (lines 136-143) — 0 test usages

**Individual test files** — remove `mock_anthropic_client` parameter from test method signatures where accepted but never referenced:

1. `tests/test_data_enricher.py` — ~9 test methods accept but don't use it. Search for `mock_anthropic_client` in method signatures and remove the parameter.
2. `tests/test_description_reformatter.py` — 3 test methods (lines ~137, 157, 204)
3. `tests/test_scoring.py` — 2 test methods (lines ~263, 281)
4. `tests/test_resume_feedback.py` — has `mock_anthropic_client_prefs` fixture (line 92) that returns SDK-style mock. Check if any test actually calls `.messages.create` on it. If not, delete the fixture and remove from test signatures.
5. `tests/test_rejection_analyzer.py` — 2 locations (lines ~84, 311)

**Approach**: For each file, `grep -n mock_anthropic_client` to find all references, then delete unused fixture parameters and fixture definitions.

---

## Phase 5: HTMX / Frontend Fixes

### P5-1: HTTP 204 in Profile Save — HTMX Redirect Broken

**File**: `job_finder/web/blueprints/profile.py:147`
```python
response = current_app.response_class("", status=204)
```

**Fix**: Change `status=204` to `status=200`. HTMX doesn't process `HX-Redirect` headers on 204 responses per spec, and CLAUDE.md explicitly prohibits 204 for fragment responses.

---

### P5-2: Missing Quotes in `hx-include` Attribute Selector

**File**: `job_finder/web/blueprints/guidelines.py:168`
```python
f' hx-include="[name=merged_guide_json],[name=guidelines_text]"'
```

**Fix**: Add quotes around attribute values:
```python
f' hx-include="[name=\'merged_guide_json\'],[name=\'guidelines_text\']"'
```

---

## Phase 6: Repo Hygiene

### P6-1: Delete Empty `remove` File

```bash
rm remove
```

Zero-byte file at repo root. Git artifact from the merge.

---

### P6-2: Unused `BudgetExceededError` Import

**File**: `job_finder/web/scoring_runner.py` — line ~10
```python
from job_finder.web.claude_client import BudgetExceededError  # DELETE
```

Never referenced in any function body. Delete the import.

---

### P6-3: SONNET_SCHEMA_WITH_EVAL_BLOCKS Field Name Mismatch

**File**: `job_finder/web/sonnet_evaluator.py`
**Current fields**: `criterion`, `assessment`, `weight`
**Expected fields** (per test data and plan docs): `criterion`, `score`, `rationale`

**Fix**: Update the schema dict to use `score` (integer 1-10) and `rationale` (string) instead of `assessment` and `weight`. This is a schema-only change — the feature isn't wired into production scoring yet.

---

## Phase 7: Architecture Improvements (Optional, Non-Blocking)

These are quality improvements identified during the review. They don't fix bugs but improve maintainability. Execute only if time permits.

### P7-1: Split `ats_scanner.py` (992 lines)

Split into:
- `ats_company_registry.py` — `upsert_company()`, company probe status management
- `ats_scanners.py` — `scan_lever()`, `scan_greenhouse()`, `scan_ashby()`
- Keep `ats_scanner.py` as thin facade with re-exports for backward compatibility

### P7-2: Add Null Guards for Optional Imports in `ats_scanner.py`

Lines 36-58 use try/except for `scoring_orchestrator`, `careers_scraper`, `homepage_discoverer`. If import fails, references are `None` but callers don't check. Add:
```python
if score_and_persist_haiku is None:
    logger.warning("Skipping scoring: scoring_orchestrator not available")
    return
```

### P7-3: Fix Scheduler Time Collision

`careers_crawl` and `company_linkage` both at 5:00 AM. Move `company_linkage` to 4:45 AM so companies are linked before careers pages are crawled.

### P7-4: Skip Enrichment for Excluded Jobs

In `scoring_runner.py`, move the `should_exclude()` check BEFORE `enrich_job()` to avoid wasting API calls on jobs that will be excluded.

### P7-5: Document Liveness vs Expiry Checker Distinction

Add docstrings to both modules explaining:
- **liveness_checker**: Lightweight HTTP check, pattern-based, nightly 3:00 AM, no API costs
- **expiry_checker**: Multi-signal cascade (URL → ATS API → careers page → SerpAPI), nightly 2:30 AM, may incur SerpAPI costs

---

## Execution Order

```
Phase 0 (P0-1, P0-2, P0-3)     — 30-45 min — fixes runtime crashes
  ↓ re-run tests, expect ~90 failures resolved
Phase 1 (P1-1 through P1-5)    — 2-3 hrs — fixes ImportErrors
  ↓ re-run tests, expect ~50 more resolved
Phase 2 (P2-1 through P2-5)    — 1-2 hrs — fixes behavioral regressions
  ↓ re-run tests, expect remaining ~15 resolved
Phase 3 (P3-1, P3-2, P3-3)     — 30 min — fixes silent correctness issues
Phase 4 (P4-1, P4-2, P4-3)     — 45 min — removes dead SDK code
Phase 5 (P5-1, P5-2)           — 5 min — fixes HTMX anti-patterns
Phase 6 (P6-1, P6-2, P6-3)     — 5 min — repo hygiene
Phase 7 (optional)              — 2-3 hrs — architecture improvements
```

**Commit strategy**: One commit per phase. Suggested messages:
- P0: `fix: resolve runtime crashes — get_filtered_jobs signature, Jinja2 globals, template vars`
- P1: `fix: implement missing functions and fix import errors from merge`
- P2: `fix: resolve behavioral regressions in careers scraper, companies, ATS scanner`
- P3: `fix: pipeline_runner shadowed functions, scheduler imports, gemini import guard`
- P4: `refactor: remove dead anthropic SDK code from CLI oneshot migration`
- P5: `fix: HTMX anti-patterns — 204→200, hx-include attribute quoting`
- P6: `chore: repo hygiene — remove empty file, unused imports, schema alignment`

**Target**: 0 failures, 0 collection errors, all pages rendering correctly.

---

## Appendix A: Full Failure Inventory (154 failures + 1 collection error)

| Test File | Failures | Root Cause Phase |
|-----------|----------|-----------------|
| test_views.py | 36 | P0-1, P0-2, P0-3, P2-5 |
| test_enrichment_tiers.py | 34 | P1-1, P1-2 |
| test_backfill_companies.py | 18 | P1-3 |
| test_companies.py | 13 | P2-3 |
| e2e/test_jobs_page.py | 10 | P0-1 (cascade) |
| test_careers_scraper.py | 6 | P2-1 |
| test_backfill_enrichment.py | 6 | P1-4 |
| test_agentic_enricher.py | 5 | P1-1, P2-2 |
| test_ats_scanner.py | 5 | P2-4 |
| test_parsers.py | 4 | P2-5 |
| test_company_enricher.py | 4 | P1-5 |
| e2e/test_smoke.py | 3 | P0-1, P0-2 (cascade) |
| test_settings.py | 3 | P0-2, P2-5 |
| test_resume.py | 2 | P0-2 (cascade) |
| test_rejection_analyzer.py | 2 | P2-5 |
| test_interview_prep.py | 2 | P2-5 |
| test_job_visibility.py | 1 | P2-5 |
| test_gemini_provider.py | (collection error) | P3-3 |
| **TOTAL** | **154 + 1** | |

## Appendix B: Files Modified in Each Phase

### Phase 0
- `job_finder/db.py` (add 6 params + SQL)
- `job_finder/web/__init__.py` (add 1 import + 1 global)
- `job_finder/web/blueprints/batch_scoring.py` (add 1 template var)

### Phase 1
- `job_finder/web/enrichment_tiers.py` (add `is_short_auth_page`, optionally `search_ddg_web`, `fetch_ddg_jds`)
- `job_finder/web/backfill_companies.py` (add 3 functions)
- `job_finder/web/backfill_enrichment.py` (fix ScoringResult unwrapping)
- `tests/test_company_enricher.py` (fix mock targets)

### Phase 2
- `job_finder/web/careers_scraper.py` (add subdomain/redirect detection)
- Various test files (after re-run, investigate residual failures)

### Phase 3
- `job_finder/web/pipeline_runner.py` (delete ~200 lines of shadowed functions)
- `job_finder/web/providers/gemini_provider.py` (guard import)
- `tests/test_gemini_provider.py` (add importorskip)

### Phase 4
- `job_finder/web/company_research.py` (remove anthropic import)
- `job_finder/web/blueprints/dashboard.py` (remove anthropic import)
- `job_finder/web/careers_crawler.py` (remove anthropic import)
- `scoring_evaluator.py` (remove anthropic import)
- `job_finder/web/ingestion_runner.py` (remove dead import)
- `tests/conftest.py` (remove 2 fixtures)
- `tests/test_data_enricher.py`, `tests/test_description_reformatter.py`, `tests/test_scoring.py`, `tests/test_resume_feedback.py`, `tests/test_rejection_analyzer.py` (remove dead params)

### Phase 5
- `job_finder/web/blueprints/profile.py` (204→200)
- `job_finder/web/blueprints/guidelines.py` (attribute quoting)

### Phase 6
- `remove` (delete file)
- `job_finder/web/scoring_runner.py` (remove unused import)
- `job_finder/web/sonnet_evaluator.py` (fix schema fields)
