# Test Audit Remediation Plan

> 3 chunks for plan-build-review workflow.
> No inter-chunk dependencies — execute in any order.
> Grouped by nature of change: fix existing, delete/restructure, add new.

---

## Chunk A: Fix All Broken, Wrong, and Weak Tests

**Scope**: Every test that exists but provides wrong/zero/weak signal.
**Files touched** (18): `test_notifier.py`, `test_agentic_enricher.py`, `test_data_enricher.py`, `test_views.py`, `test_eval_provider.py`, `test_model_provider.py`, `test_scoring_runner.py`, `test_detections_blueprint.py`, `test_costs.py`, `test_backfill_enrichment.py`, `test_resume_feedback.py`, `test_rejection_analyzer.py`, `test_parsers.py`, `test_profile.py`, `test_resume_validator.py`, `test_pipeline.py`, `test_description_reformatter.py`, `test_expiry_checker.py`
**Verification**: `uv run pytest tests/ -v`

### Task

Fix 2 zero-signal tests (always pass), 11 HIGH-severity broken/misleading tests, and 14 MEDIUM-severity tautological or weak assertions across the test suite. All changes are modifications to existing tests — no deletions, no new files.

### Implementation Plan

Each fix below is tagged with its severity and the exact location. The coder should read each test, understand what it claims to verify, then fix the assertion to actually verify that claim.

---

### CRITICAL — Zero-Signal Tests (always pass)

#### A1. `tests/test_notifier.py:39` — `test_does_not_block_caller`

**Problem**: Patches `send_notification` itself (`patch("job_finder.web.notifier.send_notification")`), then calls the mock. Measures MagicMock return time. Proves nothing about real non-blocking behavior.

**Fix**: Patch `threading.Thread` instead (not the function under test). Call the real `send_notification`, measure wall-clock time, verify thread was started but not joined:

```python
def test_does_not_block_caller(self):
    """send_notification returns immediately without waiting for thread."""
    import time
    from job_finder.web.notifier import send_notification

    with patch("threading.Thread") as mock_thread:
        t_instance = MagicMock()
        mock_thread.return_value = t_instance
        start = time.time()
        send_notification("Title", "Body")
        elapsed = time.time() - start
        assert elapsed < 1.0, f"send_notification blocked for {elapsed:.2f}s"
        t_instance.start.assert_called_once()
```

#### A2. `tests/test_agentic_enricher.py:560` — `test_company_bypass_for_long_pages_with_short_names`

**Problem**: Calls `enrich_single_job()`, stores result, then function body ends with comments but zero `assert` statements.

**Fix**: Read `job_finder/web/agentic_enricher.py` to determine what `enrich_single_job` returns for a 2-char company name ("Zo") with 0 meaningful tokens. The comments at lines 584-586 say "The job should be skipped since no meaningful tokens exist." Add assertion matching the actual skip behavior — either `assert result is None` or `assert result == ""` or whatever the skip return value is. If the function actually proceeds (bypass means "skip the check, not skip the job"), assert the result contains enriched text.

---

### HIGH — Broken or Misleading Tests

#### A3. `tests/test_notifier.py:88` — `test_no_url_omits_on_click`

**Problem**: Creates `fake_toast` and `captured_kwargs` but never executes the thread target. `captured_kwargs` stays empty. Only asserts `daemon=True` (already tested elsewhere).

**Fix**: Follow the pattern from `test_passes_url_as_on_click` (line 61-86 in same file) — capture the thread target, execute it with `win11toast.toast` mocked, assert `on_click` is NOT in kwargs:

```python
def test_no_url_omits_on_click(self):
    """send_notification without url does not pass on_click to toast."""
    import sys
    from job_finder.web.notifier import send_notification

    with patch("threading.Thread") as mock_thread:
        t_instance = MagicMock()
        mock_thread.return_value = t_instance
        send_notification("Title", "Body")  # no url
        target_fn = mock_thread.call_args.kwargs["target"]

    mock_toast = MagicMock()
    fake_win11toast = MagicMock()
    fake_win11toast.toast = mock_toast
    with patch.dict(sys.modules, {"win11toast": fake_win11toast}):
        target_fn()

    mock_toast.assert_called_once()
    _, toast_kwargs = mock_toast.call_args
    assert "on_click" not in toast_kwargs, "on_click must not be passed when url is None"
```

#### A4. `tests/test_notifier.py:145` — `test_no_exception_on_toast_error`

**Problem**: Uses `builtins.__import__` mock raising `RuntimeError`, but real failure is `ImportError` from `from win11toast import toast`. The `__import__` approach is unreliable for `from X import Y`.

**Fix**: Use `sys.modules` patching to inject a mock module whose `toast` raises:

```python
def test_no_exception_on_toast_error(self):
    """send_notification silently swallows any exception from toast."""
    import sys
    from job_finder.web.notifier import send_notification

    with patch("threading.Thread") as mock_thread:
        captured_target = []
        def capture_thread_call(*args, **kwargs):
            captured_target.append(kwargs.get("target"))
            m = MagicMock()
            return m
        mock_thread.side_effect = capture_thread_call
        send_notification("Title", "Body")

    failing_module = MagicMock()
    failing_module.toast.side_effect = RuntimeError("toast crash!")
    with patch.dict(sys.modules, {"win11toast": failing_module}):
        assert captured_target and captured_target[0]
        try:
            captured_target[0]()
        except Exception as e:
            raise AssertionError(f"Thread target must swallow exceptions, got: {e}")
```

#### A5. `tests/test_views.py:1886` — `test_expand_no_load_trigger`

**Problem**: Fetches route, decodes data, then function ends. No assertion about the absence of `hx-trigger=load`.

**Fix**: Add the assertion:
```python
data = response.data.decode()
assert 'hx-trigger="load"' not in data, "Regular expand must not include hx-trigger=load"
```

#### A6. `tests/test_views.py:1443` — `test_profile_degrades_gracefully_when_preferences_query_raises`

**Problem**: `FailingConn` delegates non-preferences queries to a new `:memory:` DB with no schema/data. All non-preferences queries also fail, masking the real degradation path.

**Fix**: `FailingConn` must delegate to the real app DB for non-preferences queries. Get the DB path from `app.config["DB_PATH"]`:

```python
class PreferencesFailingConn:
    def __init__(self, real_db_path):
        self._real = sqlite3.connect(real_db_path)
        self._real.row_factory = sqlite3.Row

    def execute(self, sql, *args, **kwargs):
        if "resume_preferences_detected" in sql:
            raise sqlite3.OperationalError("no such table")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)
```

#### A7. `tests/test_eval_provider.py` ~line 772 — `test_unknown_variant_falls_back_to_default`

**Problem**: "default" variant returns `_BASE_SYSTEM_PROMPT` (plain) but unknown variant falls back to `_SYSTEM_PROMPT` (fewshot). Either a bug locked in by a test, or intentional asymmetry with a misleading name.

**Fix**:
1. Read the `reconstruct_prompt` (or equivalent) in `eval_provider.py` to trace the logic
2. If intentional: rename to `test_unknown_variant_falls_back_to_fewshot_prompt` and add a comment explaining why
3. If a bug: fix the implementation so unknown falls back to `_BASE_SYSTEM_PROMPT` like "default" does, then update the test assertion

#### A8. `tests/test_model_provider.py` lines 87-137 — `resolve_provider_config` full-dict equality

**Problem**: 5+ tests assert `result == {entire dict with every key}`. Adding any new field breaks all simultaneously.

**Fix**: For each test, assert only the fields the test logically cares about (per its name/docstring). Keep ONE test as the comprehensive shape test that validates all keys exist:

```python
# Shape test (one only):
def test_resolve_provider_config_returns_all_expected_keys(self):
    result = resolve_provider_config(...)
    expected_keys = {"provider", "model", "prompt_variant", "fallback", "fallback_chain", "daily_limits", "throttle_delays"}
    assert set(result.keys()) == expected_keys

# Specific tests (assert only what they test):
def test_anthropic_default_config(self):
    result = resolve_provider_config(...)
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-4-6"
```

Also parametrize the 6 nearly-identical `test_call_model_skips_budget_for_*` tests (lines 316-408):
```python
@pytest.mark.parametrize("provider_name", ["gemini", "ollama", "ollm", "openrouter", "sambanova"])
def test_call_model_skips_budget_for_free_provider(self, provider_name, ...):
```

#### A9. `tests/test_data_enricher.py` ~line 817 — `test_sonnet_receives_all_fragments`

**Problem**: `"DDG" in str(fragments)` matches dict key names like `ddg_snippet`, not actual DDG content. Passes even with empty DDG text.

**Fix**: Assert on the actual content value passed to Sonnet, not the stringified dict. Read the test to find the DDG text value from the mock setup, then:
```python
call_args = mock_sonnet.call_args
fragments = ...  # extract fragments argument
assert "expected DDG text content" in str(fragments.values()), "DDG content must reach Sonnet"
```

#### A10. `tests/test_scoring_runner.py:132` — `patch.object(sr, "enrich_job", None)`

**Problem**: Patching to `None` instead of a callable. Code path change would cause confusing `TypeError: 'NoneType' is not callable`.

**Fix**: `patch.object(sr, "enrich_job", MagicMock())`. Same for `enrich_company_info` at line 223.

#### A11. `tests/test_expiry_checker.py:184` — hardcoded `"inconclusive"` string

**Problem**: Uses string literal instead of module constant. If constant value changes, mock returns wrong value.

**Fix**:
```python
from job_finder.web.expiry_checker import INCONCLUSIVE
mock_ats.return_value = INCONCLUSIVE
```

---

### MEDIUM — Tautological or Weak Assertions

For each fix below: read the test, read the template/implementation it tests, replace the loose assertion with a precise one.

#### A12. `tests/test_notifier.py:353` — `test_body_distinguishes_80_and_100_percent`

**Problem**: `"100" in bodies[100.0]` always true since it's the percentage.
**Fix**:
```python
assert "80" in bodies[80.0]
assert "100" in bodies[100.0]
assert bodies[80.0] != bodies[100.0], "80% and 100% bodies must differ"
```

#### A13. `tests/test_views.py:995` — `test_single_source_job_does_not_show_source_count_badge`

**Problem**: `"sources" not in data or "greenhouse" in data` — "greenhouse" always appears.
**Fix**: Remove the tautological second assertion. Keep only: `assert "1 sources" not in data` and add `assert "1 source" not in data`.

#### A14. `tests/test_views.py:1012` — `test_multi_source_job_shows_enrichment_indicator`

**Problem**: `"sources" in data` always true on jobs page, making OR chain vacuous.
**Fix**: Remove the `"sources" in data` fallback:
```python
assert "&#10024;" in data or "sparkle" in data.lower(), "Enrichment sparkle must appear"
```
If neither pattern exists in the actual template, read the template and assert on the real enrichment indicator markup.

#### A15. `tests/test_detections_blueprint.py:267` — `test_dashboard_shows_correct_pending_count`

**Problem**: `"1" in body` matches any "1" in full HTML.
**Fix**: Read the dashboard template to find the pending count element, assert on a specific pattern like `">1</span>"` or `"1 pending"`.

#### A16. `tests/test_costs.py:218` — `test_costs_html_contains_budget_progress_bar`

**Problem**: `"budget" in html.lower()` matches nav/headings.
**Fix**: Read the costs template, find the progress bar element, assert on its specific class or tag (e.g., `"progress"` element, `role="progressbar"`, or a specific CSS class).

#### A17. `tests/test_backfill_enrichment.py:149` — `test_convergence_multiple_passes`

**Problem**: `total_enriched > 5` but expected ~30.
**Fix**: `assert total_enriched >= 25, f"Expected ~30 enrichments (5 jobs * ~6 tiers), got {total_enriched}"`

#### A18. `tests/test_backfill_enrichment.py:213` — `test_cost_estimate_counts_tiers`

**Problem**: `"null" in captured.out.lower()` always true.
**Fix**: Read the implementation's output format and assert the specific tier count text.

#### A19. `tests/test_resume_feedback.py:768` — `test_consolidation_skips_when_budget_exceeded`

**Problem**: OR assertion too permissive.
**Fix**: `assert result.get("consolidated") is False`

#### A20. `tests/test_rejection_analyzer.py:404` — `test_route_flashes_no_unreviewed_message`

**Problem**: `"0" in m` matches any flash containing "0".
**Fix**: `assert any("no unreviewed" in m.lower() for m in messages)`

#### A21. `tests/test_rejection_analyzer.py:429` — `test_route_flashes_success_with_count`

**Problem**: `"1" in m` matches any message containing "1".
**Fix**: `assert any("analyzed" in m.lower() for m in messages)`

#### A22. `tests/test_parsers.py:579` — `test_parses_indeed_alert_jobs`

**Problem**: `len(jobs) >= 2` when fixture has 3 jobs.
**Fix**: `assert len(jobs) == 3`

#### A23. `tests/test_profile.py:548,575` — `test_post_profile_save_redirects_on_success`

**Problem**: Accepts status `200, 302, 204` — any behavior passes.
**Fix**: Read the route to determine the correct status code, assert only that one.

#### A24. `tests/test_resume_validator.py:370` — `test_fix_only_sends_error_violations`

**Problem**: Checks errors present but never verifies warnings absent.
**Fix**: Add `assert "em dash" not in user_content.lower(), "Warnings must NOT be sent to fix pass"`.

#### A25. `tests/test_resume_validator.py:648` — `test_validator_failure_does_not_block_generation`

**Problem**: `if row[1] is not None:` skips the assertion.
**Fix**: Make unconditional: `assert row[1] is not None, "validation_report must be stored"`

#### A26. `tests/test_pipeline.py:128` — `test_pipeline_shows_rejected_collapsed`

**Problem**: `b"hidden" in response.data` matches any "hidden" CSS class.
**Fix**: Read the pipeline template and assert on the specific rejected section element + hidden class together.

#### A27. `tests/test_description_reformatter.py:255,392` — cost recording assertions

**Problem**: Claim to test cost recording but only check `mock_call.call_count`. Never verify DB rows.
**Fix**: If `call_model` is mocked (preventing real cost recording), rename the tests to reflect what they actually test (e.g., `test_reformat_calls_model_per_job`) and update the docstrings. If cost recording is NOT mocked, add a DB assertion: `conn.execute("SELECT COUNT(*) FROM scoring_costs").fetchone()[0] >= 1`.

---

## Chunk B: Delete Duplicates & Structural Cleanup

**Scope**: Remove redundant tests, fix stale names, remove dead code, fix infrastructure issues.
**Files touched** (10): `test_resume.py`, `test_data_enricher.py`, `test_batch_scoring.py`, `test_resume_style_guide.py`, `test_migration.py`, `test_scheduler.py`, `test_logging.py`, `test_dedup_normalizer.py`, `test_ingestion.py`, `test_parsers.py`
**Verification**: `uv run pytest tests/ -v`

### Task

Remove ~180 duplicate tests, fix stale test names, remove dead code, and fix structural test infrastructure issues. All changes are deletions, renames, or mechanical restructuring — no behavioral changes.

### Implementation Plan

---

### Delete Duplicate Tests

#### B1. Remove 4 duplicate classes from `tests/test_resume.py`

These classes are fully duplicated in dedicated files that have MORE thorough coverage:

| Class in test_resume.py | Duplicate of | Lines (approx) |
|---|---|---|
| `TestDocxFormatter` | `test_docx_formatter.py` | ~21-115 |
| `TestDriveUpload` | `test_drive_uploader.py` | ~117-217 |
| `TestDriveServiceScopeCheck` | `test_drive_uploader.py` | ~219-371 |
| `TestDriveStatus` | `test_drive_status.py` | ~379-543 |

**Procedure**: Read `test_resume.py`, identify exact class boundaries, delete the 4 classes and any imports only used by them. Run `uv run pytest tests/test_resume.py tests/test_docx_formatter.py tests/test_drive_uploader.py tests/test_drive_status.py -v` to confirm the dedicated files still pass.

#### B2. Remove 2 duplicate classes from `tests/test_data_enricher.py`

| Class in test_data_enricher.py | Duplicate of |
|---|---|
| `TestSearchSerpapi` (~lines 165-241) | `test_enrichment_tiers.py` |
| `TestEnrichCompanyInfo` (~lines 392-449) | `test_company_enricher.py` |

#### B3. Remove dead meta-test from `tests/test_batch_scoring.py`

Delete `TestDeadCodeRemoved.test_update_session_counter_removed` (~line 273). This asserts that a removed function doesn't exist — a migration guard that served its purpose and is now a permanent no-op.

#### B4. Remove redundant test from `tests/test_resume_style_guide.py`

Delete `test_load_style_guide_returns_dict` (~line 28). Fully redundant with `test_save_load_roundtrip` in the same file.

---

### Fix Stale Names & Misplaced Tests

#### B5. Rename stale migration test names in `tests/test_migration.py`

- `test_migration_count_is_thirteen` (line 405) → `test_migration_count_is_24`. Update docstring and assertion message.
- `test_migrations_count_is_19` (line 1124) → `test_migration_count_matches_current`. Update docstring.

#### B6. Move misplaced Migration 12 tests in `tests/test_migration.py`

`TestMigration14` (line 518) contains 3 tests that belong to Migration 12:
- `test_migration12_adds_retry_after_to_companies`
- `test_migration12_adds_miss_reason_to_companies`
- `test_migration12_retry_count_defaults_to_zero`

Move them to `TestMigration12` class (create it if needed, using the same `migrated_db_class` fixture pattern).

---

### Remove Dead Code & Fix Infrastructure

#### B7. Remove dead `_make_app` helper from `tests/test_scheduler.py` (line 23)

Defined but never called by any test. Delete the function.

#### B8. Fix `add_job.call_args` in `tests/test_scheduler.py` (line 211)

**Problem**: Gets the LAST `add_job` call, which may not be the ingestion job.
**Fix**: Use `call_args_list` and find the ingestion call specifically:
```python
ingestion_call = next(
    c for c in mock_sched.add_job.call_args_list
    if "run_ingestion" in str(c)
)
assert ingestion_call.kwargs.get("replace_existing") is True
```

#### B9. Replace `os.chdir()` in `tests/test_logging.py` (lines 25, 44)

**Problem**: `os.chdir()` changes process-wide CWD. If test fails before `finally`, all subsequent tests run in wrong directory.

**Fix**: Convert from `unittest.TestCase` to plain pytest class and use `monkeypatch.chdir(tmp_path)`:
```python
class TestFileLogging:
    def test_setup_file_logging_attaches_handler(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # ... rest of test, no try/finally needed
```

Also clean up the root logger handler mutations to use `monkeypatch` or `addCleanup`.

#### B10. Fix FK assertion in `tests/test_dedup_normalizer.py` (line 402)

**Problem**: Asserts `events[0]["job_id"] != "old-key-2"` but never verifies the correct new value.
**Fix**: Add positive assertion. Read the test setup to find what the canonical key should be, then:
```python
assert events[0]["job_id"] == expected_canonical_key
```

#### B11. Fix unused fixtures in `tests/test_ingestion.py` (lines 458, 504)

**Problem**: Two tests receive `migrated_db_path` fixture but create own temp DB via `__import__("tempfile")`.
**Fix**: Use the `migrated_db_path` fixture directly, remove the `__import__("tempfile")` / `__import__("os")` calls.

#### B12. Fix conditional assertion in `tests/test_ingestion.py` (line 430)

**Problem**: `if result:` silently skips assertions when parser returns empty list.
**Fix**: `assert len(result) >= 1, "Parser must extract at least one job"`

#### B13. Convert silent-skip tests in `tests/test_parsers.py` (~lines 222, 1135)

**Problem**: `if os.path.exists(email_path):` silently passes without data files.
**Fix**: Convert to `pytest.mark.skipif` so skips are visible in test output:
```python
@pytest.mark.skipif(not os.path.exists(ARCHIVE_PATH), reason="Archived email fixture not present")
def test_real_archived_email(self):
```

---

## Chunk C: Add Missing Coverage

**Scope**: Net-new tests for systematic coverage gaps identified in the audit.
**Files touched** (6): `test_scoring.py`, `test_db_helpers.py`, `test_ingestion.py`, `test_interview_prep.py`, `test_rejection_analyzer.py`, `test_log_levels.py`
**Verification**: `uv run pytest tests/test_scoring.py tests/test_db_helpers.py tests/test_ingestion.py tests/test_interview_prep.py tests/test_rejection_analyzer.py tests/test_log_levels.py -v -k "malformed or type_mismatch or batch_error or budget_zero or caplog"`

### Task

Add targeted tests for 4 systematic coverage gaps: malformed AI responses, `safe_json_load` type mismatch, budget gate zero boundary, and batch error continuation. Also add runtime (`caplog`) companion tests for 7 source-inspection-only log-level tests.

### Implementation Plan

---

### 1. Malformed AI Response Tests

No test anywhere verifies behavior when AI models return JSON missing expected keys. Add 1-2 tests per critical module.

#### C1. `tests/test_scoring.py` — add to `TestHaikuScorer`

```python
def test_haiku_malformed_response_returns_none(self, ...):
    """Haiku returning JSON without 'score' key does not crash."""
```
- Read `haiku_scorer.py` to find what keys it accesses from the response
- Mock the Claude client to return `{"summary": "good"}` (missing `score`)
- Assert the function returns `None` or a default, not an unhandled `KeyError`

#### C2. `tests/test_scoring.py` — add to Sonnet section

```python
def test_sonnet_malformed_response_returns_none(self, ...):
    """Sonnet returning JSON without expected keys does not crash."""
```
- Same approach: mock response with missing keys, assert graceful handling

#### C3. `tests/test_interview_prep.py`

```python
def test_generate_handles_malformed_opus_response(self, ...):
    """Opus returning unexpected schema does not crash interview prep."""
```
- Mock Opus to return `{"random": "data"}` instead of expected structure
- Assert function returns gracefully

#### C4. `tests/test_rejection_analyzer.py`

```python
def test_analyze_handles_malformed_opus_response(self, ...):
    """Opus returning JSON without 'patterns' key does not crash."""
```

For each test: read the module's response parsing code to identify what keys are accessed, mock to return a response missing those keys, assert graceful handling.

---

### 2. `safe_json_load` Type Mismatch

#### C5. `tests/test_db_helpers.py`

```python
def test_valid_json_scalar_returns_scalar_not_default(self):
    """safe_json_load with valid JSON string literal returns the string, not default.
    
    Documents that callers passing default=[] could get a string back
    if the stored JSON is a valid scalar.
    """
    result = safe_json_load('"just a string"', default=[])
    assert result == "just a string"
    assert not isinstance(result, list)
```

---

### 3. Budget Gate Zero Boundary

#### C6. `tests/test_scoring.py` — add to `TestCostGate`

Read `cost_gate` implementation first to determine if 0.0 means "zero budget" or "unlimited."

```python
def test_cost_gate_zero_budget_with_zero_spend(self, ...):
    """cost_gate with budget=0.0 and zero spend — verify boundary behavior."""
    # Insert 0 cost rows, call cost_gate with budget=0.0
    # Assert based on implementation's boundary semantics

def test_cost_gate_zero_budget_with_positive_spend(self, ...):
    """cost_gate with budget=0.0 and actual spend returns False."""
    # Insert a small cost row, call cost_gate with budget=0.0
    # Assert False (over budget)
```

---

### 4. Batch Error Continuation

#### C7. `tests/test_ingestion.py`

```python
def test_run_ingestion_continues_after_single_source_failure(self, ...):
    """If one source raises during ingestion, other sources still run."""
```
- Read `pipeline_runner.run_ingestion` to verify it has try/except per source
- Mock gmail to raise, mock thordata/serpapi to return jobs
- Assert the non-failing sources' results were processed
- If the implementation doesn't have per-source error isolation, this test correctly fails — revealing a real gap

---

### 5. Log-Level Runtime Companions (caplog tests)

7 tests in `tests/test_log_levels.py` use only `inspect.getsource()` + substring matching with no runtime verification. Add a `caplog`-based companion for each, following the pattern already established in that file (see existing caplog tests at lines 72-121, 196-237, 296-335).

#### C8-C14. Tests to add:

| Source-inspection test | Companion to add |
|---|---|
| `test_zero_job_email_routed_to_activity_feed_logs_at_debug` (line 123) | Trigger the zero-job email path with mocks, assert `caplog` has DEBUG record |
| `test_haiku_no_result_logs_at_debug` (line 148) | Mock Haiku to return None, assert DEBUG log |
| `test_cost_gate_false_logs_at_info` (line 247) | Set up exceeded budget, assert INFO log |
| `test_budget_exceeded_error_logs_at_info` (line 273) | Raise BudgetExceededError, assert INFO log |
| `test_blocked_wipe_logs_at_debug` (line 416) | Trigger blocked wipe path, assert DEBUG log |
| `test_paste_jd_budget_cap_logs_at_info` (line 443) | Trigger paste JD budget cap, assert INFO log |
| `test_rescore_budget_cap_logs_at_info` (line 462) | Trigger rescore budget cap, assert INFO log |

For each:
1. Read the source-inspection test to identify the target code path
2. Read the target module to understand what setup triggers that log call
3. Write a companion test that exercises the real code path with appropriate mocks
4. Use `caplog.at_level(logging.DEBUG)` and assert on both message content and log level

Note: Some may require significant fixture setup (Flask app, mock DB). Use existing fixtures from `conftest.py`. If a particular test requires excessive setup for the value it provides, document why in a comment and keep only the source-inspection version — but this should be the exception, not the rule.
