---
phase: 35
slug: audit-telemetry-callsite-attribution
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-21
---

# Phase 35 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Application code → local SQLite telemetry DB | Phase 35 writes cost telemetry into the local `scoring_costs` table. | Job IDs, call purpose labels, model/provider identifiers, token counts, costs, schema-validity booleans. |
| LLM response parser → telemetry attribution | Schema validation outcome is converted to persisted `schema_valid` integers. | Boolean validation outcome only; no prompt or credential data added by this phase. |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-35-01 | Tampering | `scoring_costs.schema_valid` telemetry | mitigate | Automated tests verify both `_maybe_record_cost` and `record_cost` persist `schema_valid` as `0` or `1`; focused suite passed on 2026-05-21. | closed |
| T-35-02 | Information Disclosure | Per-callsite telemetry purpose split | mitigate | Phase stores purpose labels (`find_careers_url`, `extract_jobs`) and numeric telemetry only; no new credential, prompt, or email-body persistence was introduced. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

No accepted risks.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-21 | 2 | 2 | 0 | Cascade |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-21
