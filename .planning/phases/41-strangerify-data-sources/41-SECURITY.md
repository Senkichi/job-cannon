---
phase: 41
slug: strangerify-data-sources
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 41 — Security

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| User Gmail IMAP account → local ingestion | IMAP app-password source fetches job-alert messages. | Email message metadata and bodies for configured job alerts. |
| Resume file → parser/LLM extraction | Local PDF/DOCX text extraction feeds structured profile generation. | Resume text and extracted profile fields. |
| Parser output → local config/profile files | Parsed profile can be written locally for onboarding. | Experience profile JSON. |

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-41-01 | Information Disclosure | Resume parser logging | mitigate | Summary records that warning logs include file path/exception only and never extracted resume text or LLM response content. | closed |
| T-41-02 | Denial of Service | Resume parse failures | mitigate | Parser catches parse/LLM failures and returns empty profile instead of blocking onboarding. | closed |
| T-41-03 | Information Disclosure | IMAP credentials/messages | mitigate | Focused IMAP tests validate source behavior; credentials remain local configuration, not committed artifacts. | closed |

## Accepted Risks Log

No accepted risks.

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-21 | 3 | 3 | 0 | Cascade |

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-21
