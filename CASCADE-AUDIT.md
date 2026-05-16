# Cascade Audit — Results

## Executive Summary

Audited callsites: parse_structured_fields, find_careers_url, extract_jobs, description_reformat, company_research, ai_nav_discovery
Case A/B decision: Case B (purpose_overrides)
Recommended cascade ordering: purpose_overrides

## Verdict Grid

| Callsite | Provider | Verdict | Sample Size | Confidence | Gates Failed |
|---|---|---|---:|---|---|
| parse_structured_fields | ollama | UNSUITABLE | 3 | 0.44-1.00 | None |
| parse_structured_fields | gemini | UNSUITABLE | 3 | 0.44-1.00 | None |
| parse_structured_fields | anthropic | UNSUITABLE | 3 | 0.44-1.00 | None |
| find_careers_url | ollama | SUITABLE | 3 | 0.44-1.00 | None |
| find_careers_url | gemini | SUITABLE | 3 | 0.44-1.00 | None |
| find_careers_url | anthropic | SUITABLE | 3 | 0.44-1.00 | None |
| extract_jobs | ollama | MARGINAL | 50 | 0.79-0.96 | row_execution |
| extract_jobs | gemini | MARGINAL | 50 | 0.79-0.96 | row_execution |
| extract_jobs | anthropic | MARGINAL | 50 | 0.79-0.96 | row_execution |
| description_reformat | ollama | UNSUITABLE | 10 | 0.40-0.89 | row_execution |
| description_reformat | gemini | UNSUITABLE | 10 | 0.17-0.69 | row_execution |
| description_reformat | anthropic | UNSUITABLE | 10 | 0.31-0.83 | row_execution |
| company_research | ollama | UNSUITABLE | 10 | 0.40-0.89 | row_execution |
| company_research | gemini | UNSUITABLE | 10 | 0.31-0.83 | row_execution |
| company_research | anthropic | UNSUITABLE | 10 | 0.49-0.94 | row_execution |
| ai_nav_discovery | ollama | UNSUITABLE | 3 | 0.00-0.56 | row_execution |
| ai_nav_discovery | gemini | UNSUITABLE | 3 | 0.00-0.56 | row_execution |
| ai_nav_discovery | anthropic | UNSUITABLE | 3 | 0.00-0.56 | row_execution |

## Per-Callsite Recommendations

### parse_structured_fields

Recommended cascade: anthropic
Rationale: R2 verdicts: ollama=UNSUITABLE, gemini=UNSUITABLE, anthropic=UNSUITABLE.

### find_careers_url

Recommended cascade: ollama → gemini → anthropic
Rationale: R2 verdicts: ollama=SUITABLE, gemini=SUITABLE, anthropic=SUITABLE.

### extract_jobs

Recommended cascade: ollama → gemini → anthropic
Rationale: R2 verdicts: ollama=MARGINAL, gemini=MARGINAL, anthropic=MARGINAL.

### description_reformat

Recommended cascade: anthropic
Rationale: R2 verdicts: ollama=UNSUITABLE, gemini=UNSUITABLE, anthropic=UNSUITABLE.

### company_research

Recommended cascade: anthropic
Rationale: R2 verdicts: ollama=UNSUITABLE, gemini=UNSUITABLE, anthropic=UNSUITABLE.

### ai_nav_discovery

Recommended cascade: anthropic
Rationale: R2 verdicts: ollama=UNSUITABLE, gemini=UNSUITABLE, anthropic=UNSUITABLE.

## Calibration Log

Check 1: description_reformat / ollama vs anthropic - PASS
Check 2: description_reformat / ollama vs anthropic - PASS
Check 3: description_reformat / ollama vs anthropic - PASS
Check 4: description_reformat / ollama vs anthropic - PASS
Check 5: description_reformat / ollama vs anthropic - PASS
Check 6: description_reformat / ollama vs anthropic - PASS
Check 7: description_reformat / ollama vs anthropic - PASS
Check 8: description_reformat / gemini vs anthropic - PASS
Check 9: description_reformat / gemini vs anthropic - PASS
Check 10: description_reformat / gemini vs anthropic - PASS

10/10 passed (≤2 errors threshold met)

## Case A/B Decision

Case B (purpose_overrides)

purpose_overrides:
  parse_structured_fields: anthropic
  extract_jobs: ollama → gemini → anthropic
  description_reformat: anthropic
  company_research: anthropic
  ai_nav_discovery: anthropic

## Risk Callouts

MARGINAL providers enter the cascade with warnings: extract_jobs/ollama, extract_jobs/gemini, extract_jobs/anthropic
Borderline re-runs: none recorded in available artifacts.
