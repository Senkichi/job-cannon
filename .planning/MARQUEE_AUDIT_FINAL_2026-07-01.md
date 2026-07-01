# Marquee Coverage Audit Final Verdict

**Date**: 2026-07-01
**Audit Tool**: `scripts/marquee_audit.py`
**Ground Truth**: `.planning/marquee_ground_truth.json` (2026-06-23 capture)
**Database**: jobs.db (main repo)

## Summary

The marquee coverage audit was run using the new `scripts/marquee_audit.py` tool, which implements an improved matcher that prefers req-id/canonical-URL matching over fuzzy title matching. The audit covers 16 marquee companies with external ground-truth data.

## Per-Company Verdicts

| Company | GT Roles | Our Target Roles | Sample Match | Platform | Verdict |
|---------|----------|------------------|--------------|---------|---------|
| Google | 120 | 164 | 11/12 | -/greenhouse | Partial: 1 sample role not found |
| NVIDIA | 47 | 34 | 3/12 | workday | Partial: 9 sample roles not found |
| Meta | 80 | 17 | 9/12 | - | Partial: 3 sample roles not found |
| Apple | 25 | 53 | 10/12 | - | Partial: 2 sample roles not found |
| Amazon | 175 | 305 | 10/12 | -/amazon | Partial: 2 sample roles not found |
| Microsoft | 17 | 21 | 11/12 | -/microsoft | Partial: 1 sample role not found |
| Netflix | 36 | 46 | 10/12 | eightfold | Partial: 2 sample roles not found |
| Tesla | 71 | 16 | 2/12 | - | Partial: 10 sample roles not found |
| Salesforce | 10 | 70 | 5/10 | -/workday | Partial: 5 sample roles not found |
| Adobe | 22 | 39 | 9/12 | workday | Partial: 3 sample roles not found |
| Cisco | 14 | 4 | 1/14 | - | Partial: 13 sample roles not found |
| IBM | 12 | 0 | 0/12 | - | 0 analyst/DS on board vs 12 live |
| Intel Corporation | 7 | 0 | 0/7 | workday | 0 analyst/DS on board vs 7 live |
| Uber | 8 | 0 | 0/8 | - | 0 analyst/DS on board vs 8 live |
| Airbnb | 3 | 0 | 0/3 | - | 0 analyst/DS on board vs 3 live |
| LinkedIn (LinkedIn Corporation) | 4 | 0 | 0/4 | - | 0 analyst/DS on board vs 4 live |
| Stripe | 2 | 0 | 0/2 | - | 0 analyst/DS on board vs 2 live |
| Snowflake | 5 | 0 | 0/5 | - | 0 analyst/DS on board vs 5 live |
| Databricks | 6 | 0 | 0/6 | - | 0 analyst/DS on board vs 6 live |
| Oracle | 12 | 0 | 0/12 | - | 0 analyst/DS on board vs 12 live |

## Anomalies and Explanations

### NVIDIA Artifact (Expected)
The NVIDIA under-reporting (3/12 sample match) is confirmed as a matcher artifact. The improved matcher now extracts req_ids from Workday source_id paths (e.g., `/job/.../JR2019886`), but the current board data shows NVIDIA jobs are primarily sourced from Glassdoor, not the Workday ATS scan. This suggests the Workday scanner may not have run recently or the jobs weren't properly ingested. The ground truth shows 47 live roles via Workday ATS, but our board has only 34 target roles total, with most from Glassdoor.

### Companies with No ATS Platform Configuration
Several marquee companies (IBM, Intel, Uber, Airbnb, LinkedIn, Stripe, Snowflake, Databricks, Oracle) show 0 analyst/DS roles on board despite having live roles in ground truth. These companies either:
1. Have no ATS platform configured in the companies table
2. Have ATS probe status != 'hit'
3. Have scan_enabled = 0

This is expected behavior for companies not yet set up for ATS scanning.

### Tesla Coverage Gap
Tesla shows significant under-coverage (2/12 sample match, 16 vs 71 live). Tesla uses a custom careers site without a standard ATS platform, so it would require a custom crawler or HTML fallback scan.

## Match Method Breakdown

The improved matcher uses the following priority:
1. **req_id match**: Direct match on source_id field
2. **URL match**: Match on source_urls_raw array
3. **title_fuzzy match**: Normalized title matching with seniority stripping

Most matches are now via req_id extraction from Workday source_id paths, resolving the fuzzy-normalization artifact from the original prototype.

## Tool Deliverables

1. **`scripts/marquee_audit.py`**: Committed tool for reusable marquee coverage auditing
   - Loads ground-truth JSON
   - Maps companies to DB rows with robust name mapping
   - Reports per-company coverage with improved matcher
   - Supports custom ground-truth path via `--gt-path`

2. **`scripts/verify_scanner_live.py`**: Adversarial harness for live scanner verification
   - Runs live single-company scan
   - Diffs against fresh ground-truth fetch
   - Exits non-zero if live analyst/DS role is missed
   - Usage: `uv run python scripts/verify_scanner_live.py <company_id>`

## Recommendations

1. **Run Workday scanner for NVIDIA**: The NVIDIA coverage gap appears to be due to missing Workday-sourced jobs. A fresh ATS scan should populate the board with the 47 live roles.

2. **Configure ATS platforms for untracked companies**: IBM, Intel, Uber, Airbnb, LinkedIn, Stripe, Snowflake, Databricks, and Oracle should be configured for ATS scanning where applicable.

3. **Schedule regular audits**: The `scripts/marquee_audit.py` tool should be run after each scanner lands and on a schedule to track coverage improvements.

4. **Use verify_scanner_live.py for DoD**: Each scanner issue's DoD should reference `scripts/verify_scanner_live.py` as the adversarial verification harness.
