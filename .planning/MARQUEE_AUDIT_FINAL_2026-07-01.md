# Marquee Coverage Audit Final Verdict

**Date**: 2026-07-01 (updated after remediation)
**Audit Tool**: `scripts/marquee_audit.py` (v2 with req_id URL extraction, fuzzy threshold, consumed tracking)
**Ground Truth**: `.planning/marquee_ground_truth.json` (2026-06-23 capture)
**Database**: jobs.db (main repo)

## Summary

The marquee coverage audit was run using the remediated `scripts/marquee_audit.py` tool, which implements:
- Actual req_id extraction from job URLs (not just source_id)
- Token-set fuzzy matching with 70% threshold (replacing naive substring containment)
- Consumed job tracking to prevent double-counting
- Exact-match-first company mapping with ambiguity warnings
- ASCII-only output for Windows compatibility

The audit covers 11 marquee companies with external ground-truth data.

## Per-Company Verdicts (Post-Remediation)

| Company | GT Roles | Our Target Roles | Sample Match | Platform | Verdict |
|---------|----------|------------------|--------------|---------|---------|
| Google | 120 | 161 | 12/12 | greenhouse | sample fully covered (161 analyst/DS ours; conf=medium) |
| NVIDIA | 47 | 34 | 8/12 | workday | partial: 4 sample roles not found (conf=medium) |
| Meta | 80 | 15 | 9/12 | - | partial: 3 sample roles not found (conf=high) |
| Apple | 25 | 53 | 11/12 | - | partial: 1 sample role not found (conf=medium) |
| Amazon | 175 | 298 | 12/12 | amazon | sample fully covered (298 analyst/DS ours; conf=high) |
| Microsoft | 17 | 19 | 11/12 | microsoft | partial: 1 sample role not found (conf=medium) |
| Netflix | 36 | 46 | 12/12 | eightfold | sample fully covered (46 analyst/DS ours; conf=high) |
| Tesla | 71 | 16 | 6/12 | - | partial: 6 sample roles not found (conf=high) |
| Salesforce | 10 | 69 | 5/10 | workday | partial: 5 sample roles not found (conf=high) |
| Adobe | 22 | 39 | 10/12 | workday | partial: 2 sample roles not found (conf=high) |
| Cisco | 14 | 4 | 2/14 | - | partial: 12 sample roles not found (conf=medium) |

## Remediation Impact

### NVIDIA Improvement
NVIDIA sample match improved from 3/12 to 8/12 after implementing actual req_id extraction from job URLs. The prior implementation only checked byte-identical URL equality or source_id match; the remediated version extracts req_ids from each URL in `source_urls_raw` and compares them, handling path structure differences (e.g., location segment present/absent).

### Companies with No ATS Platform Configuration
Several marquee companies (IBM, Intel, Uber, Airbnb, LinkedIn, Stripe, Snowflake, Databricks, Oracle) show 0 analyst/DS roles on board despite having live roles in ground truth. These companies either:
1. Have no ATS platform configured in the companies table
2. Have ATS probe status != 'hit'
3. Have scan_enabled = 0

This is expected behavior for companies not yet set up for ATS scanning.

### Tesla Coverage Gap
Tesla shows significant under-coverage (6/12 sample match, 16 vs 71 live). Tesla uses a custom careers site without a standard ATS platform, so it would require a custom crawler or HTML fallback scan.

## Match Method Breakdown

The remediated matcher uses the following priority:
1. **req_id match**: Direct match on source_id field or extracted from source_id path
2. **URL match**: Extract req_id from each URL in source_urls_raw and compare to GT req_id
3. **title_fuzzy match**: Token-set similarity with 70% threshold, employment-type disqualifiers (intern/contract), consumed tracking

## Tool Deliverables

1. **`scripts/marquee_audit.py`**: Committed tool for reusable marquee coverage auditing
   - Loads ground-truth JSON
   - Maps companies to DB rows with exact-match-first logic and ambiguity warnings
   - Reports per-company coverage with improved matcher
   - Supports custom ground-truth path via `--gt-path`

2. **`scripts/verify_scanner_live.py`**: Adversarial harness for live scanner verification
   - Requires `--ground-truth` argument (no self-comparison fallback)
   - Runs live single-company scan
   - Diffs against independent ground-truth file
   - Exits non-zero if live analyst/DS role is missed
   - ASCII-only output for Windows compatibility
   - Usage: `uv run python scripts/verify_scanner_live.py <company_id> --ground-truth <path>`

## Recommendations

1. **Run Workday scanner for NVIDIA**: The NVIDIA coverage gap (8/12 sample match) is partially resolved by URL req_id extraction, but 4 sample roles remain unmatched. A fresh Workday ATS scan may capture the remaining roles.

2. **Configure ATS platforms for untracked companies**: IBM, Intel, Uber, Airbnb, LinkedIn, Stripe, Snowflake, Databricks, and Oracle should be configured for ATS scanning where applicable.

3. **Schedule regular audits**: The `scripts/marquee_audit.py` tool should be run after each scanner lands and on a schedule to track coverage improvements.

4. **Use verify_scanner_live.py for DoD**: Each scanner issue's DoD should reference `scripts/verify_scanner_live.py --ground-truth <path>` as the adversarial verification harness.
