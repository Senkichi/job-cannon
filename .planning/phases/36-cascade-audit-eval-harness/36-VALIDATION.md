---
phase: 36
slug: cascade-audit-eval-harness
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-14
---

# Phase 36 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml (existing) |
| **Quick run command** | `uv run pytest tests/ -k "cascade_audit" -v` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -k "cascade_audit" -v`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 36-01-01 | 01 | 1 | AUDIT-05 | — | OpenRouter provider uses HTTPS, validates API key | unit | `uv run pytest tests/test_providers.py::test_openrouter_provider` | ❌ W0 | ⬜ pending |
| 36-02-01 | 02 | 1 | AUDIT-06 | — | Corpus loader uses parameterized queries, no SQL injection | unit | `uv run pytest tests/evals/test_corpus_loader.py::test_load_round_0` | ❌ W0 | ⬜ pending |
| 36-03-01 | 03 | 1 | AUDIT-07 | — | Judge uses temperature=0, validates JSON schema | unit | `uv run pytest tests/evals/test_judge.py::test_judge_pair` | ❌ W0 | ⬜ pending |
| 36-04-01 | 04 | 2 | AUDIT-08 | — | Adapters implement TaskAdapter protocol, type hints validated | integration | `uv run pytest tests/evals/test_adapters.py::test_parse_structured_fields_adapter` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/evals/test_providers.py` — stubs for OpenRouter provider (AUDIT-05)
- [ ] `tests/evals/test_corpus_loader.py` — stubs for corpus loader (AUDIT-06)
- [ ] `tests/evals/test_judge.py` — stubs for judge protocol (AUDIT-07)
- [ ] `tests/evals/test_adapters.py` — stubs for adapter protocol (AUDIT-08)
- [ ] `tests/conftest.py` — shared fixtures for eval harness testing

*Existing infrastructure covers pytest framework via pyproject.toml.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| OpenRouter API key validation | AUDIT-05 | Requires live API credential | Set OPENROUTER_API_KEY, run judge CLI, verify endpoint reachable |
| Playwright browser launch | AUDIT-08 | Requires browser installation | Run ai_nav_discovery adapter with cached recipe, verify browser launches |
| Position-swap agreement measurement | AUDIT-07 | Requires statistical analysis | Run judge on sample pairs, compute agreement rate manually |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
