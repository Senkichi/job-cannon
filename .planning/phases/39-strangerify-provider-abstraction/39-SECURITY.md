---
phase: 39
slug: strangerify-provider-abstraction
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 39 — Security

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| App → external API providers | Anthropic/Gemini-style providers send prompts to remote APIs when configured. | Job data and scoring prompts governed by provider credentials. |
| App → local CLI providers | Claude/Gemini/Ollama CLI providers invoke local subprocesses. | Prompt text crosses process boundary on the user's machine. |
| App → local bundled model | Optional local model loads local inference dependency/model. | Prompt text stays local. |

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-39-01 | Information Disclosure | Provider routing | mitigate | Provider abstraction preserves explicit provider selection and test coverage for provider identity/cost fields. | closed |
| T-39-02 | Tampering | CLI provider subprocess invocation | mitigate | Cross-provider tests cover error propagation and consistent result shape; provider-specific I/O is isolated in mocks. | closed |
| T-39-03 | Denial of Service | Optional local bundled dependency | mitigate | Local bundled provider uses lazy import/test coverage so missing optional dependency does not break module import. | closed |

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
