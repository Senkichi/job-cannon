---
phase: 37
slug: cascade-audit-execution-decision
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 37 — Security

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Local eval harness → OpenRouter judge | Phase 37 uses judge calls for audit verdicts when credentials are configured. | Evaluation prompts and sampled production-like rows; API key remains in environment/config, not artifacts. |
| Eval artifacts → committed report | Raw artifacts are generated locally while committed report summarizes decisions. | Verdict summaries and routing decisions; raw artifacts remain outside committed source. |

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-37-01 | Information Disclosure | Cascade audit artifacts | mitigate | Summary records generated report and local artifacts; no credential material is committed. | closed |
| T-37-02 | Tampering | Case A/B decision report | mitigate | Integration tests and audit completeness checks verify report structure and explicit Case B decision. | closed |

## Accepted Risks Log

No accepted risks.

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-21 | 2 | 2 | 0 | Cascade |

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-21
