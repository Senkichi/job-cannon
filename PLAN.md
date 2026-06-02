# Execution Plan — Test Suite Remediation

**Contract:** `.planning/test-suite-remediation-plan.md`. This file is my execution
plan + deltas from the literal plan that I verified against the live code.

## Task restatement

Fix **tests, not product** (one narrow exception: C1 corrects a test's stated intent).
The suite is already green. Three production seams do real outbound I/O (a `claude -p`
subprocess + real HTTP/DDG/SerpAPI) that the conftest autouse net never covered, so a
handful of "unit" tests make real calls (12–63s each) and pass by environmental accident.
We block those seams in tests so they run fast and deterministically, fix one test whose
name/docstring asserts something false (C1), and apply minor hardening (m1–m3, nit).

**Success:** suite stays green (pass count ≥ baseline; collection floor ≥2130 holds);
the formerly-slow homepage/ats/data_enricher/agentic tests drop to sub-second; ruff clean.

**Out of scope:** no new product-code coverage; no product behavior change (the Tier-3
no-key question in C1 is flagged, not implemented); Phase 5 global socket kill-switch is a
recommendation only.

## Verified facts (against live code)

- `claude_enricher.py:137` `subprocess.run(...)`, `except FileNotFoundError` at :149 → `[]`.
  Patching `claude_enricher.subprocess.run` with `FileNotFoundError` exercises real handling.
- `homepage_discoverer.discover_homepage`: Tier 3 (`_try_claude_enricher`) is **unconditional**
  (:168); Tier 4 SerpAPI gated by `api_key is not None` (:173). → C1 is correct.
- ats: sibling tests patch `job_finder.web.ats_scanner._run.run_homepage_discovery` (confirmed
  at :1426/:3191/:3208/:3265). Module-autouse no-op is safe; per-test `with patch(...)` overrides.
- data_enricher imports 8 tier fns from `enrichment_tiers` (:45-53). **Verified real
  no-result shapes** (plan's literal dict had 3 crashers — plan authorized adjusting):

  | fn | sig return | plan literal | corrected stub |
  |----|-----------|--------------|----------------|
  | fetch_direct_jd | `str\|None` | None | `None` ✓ |
  | query_ats_api | `dict` | None | `{}` |
  | scrape_careers | `dict` | None | `{}` |
  | search_ddg_web | `dict` (caller does `.get()`) | `[]` ✗ | `{}` |
  | fetch_ddg_jds | `tuple` (caller unpacks 2) | None ✗ | `(None, None)` |
  | search_duckduckgo | `str\|None` | `[]` | `None` |
  | search_serpapi | `tuple` (caller unpacks 2) | `[]` ✗ | `(None, [])` |

  (`parse_structured_fields` left unstubbed, matching the plan's fixture.)
- log_throttle: `import time`; `time.monotonic()` at :36 → patch `lt.time.monotonic`.

## Files touched (in order, one commit per phase)

**Phase 1 (C1/M1/M2):**
1. `tests/conftest.py` — add `block_claude_cli_subprocess` autouse (FileNotFoundError).
2. `tests/test_ats_scanner.py` — add module-autouse `_no_op_homepage_discovery_in_ats`.
3. `tests/test_homepage_discoverer.py:300` — rename `test_no_api_key_skips_tier3` →
   `test_no_api_key_skips_serpapi_tier4`, rescope docstring/assert to the api_key-gated tier.

**Phase 2 (M3/M3b):**
4. `tests/test_data_enricher.py` — add `stub_enrichment_network` fixture (corrected shapes),
   apply `@pytest.mark.usefixtures` to `TestDescriptionPromotion` (:1275) and
   `TestEnrichJobBackwardCompat` (:581).
5. `tests/test_agentic_enricher.py:860` — mock `_search_ddg` + `_fetch_page_text` to kill the
   real DDG search + 1.5s sleeps. Scan file for other unpatched `enrich_one_job`/
   `enrich_single_job` callers.

**Phase 3 (m1/m2/m3/nit) — minor, m1 skippable if flaky:**
6. `tests/conftest.py` — session `_migrated_template_db` + copy-based `migrated_db`/
   `migrated_db_with_jobs`.
7. `tests/test_log_throttle.py:51` — fake `time.monotonic`, drop `time.sleep(1.1)`.
8. `tests/test_migration_069..075_*.py` — before/after count assertions on no-op tests.
9. `tests/test_imap_parser_roundtrip.py:118` — assert `.eml` glob non-empty.

## Tests / verification per phase

Per the plan's per-phase verify commands (`-q --durations`), then Phase 4 full run
(`-m "not integration and not e2e"`), ruff check/format, regression-guard single-test run,
and the `_classification` mutation sanity check (revert after).

## Failure modes I'll handle

- Stub shape mismatch → AttributeError/unpack error inside enrich_job (corrected above).
- ats tests that assert `run_homepage_discovery` IS called → their inner `with patch` wins.
- m1 WAL/copy → if "file is not a database"/"locked", fall back to `sqlite3` backup API.
- m1 any flakiness I can't quickly resolve → skip m1, keep Phases 1–2.

## DELTA / decision needed — M3b meaningfulness

The plan's M3b says the fixed test "must fail if the `except RuntimeError → return {}`
handler is removed." **No such handler exists.** The RuntimeError from `OllamaProvider()`
is swallowed at two inner layers: `_generate_queries` (`except Exception` → heuristic
fallback) and `_validate_page` (`except Exception` → `(False, 0.0)`); the only top-level
catch is `enrich_one_job:526` `except Exception → {}`. With the plan's mocks the error
never propagates to :526 — the test returns `{}` because validation yields no-match, which
is indistinguishable from genuine no-match. So the plan's **speed** goal is fully met
(20.5s → <1s) but its **discriminating** goal / sanity-check is infeasible as written.

I will implement the plan's fix (strictly better than today; assertion unchanged) and
report the limitation honestly rather than fabricate a passing sanity-check. **Question for
you below** on whether that's acceptable or you want a more discriminating test.
