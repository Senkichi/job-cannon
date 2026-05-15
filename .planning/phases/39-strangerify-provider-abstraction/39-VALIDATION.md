---
phase: 39
slug: strangerify-provider-abstraction
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-14
---

# Phase 39 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x (existing) |
| **Config file** | `pyproject.toml` (existing `[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run --active pytest tests/test_provider_detection.py tests/test_provider_claude_code_cli.py tests/test_provider_gemini_cli.py tests/test_provider_local_bundled.py tests/test_model_provider.py -q --tb=short` |
| **Full suite command** | `uv run --active pytest tests/ -q --tb=short` |
| **Estimated runtime** | ~60–90 seconds for new files; ~5–8 minutes full suite |

---

## Sampling Rate

- **After every task commit:** Run quick run command (new + modified test files only)
- **After every plan wave:** Run full suite command
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 90 seconds for quick run

---

## Per-Task Verification Map

*Populated by planner during Step 8. Each plan task ships with `<automated>` verify command. Map will list test commands per task ID, covering:*

- All 5 STRANGE-PROV-* requirements
- All 6 production providers' `.call()` signature consistency
- Legacy-tier-name translation regression (`tier="low"` → quick model, `tier="scoring"` → score model)
- `_PROVIDER_DEFAULTS` membership assertion
- Lazy `llama_cpp` import (no crash when extra not installed)
- Detection cache hit/miss paths
- Subprocess security: list-form args + `timeout=` on every `subprocess.run`

---

## Wave 0 Requirements

- [ ] `tests/test_provider_detection.py` — new file; stubs for STRANGE-PROV-02
- [ ] `tests/test_provider_claude_code_cli.py` — new file; stubs for STRANGE-PROV-01
- [ ] `tests/test_provider_gemini_cli.py` — new file; stubs for STRANGE-PROV-01
- [ ] `tests/test_provider_local_bundled.py` — new file; stubs for STRANGE-PROV-01 (uses `pytest.importorskip("llama_cpp")`)
- [ ] `tests/test_model_provider.py` — modify; add `_PROVIDER_DEFAULTS` + legacy-tier-translation tests for STRANGE-PROV-03

*pytest is already installed; no framework-install step required.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `gemini -p "ping"` returns a successful (non-429, non-error) response envelope | STRANGE-PROV-02 (liveness probe) | Live API call; user's quota state varies | Run `gemini -p "ping" --output-format json` from terminal; confirm `is_error: false`. Document envelope shape in code comment if it differs from the planner's assumption. |
| `llama-cpp-python` installs successfully on this Windows machine via `uv sync --extra local-ai` | STRANGE-PROV-01 (local_bundled provider) | Windows-specific CMake/MSVC toolchain dependency; install may need pre-built wheel | Run `uv sync --extra local-ai`; confirm import succeeds: `python -c "from llama_cpp import Llama; print(Llama.__module__)"`. If install fails, document the wheel-fallback URL in `pyproject.toml` comment. |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
