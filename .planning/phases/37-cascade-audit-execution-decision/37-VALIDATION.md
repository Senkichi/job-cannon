---
phase: 37
slug: cascade-audit-execution-decision
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-14
---

# Phase 37 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run --active pytest tests/test_audit_harness.py -v` |
| **Full suite command** | `uv run --active pytest tests/ -k audit -v` |
| **Estimated runtime** | ~120 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run --active pytest tests/test_audit_harness.py -v`
- **After every plan wave:** Run `uv run --active pytest tests/ -k audit -v`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 37-01-01 | 01 | 1 | AUDIT-09 | — | N/A | integration | `uv run --active pytest tests/test_audit_execution.py::test_round_execution -v` | ✅ W0 | ⬜ pending |
| 37-01-02 | 01 | 1 | AUDIT-10 | — | N/A | integration | `uv run --active pytest tests/test_audit_execution.py::test_cascade_audit_md_generation -v` | ✅ W0 | ⬜ pending |
| 37-01-03 | 01 | 1 | AUDIT-11 | — | N/A | manual | Manual spot-check of 10 judge verdicts | ❌ N/A | ⬜ pending |
| 37-01-04 | 01 | 1 | AUDIT-12 | — | N/A | integration | `uv run --active pytest tests/test_audit_execution.py::test_case_a_b_decision_explicit -v` | ✅ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_audit_execution.py` — stubs for AUDIT-09, AUDIT-10, AUDIT-12
- [ ] `tests/conftest.py` — shared fixtures (already exists)
- [ ] pytest framework (already installed)

*Existing infrastructure covers all phase requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Judge verdict spot-checking | AUDIT-11 | Requires human judgment of LLM output quality | User manually reviews 10 judge verdicts from R2 artifacts; records pass/fail in calibration log; ≤2 errors allowed |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
