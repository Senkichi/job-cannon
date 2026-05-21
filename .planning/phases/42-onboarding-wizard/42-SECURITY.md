---
phase: 42
slug: onboarding-wizard
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 42 — Security

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Browser wizard → local Flask app | First-time user enters provider, IMAP, resume, and schedule settings. | Credentials/config/profile values submitted to localhost. |
| Wizard completion → local filesystem/database | Done step writes config/profile and onboarding state. | Local config, profile JSON, onboarding completion flag. |
| Wizard → scheduler | First ingest is scheduled after onboarding completion. | Local scheduler state and ingest trigger. |

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-42-01 | Tampering | Done-step persistence | mitigate | Focused done-step tests verify atomic side effects and completion-state behavior. | closed |
| T-42-02 | Information Disclosure | Credential/profile forms | mitigate | Wizard stores data locally through existing config/profile persistence; no telemetry or remote storage introduced by the wizard. | closed |
| T-42-03 | Denial of Service | First ingest kickoff | mitigate | Tests cover final redirect and scheduler integration behavior recorded in Phase 42 summaries. | closed |

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
