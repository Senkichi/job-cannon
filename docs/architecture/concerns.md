# Codebase Concerns

This document catalogs known tech debt, fragile areas, scaling limits, and test-coverage gaps in `job_finder/` for engineers reading the source. Items marked `Resolution:` were closed out before this audit was migrated. For setup and run instructions, see [docs/SETUP.md](../SETUP.md).

## Tech Debt

**Bare Exception Handlers (Low Priority but Present):**
- Issue: Multiple modules catch `except Exception:` without specific error classification, silently swallowing exceptions and returning empty defaults
- Files: `job_finder/db/_queries.py` (`get_distinct_sources` JSON-decode try/except), `job_finder/main.py` (line 57), `job_finder/web/activity_tracker.py` (fault-tolerant by design). Note: post-S7d the original `job_finder/db.py` is now a package; the bare-except sites moved with the functions.
- Impact: Failures are logged at DEBUG level only and never propagate. In single-user local app this is acceptable for non-critical operations like activity logging (which is designed to be fault-tolerant), but makes debugging harder when legitimate errors occur
- Fix approach: Keep activity_tracker's design (intentionally swallows all exceptions at DEBUG level per design). For DB-package query failures, consider logging at WARNING level for visibility while maintaining graceful fallback returns

**Indeed Email Parser Plain-Text Mismatch (BLOCKING):**
- Issue: `job_finder/parsers/indeed_parser.py` expects HTML structure (BeautifulSoup parsing, looking for `<a>` tags), but `job_finder/sources/gmail_source.py` delivers PLAIN TEXT bodies (prefers text/plain over text/html)
- Files: `job_finder/sources/gmail_source.py` (lines 176-214), `job_finder/parsers/indeed_parser.py` (lines 69-102, 112, 134)
- Impact: ALL 60+ Indeed emails in archive parse to 0 jobs. Parser logs warning "Indeed parser: no jobs found -- HTML structure may have changed" but fails silently with empty results
- Evidence: Real Indeed emails examined (2026-03-13) are pure plain-text with line-delimited job blocks (title, company-location, salary, description, URL). Parser requires HTML tags that don't exist
- Fix approach: Replace BeautifulSoup HTML parsing with plain-text line-based parser. Pattern already exists in `job_finder/parsers/linkedin_parser.py` and partially in `indeed_parser.py._parse_plaintext()` (lines 123-152). The `_parse_plaintext()` function exists but is unused — refactor to make it the primary strategy and HTML fallback
- **Resolution:** Fixed in Phase 14 (v1.1, 2026-03-13). Parser rewritten to use plain-text parsing as primary strategy with HTML fallback. See `.planning/milestones/v1.1-phases/14-ats-retry-indeed-parser/14-03-SUMMARY.md`.

**JSON Field Deserialization Fragility (Medium Priority):**
- Issue: Multiple modules deserialize JSON from SQLite `TEXT` columns without try/except validation
- Files (legacy locations): `job_finder/web/haiku_scorer.py` (deleted in v3.0; logic now in `job_finder/web/job_scorer.py`), `job_finder/web/ats_scanner/` (multiple locations), `job_finder/db/_jobs.py` (`upsert_job` JSON merge logic)
- Impact: If a JSON field is corrupted or NULL, the code may crash with `JSONDecodeError` or `TypeError`. Currently mitigated by defensive checks but no universal pattern
- Fix approach: Create utility function `safe_json_load(field, default={})` in `job_finder/web/db_helpers.py` and use throughout codebase. Already done partially in the legacy `haiku_scorer._build_comp_context()` — extend pattern
- **Resolution:** Fixed in Phase 34 (v1.5, 2026-03-17). `safe_json_load()` utility created in `job_finder/web/db_helpers.py` and adopted at all JSON deserialization call sites. See `.planning/phases/34-data-quality/34-01-SUMMARY.md`.

**APScheduler Version Lock Risk (Low Priority):**
- Issue: `pyproject.toml` pins APScheduler to `>=3.11,<4.0` to avoid breaking async API changes
- Files: `pyproject.toml`, `job_finder/web/scheduler.py`
- Impact: Version 4.x has incompatible async patterns. Pinning to 3.11 prevents security updates if any are released in the 3.x series
- Fix approach: Review APScheduler 4.x API periodically and plan migration when time permits. Not urgent for single-user local app.

**Database Migration Pattern Relies on Individual Statement Execution (Low Priority):**
- Issue: Migrations use list of discrete SQL statements rather than semicolon-delimited script. While intentional (per comment "avoids semicolon-splitting hazards"), it creates maintenance friction
- Files: `job_finder/web/db_migrate.py` (entire file)
- Impact: Adding a new migration requires writing as a list of SQL strings. Accidental duplicate column errors fail per-statement but are not automatically caught
- Fix approach: Pattern is working as designed. Acceptable for this codebase scale. Could be improved with try/except around each ALTER TABLE, but not critical

## Known Bugs

**ATS Probe/Scan Workflow Separation (DESIGN GAP - Resolved at Design Level):**
- Symptoms: User adds company → clicks "Scan ATS" → status still shows "pending", 0 jobs found
- Files: `job_finder/web/blueprints/companies.py` (line 278-291), `job_finder/web/ats_scanner.py` (lines 715-719 query), `job_finder/web/scheduler.py` (lines 215-238)
- Trigger: run_ats_scan() only queries companies WHERE `ats_probe_status='hit'`, but newly-added companies start at `ats_probe_status='pending'`. probe_ats_slugs() (which transitions pending→hit/miss) is only called by scheduler on Mon/Wed 7:30 AM, not on manual "Scan ATS" button
- Workaround: Manual scan button only works for companies already probed. User must wait for scheduled Mon/Wed probe, or code needs to chain probe+scan in the /companies/scan route
- Status: Documented in `.planning/debug/ats-scan-pending-after-complete.md` as resolved-by-documentation. Root cause confirmed March 12, 2026
- **Resolution:** Resolved by documentation (2026-03-12). Root cause confirmed and documented. Probe-then-scan is by-design behavior; manual scan only works for already-probed companies.

## Security Considerations

**API Key Configuration Pattern (Acceptable Risk for Single-User Local App):**
- Risk: API keys (Claude, SerpAPI, Gmail OAuth) stored in `config.yaml` (not in version control, but on disk plaintext)
- Files: `config.yaml` (user data file, `.gitignore`d), `job_finder/config.py` (loader)
- Current mitigation: File is `.gitignore`d and user is responsible for manual backup (`bash backup_userdata.sh`). No transmission over network (localhost:5000 only)
- Recommendations: For this single-user local app, acceptable. If ever deployed: use environment variables + secrets manager. Current design matches project scope ("Single-user, local-only app")

**SQL Injection Guard for Dedup Key Normalization (Well-Protected):**
- Risk: `job_finder/web/dedup_normalizer.py` uses dynamically-built SQLite queries on FK tables during retroactive dedup
- Files: `job_finder/web/dedup_normalizer.py` (`ALLOWED_FK_TABLES` allowlist near the top of the module)
- Current mitigation: Explicit allowlist of FK tables (`pipeline_events`, `pipeline_detections`, `scoring_costs`) prevents arbitrary table injection. Column names not interpolated (only table names checked against the allowlist)
- Assessment: SAFE — Pattern is correct and well-documented

**Anthropic API Request Validation (Good Practice):**
- Risk: Structured output schema validation relies on Anthropic's `json_schema` validation. Malformed schema could cause parsing failures
- Files: `job_finder/web/job_scorer.py` (post-v3.0 single-tier; `JOB_ASSESSMENT_SCHEMA`), per-provider clients in `job_finder/web/providers/`. Pre-v3.0 location was `haiku_scorer.py` + `sonnet_evaluator.py` (deleted).
- Current mitigation: All schemas follow same pattern (required fields, additionalProperties=False). Each provider validates server-side or via the local `jsonschema` validator in `model_provider.py`. Schema-validation failure on a provider falls through to the next link in the cascade rather than failing the whole call.
- Assessment: ACCEPTABLE — multi-provider cascade with per-link schema validation gives defense-in-depth.

## Performance Bottlenecks

**Large File Description Merging (Low Priority - Only on Duplicates):**
- Problem: When a job appears multiple times (from different sources), `job_finder/db/_jobs.py::merge_description()` appends new description to existing with "\n\n---\n\n" separator
- Files: `job_finder/db/_jobs.py` (`merge_description` function)
- Cause: Substring comparison on every update, string concatenation accumulates over multiple upserts
- Impact: For jobs seen 5+ times from different sources, description field grows large. No hard limit, but TEXT field in SQLite has practical limits
- Improvement path: Cap merged description at 50KB, truncate oldest entries if exceeded. Or: store descriptions separately (description_v1, description_v2) and render selected version. Current approach works for typical job lifetime (not expected to see same job >10 times)
- Priority: Low — affects small percentage of jobs

**Batch AI Scoring Without Parallel Processing (Design Trade-off):**
- Problem: scoring runs jobs sequentially through the cascade (one `score_job()` call per new job)
- Files: `job_finder/web/pipeline_runner.py`, `job_finder/web/scoring_orchestrator.py`
- Cause: Sequential design is intentional — keeps budget predictable, avoids overwhelming free-tier per-day quotas, and lets schema-validation fall-through happen per-job rather than blast-radius across a parallel batch
- Impact: 50 new jobs → 50 sequential cascade calls. Local Ollama is fast (~1–3s each), so ingestion latency is ~1–2 minutes for a typical batch. Higher when free providers exhaust and the cascade walks deeper
- Improvement path: Pool-parallel local Ollama (the only provider not subject to per-day request quotas) for the batch and serialize the rest. Or accept current latency since ingestion is a background scheduler job
- Priority: Medium — acceptable for background scheduler job

**Dedup Normalizer String Operations on Large Company Lists (Very Low Priority):**
- Problem: `job_finder/web/dedup_normalizer.py` applies regex patterns to company names in retroactive dedup loop. ~30 regex operations per company
- Files: `job_finder/web/dedup_normalizer.py` (lines 46-118 for suffix/title normalization)
- Cause: Each retroactive pass through all companies applies full normalization chain
- Impact: Retroactive dedup with 10,000 jobs takes ~5-10 seconds per run. Not a bottleneck in practice (runs once per month manually, not in hot path)
- Improvement path: Pre-compile all regexes (already done), memoize normalization results. Not needed for current scale
- Priority: Very Low

## Fragile Areas

**Pipeline Detector Email Classification (Multi-Signal Confidence Architecture):**
- Files: `job_finder/web/pipeline_detector.py` (entire module, 883 lines)
- Why fragile: Matches emails to jobs using 7 confidence signals (company match, title match, domain match, keyword match, ATS domain, rejection keyword, email snippet similarity). Confidence threshold requires 3+ signals for auto-update, otherwise queued for manual review. Multiple regex patterns across lines 27-90 could miss or false-match emails
- Safe modification: Test with full email corpus (60+ real emails) before deploying changes. Unit tests in `tests/test_pipeline_detector.py` exist but don't cover all email variations. Email parsing is inherently fragile (email format varies widely)
- Test coverage: Good — multi-signal approach gives defense-in-depth. Low false positive risk because auto-update requires 3+ signals
- Risk: False negatives (missed matches) more likely than false positives. User will see queued manual detections for review, so silent failures are caught

**Dedup Normalizer Title/Company Matching (Regex-Heavy):**
- Files: `job_finder/web/dedup_normalizer.py` (lines 46-118 for company suffixes and title abbreviations)
- Why fragile: Company suffix stripping uses regex that may miss variations (e.g., "Company & Assoc" won't match `[,\s]+corp` pattern). Title abbreviation expansion only covers known abbreviations (Sr, Jr, Mgr, etc.)
- Safe modification: Add integration test that verifies specific company variations normalize correctly. Example: "Apple Inc." and "Apple Inc" and "Apple" all map to same dedup_key. Currently passes unit tests but edge cases exist
- Test coverage: Moderate — `tests/test_dedup_normalizer.py` covers main patterns but not exhaustive variations
- Risk: Duplicate jobs created if normalization edge case not caught. Low impact because duplicates are visible to user and can be merged manually

**Indeed Parser Plain-Text Format Brittleness (After Fix Applied):**
- Files: `job_finder/parsers/indeed_parser.py` (lines 123-200, _parse_plaintext strategy)
- Why fragile: Plain-text parser relies on line order (title, company-location, salary) and specific regex patterns for URL extraction. If Indeed changes email format (different line order, URL pattern), parser breaks
- Safe modification: Test with 20+ real Indeed emails spanning 3-6 months to ensure format stability. Keep `_parse_plaintext()` as primary and HTML fallback as secondary. Add logging to track which strategy succeeds per email
- Test coverage: Needs improvement — current tests use fabricated emails, not real examples. Real email samples in `data/parse_failures/indeed_*.html` should be added to test suite
- Risk: Parser could silently return 0 jobs for new Indeed email format. Mitigation: log warning when parser finds 0 jobs, surface to user in activity log
- **Resolution:** Fixed in Phase 14 (v1.1, 2026-03-13). Plain-text parser is now primary strategy. HTML fallback retained as secondary. Test coverage added with real email samples.

**Anthropic Paid-Fallback Budget Gating (Gate is Checked but Error Can Be Deferred):**
- Files: `job_finder/web/claude_client.py` (cost_gate, BudgetExceededError), `job_finder/web/model_provider.py` (cascade dispatch — only the Anthropic link is gated)
- Why fragile: `cost_gate()` returns bool, caller decides whether to raise. If a new caller routes directly to the Anthropic provider without first calling `cost_gate(...)`, budget is bypassed. Free-provider hops in the cascade are exempt by design.
- Safe modification: Use decorator/context manager to enforce gating at the Anthropic-provider call site so it's structurally impossible to bypass. Currently: `if not cost_gate(...): raise BudgetExceededError(...)`.
- Test coverage: Good — tests in `tests/test_claude_client.py` and `tests/test_costs.py` verify budget gating behavior
- Risk: Accidental over-budget spend if budget gate is forgotten in a new Anthropic-direct caller. Mitigation: code review; structurally we'd prefer all Anthropic dispatch funnel through `model_provider.call_model()` rather than direct `claude_client.call_claude()` calls.

## Scaling Limits

**Monthly AI Scoring Budget Cap (By Design, Works Well):**
- Current capacity: $100/month default (configurable in `config.yaml`)
- Limit: Claude API calls stop when monthly_spend >= budget_cap
- Files: `job_finder/web/claude_client.py` (lines 101-138, cost_gate function)
- Scaling path: production cascade resolves on free providers in the typical case (Ollama → Groq → Cerebras → Gemini), so monthly spend is usually ~$0. The cap matters only when free providers all exhaust and the cascade falls through to Anthropic. At 50 new jobs/month with the cascade, typical spend is $0–2/month; a worst-case all-Anthropic month at 500 jobs would land around $50–75. Increase budget in config if the Anthropic-fallback path activates regularly.
- Assessment: ADEQUATE for current use case

**SQLite Database File Size (Write-Amplification from WAL Mode):**
- Current capacity: 50,000 jobs with metadata takes ~150MB SQLite file
- Limit: SQLite works fine up to 1GB+ but file locks may cause issues on slow storage (USB drives, network mounts). Not applicable here (local SSD)
- Scaling path: If database exceeds 1GB, consider archiving old jobs to separate .db file or migrating to PostgreSQL. For single-user app, SQLite is appropriate
- Assessment: Not a concern for foreseeable future (would need 300,000+ jobs to hit practical limit)

**APScheduler Background Job Pile-Up (If Ingestion Falls Behind):**
- Current capacity: Ingestion job runs every 15 minutes (configurable). If one run takes >15 minutes and next trigger fires, APScheduler will queue the next job (configurable `max_instances=1` prevents parallel runs)
- Limit: If ingestion consistently takes >15 minutes (e.g., 1000+ new emails per run), jobs queue up and app feels laggy
- Scaling path: Reduce ingestion interval, parallelize email parsing, or batch smaller subsets
- Assessment: Not observed in practice. Current ingestion on 100-200 emails per run takes ~30 seconds total
- Mitigation: Scheduler config (lines 215-238 in `scheduler.py`) has `max_instances=1` which prevents pile-up

## Dependencies at Risk

**APScheduler 3.11.x Pinning (Minor Risk):**
- Risk: Version 3.x is aging (last release 2020). Version 4.x has breaking async changes. Pinning prevents updates but limits security/stability improvements
- Files: `pyproject.toml` (APScheduler>=3.11,<4.0 in [project.dependencies])
- Impact: If future CVE found in APScheduler 3.11.x, only option is to migrate to 4.x (large effort)
- Migration plan: Monitor APScheduler 4.x adoption. When stable, create isolated test environment and validate scheduler behavior with async API
- Priority: Low — single-user local app, limited attack surface

**BeautifulSoup4 Email Parsing (Low Risk but Subtle):**
- Risk: Email parsing relies on BS4 HTML parsing for fallback strategy. If email format is unusual (broken HTML, mixed MIME types), parser may fail silently
- Files: `job_finder/parsers/indeed_parser.py`, `job_finder/parsers/linkedin_parser.py`, `job_finder/parsers/glassdoor_parser.py`
- Impact: Jobs from malformed emails not extracted. Silent failure (logged as warning)
- Mitigation: Good — HTML fallback only used when plain-text strategy fails. Most emails are well-formed. Real-world issues caught through parser test suite and manual email inspection
- Priority: Low

## Missing Critical Features

**Indeed Email Parser Complete Rewrite (NOW URGENT - Blocking All Indeed Ingestion):**
- Problem: Parser fails on ALL Indeed emails (0 jobs from 60+ real emails)
- Blocks: All Indeed job ingestion. Users with Indeed alerts configured get 0 jobs from those emails
- Severity: HIGH — Currently shipping broken
- Files affected: `job_finder/parsers/indeed_parser.py`
- Approach:
  1. Refactor to use plain-text parsing as primary (_parse_plaintext already exists but unused)
  2. Make HTML fallback secondary
  3. Test with real Indeed email corpus (20+ emails from data/parse_failures/)
  4. Add integration test to prevent regression
- **Resolution:** Fixed in Phase 14 (v1.1, 2026-03-13). Parser rewritten with plain-text primary strategy. All 60+ Indeed emails now parse successfully.

**Per-Email Parsing Granularity (Enhancement, Lower Priority):**
- Problem: email_parse_log table tracks runs but not individual emails within a run. If one email parses to 0 jobs, we don't know which email failed
- Blocks: Detailed parsing audit trail
- Approach: Add email_id column to pipeline_detections, log message_id alongside job_id
- Priority: Medium — useful for debugging but not blocking

## Test Coverage Gaps

**Indeed Parser Test Coverage (CRITICAL - Post-Fix):**
- What's not tested: Plain-text Indeed alert email parsing with real email structure
- Files: `tests/test_indeed_parser.py` (currently uses fabricated HTML fixtures)
- Risk: Parser could break on real email variations (format changes, encoding issues) without being caught
- Recommendation: Add test data directory with 10+ real Indeed emails from data/parse_failures/. Create parameterized test that runs parser against each. Verify title, company, salary, URL extraction
- Priority: HIGH after plain-text parser fix applied
- **Resolution:** Fixed in Phase 14 (v1.1, 2026-03-13). Test coverage added with parameterized tests against real Indeed email samples. See `tests/test_indeed_parser.py`.

**Pipeline Detector Multi-Signal Confidence (Good Coverage, but Needs Real Emails):**
- What's not tested: Real email samples across rejection/interview/confirmation categories
- Files: `tests/test_pipeline_detector.py` (uses synthetic emails)
- Risk: Confidence scoring thresholds (3+ signals for auto-update) may not align with real email characteristics
- Recommendation: Add sample emails from archive to test suite. Run detector against 50+ real emails and verify signal counts match expectations
- Priority: Medium

**Database Migration Idempotence (Good, but Could Add Negative Tests):**
- What's not tested: Migration behavior when columns/tables already exist (negative case)
- Files: `tests/test_db_migrate.py`
- Risk: Re-running migration with duplicate columns could silently skip statements without error handling
- Recommendation: Add test that runs migration twice and verifies second run completes without errors and schema is unchanged
- Priority: Low — Pattern is working, but test would improve confidence

## DB Layer Architecture (post-S7d)

The CLI-era DB layer was previously a single 845-LOC module (`job_finder/db.py`).
S7d (2026-05-06) split it into a package while preserving the public import
surface. Engineers reading downstream code do not need to update any
`from job_finder.db import X` paths — the package re-exports every name the
old module exposed.

**Package layout (`job_finder/db/`):**
- `__init__.py` (~66 LOC) — lifecycle + re-exports only. No module-level
  functions live here.
- `_classification.py` — `JobAssessment`, `derive_classification`,
  `_SUB_SCORE_KEYS`. Pure scoring-rule logic. Zero DB side-effects.
- `_persistence.py` — write paths: `log_run`, `persist_job_assessment`
  (depends on `_classification`), `persist_job_expiry_state`,
  `persist_job_archetype`, `update_pipeline_status`.
- `_jobs.py` — job CRUD: `upsert_job` (still D=21 — complexity reduction is
  out of scope for S7d), `get_job`, `merge_description`, `load_job_context`.
  Owns the canonical `JOBS_ALL_COLUMNS` projection re-imported by
  `_queries.py`.
- `_queries.py` — read filters: `get_filtered_jobs` (D=26), `get_distinct_sources`,
  legacy-shim helpers (`_classifications_for_min_score`/`_max_score`),
  classification-rank SQL fragments.

**Dual-path with `web/db_helpers.py` (preserved by design):** the package
above is the original CLI-era surface — every function takes an open
`sqlite3.Connection`. `job_finder/web/db_helpers.py` is the web-era
per-request `g.db` pattern used by Flask blueprints. They coexist; S7d did
not collapse them. Sibling modules `job_finder/db_pipeline.py` and
`job_finder/db_queries.py` (NOT inside the new package) are also
re-exported by `db/__init__.py` so callers continue to use a single import
path for any DB-layer name.

### sort_by Allowlist Co-location (Security-Critical Invariant)

CLAUDE.md documents the rule: `sort_by` is validated against a Python
allowlist before SQL interpolation, because SQLite does not support
parameterized column names in `ORDER BY`. The allowlist set literal
(`allowed_sort_cols` inside `get_filtered_jobs`) and the f-string composer
that interpolates `sort_by` into the query MUST stay in the same lexical
scope. A future split that promotes the allowlist to a module-level
constant in a separate file re-introduces SQL-injection surface even when
existing tests still pass.

- File: `job_finder/db/_queries.py` (the `get_filtered_jobs` function body
  carries a `SECURITY-CRITICAL` comment block at the entry).
- Sentinel: `tests/test_db_public_surface.py::test_malicious_sort_by_does_not_drop_table`
  asserts that a malicious `sort_by` value (`id; DROP TABLE jobs; --`) does
  not drop the jobs table. This sentinel ran on every intermediate S7d
  commit, not just the final one — bisectable proof.
- Rule for future maintainers: if `get_filtered_jobs` ever needs to relocate,
  move the WHOLE function (signature + allowlist + composer + WHERE-clause
  builder) as a single unit. Do not split pieces of it across files.
