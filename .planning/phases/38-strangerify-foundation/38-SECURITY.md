---
phase: 38
slug: strangerify-foundation
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 38 — Security

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| App runtime → OS user-data directory | Config, DB, logs, and cache paths move to platform-specific user data locations. | Local config/database paths and application data. |
| First-run config bootstrap → filesystem | Missing config can be tolerated and later written atomically. | User config values written to local disk. |
| Public repo → new users | Example files and docs must not leak maintainer-specific personal data. | Public documentation and example config/profile files. |

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-38-01 | Information Disclosure | Public example files/docs | mitigate | Personal-data audit genericized emails and local paths; prompt templates were verified clean. | closed |
| T-38-02 | Tampering | Config writes | mitigate | `write_config` uses temp file plus `os.replace()` for atomic local config writes. | closed |
| T-38-03 | Spoofing/Confusion | Config path resolution | mitigate | `JOB_CANNON_USER_DATA_DIR` override and platformdirs behavior are covered by tests. | closed |

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
