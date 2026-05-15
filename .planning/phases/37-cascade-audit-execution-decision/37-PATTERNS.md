# Phase 37: Cascade Audit Execution & Decision - Pattern Map

**Created:** 2026-05-14
**Status:** Complete

## Files to Create/Modify

### 1. evals/cascade_audit/run_audit.py (NEW)
**Role:** Audit CLI entrypoint
**Data flow:** Command-line args → harness orchestration → artifact writes
**Classification:** Execution script

**Closest analog:** `evals/cascade_audit/harness.py` (from Phase 36)
**Pattern:** CLI argument parsing with argparse, round-specific orchestration functions

**Code excerpt from Phase 36 harness pattern:**
```python
# evals/cascade_audit/harness.py (Phase 36)
def run_calibration_round(corpus, judge_adapter, callsite_adapters):
    """R0: Dry-run validation (n=1-3)"""
    for callsite, adapter in callsite_adapters.items():
        sample = corpus.sample(n=1, purpose=callsite)
        results = adapter.evaluate(sample, judge_adapter)
        write_artifact(f'artifacts/round_0/{callsite}_r0.json', results)
```

**Expected pattern for run_audit.py:**
```python
# evals/cascade_audit/run_audit.py (Phase 37)
import argparse
from evals.cascade_audit.harness import AuditHarness

def main():
    parser = argparse.ArgumentParser(description='Execute cascade audit')
    parser.add_argument('--round', choices=['r0', 'r1', 'r2'], required=True,
                        help='Audit round to execute')
    parser.add_argument('--callsite', help='Specific callsite (default: all)')
    args = parser.parse_args()

    harness = AuditHarness(db_path='jobs.db')
    if args.round == 'r0':
        harness.run_calibration(callsite=args.callsite)
    elif args.round == 'r1':
        harness.run_contenders(callsite=args.callsite)
    elif args.round == 'r2':
        harness.run_head_to_head(callsite=args.callsite)

if __name__ == '__main__':
    main()
```

### 2. CASCADE-AUDIT.md (NEW at repo root)
**Role:** Comprehensive audit report
**Data flow:** Raw artifacts → aggregation → markdown report
**Classification:** Documentation artifact

**Closest analog:** None (new report type)
**Pattern:** Markdown report with tables, sections, and explicit decision block

**Expected pattern from design spec section 7:**
```markdown
# Cascade Audit Report

## Executive Summary
- 6 callsites audited: [list]
- Case A/B decision: [explicit choice]
- Recommended cascade ordering: [verbatim list from config.yaml]

## Verdict Grid
| Callsite | Provider | Verdict | Sample Size | Confidence | Gates Failed |
|----------|----------|---------|-------------|------------|--------------|
| parse_structured_fields | ollama | SUITABLE | 50 | 95% | None |
| find_careers_url | groq | MARGINAL | 50 | 92% | latency |

## Per-Callsite Recommendations
### parse_structured_fields
- Recommended cascade: ollama → groq → cerebras
- Rationale: All gates passed, cost-effective ordering

### find_careers_url
- Recommended cascade: groq → ollama (purpose_overrides)
- Rationale: MARGINAL on ollama due to latency gate

## Calibration Log
10 spot-checks performed (spec section 11):
- Check 1: parse_structured_fields / ollama vs anthropic - PASS
- Check 2: find_careers_url / groq vs anthropic - PASS
...
**Result:** 9/10 passed (≤2 errors threshold met)

## Case A/B Decision
**Decision:** Case B (purpose_overrides)
**Rationale:** find_careers_url has no SUITABLE providers
**Cascade ordering:**
- Default cascade: ollama → groq → cerebras → gemini
- purpose_overrides:
  - find_careers_url: groq → ollama
```

### 3. evals/cascade_audit/artifacts/round_N/ (NEW directory structure)
**Role:** Atomic artifact storage per round
**Data flow:** Harness execution → JSON writes → read by report generator
**Classification:** Data artifacts (gitignored)

**Closest analog:** None (new artifact structure)
**Pattern:** Round-specific subdirectories with JSON files

**Expected directory structure:**
```
evals/cascade_audit/artifacts/
├── round_0/
│   ├── parse_structured_fields_r0.json
│   ├── find_careers_url_r0.json
│   └── ...
├── round_1/
│   ├── parse_structured_fields_r1.json
│   └── ...
└── round_2/
    ├── parse_structured_fields_r2.json
    ├── find_careers_url_r2.json
    └── ...
```

**Artifact file pattern (from Phase 36):**
```python
artifact = {
    'round': 'r2',
    'callsite': 'parse_structured_fields',
    'timestamp': datetime.now().isoformat(),
    'config_snapshot': load_config_snapshot(),
    'model_versions': {
        'ollama': 'qwen2.5:14b',
        'groq': 'llama-3.1-70b',
        'judge': 'deepseek-v3.2'
    },
    'commit_sha': get_git_sha(),
    'results': {
        'provider': 'ollama',
        'sample_size': 50,
        'accuracy': 0.92,
        'latency_p50_ms': 450,
        'latency_p95_ms': 890,
        'cost_per_1k': 0.001,
        'schema_valid_rate': 0.98,
        'verdict': 'SUITABLE',
        'confidence_interval': [0.85, 0.96]
    }
}
```

### 4. tests/test_audit_execution.py (NEW)
**Role:** Integration tests for audit execution
**Data flow:** Test fixtures → harness calls → assertions
**Classification:** Test file

**Closest analog:** `tests/test_audit_harness.py` (from Phase 36)
**Pattern:** pytest fixtures, round-specific test functions, artifact validation

**Code excerpt from Phase 36 test pattern:**
```python
# tests/test_audit_harness.py (Phase 36)
@pytest.fixture
def mock_harness():
    with tempfile.TemporaryDirectory() as tmpdir:
        harness = AuditHarness(db_path=':memory:', artifact_dir=tmpdir)
        yield harness

def test_calibration_round(mock_harness):
    results = mock_harness.run_calibration()
    assert results is not None
    assert os.path.exists(f'{mock_harness.artifact_dir}/round_0')
```

**Expected pattern for test_audit_execution.py:**
```python
# tests/test_audit_execution.py (Phase 37)
import pytest
from evals.cascade_audit.run_audit import main
from pathlib import Path

@pytest.fixture
def artifact_dir(tmp_path):
    dir_path = tmp_path / 'artifacts'
    dir_path.mkdir()
    return dir_path

def test_round_execution(artifact_dir, monkeypatch):
    # Mock harness dependencies
    # Execute round via CLI
    # Verify artifacts created
    assert (artifact_dir / 'round_0').exists()

def test_cascade_audit_md_generation(tmp_path):
    # Generate CASCADE-AUDIT.md
    # Verify file exists at repo root
    # Verify required sections present
    cascade_md = tmp_path / 'CASCADE-AUDIT.md'
    assert cascade_md.exists()
    content = cascade_md.read_text()
    assert '## Executive Summary' in content
    assert '## Case A/B Decision' in content

def test_case_a_b_decision_explicit(tmp_path):
    # Verify decision block is explicit
    cascade_md = tmp_path / 'CASCADE-AUDIT.md'
    content = cascade_md.read_text()
    assert 'Case A' in content or 'Case B' in content
    assert 'purpose_overrides' in content or 'single shared cascade' in content
```

## Existing Patterns to Reuse

### 1. Harness Orchestration Pattern
**Source:** `evals/cascade_audit/harness.py` (Phase 36)
**Reuse:** Round-specific orchestration functions already implemented
**Adaptation:** Add resume-scheduler prompt emission at end of Round 2

### 2. Artifact Write Pattern
**Source:** `evals/cascade_audit/harness.py` (Phase 36)
**Reuse:** Atomic writes with environment provenance blocks
**Adaptation:** No changes needed - use as-is

### 3. Judge Protocol Pattern
**Source:** `evals/cascade_audit/judge_protocol.py` (Phase 36)
**Reuse:** Pairwise-blind comparison + position-swap
**Adaptation:** No changes needed - use as-is

### 4. Verdict Gates Pattern
**Source:** `evals/cascade_audit/verdict_gates.py` (Phase 36)
**Reuse:** ADT definitions and gate threshold logic
**Adaptation:** No changes needed - use as-is

### 5. Corpus Loader Pattern
**Source:** `evals/cascade_audit/corpus_loader.py` (Phase 36)
**Reuse:** Production DB row replay with purpose grouping
**Adaptation:** No changes needed - use as-is

## Integration Points

### 1. Phase 36 Harness Dependency
**File:** `evals/cascade_audit/harness.py`
**Integration:** run_audit.py imports and orchestrates harness methods
**Pattern:** Direct function calls, no subclassing

### 2. Production DB Access
**File:** `jobs.db` (SQLite)
**Integration:** Corpus loader queries `scoring_costs` table
**Pattern:** Read-only access, purpose-based filtering

### 3. OpenRouter API
**File:** `providers/openrouter_provider.py` (Phase 36)
**Integration:** Judge adapter uses OpenRouter for DeepSeek-V3.2
**Pattern:** API key from env var, rate limiting handled by provider

### 4. CASCADE-AUDIT.md Consumer
**File:** Phase 40 config rewire
**Integration:** Phase 40 reads CASCADE-AUDIT.md to determine Case A/B decision
**Pattern:** File read at repo root, parses explicit decision block

## Anti-Patterns to Avoid

### 1. Custom Judge Implementation
**Don't:** Implement new judge logic
**Do:** Use Phase 36's judge_protocol.py module

### 2. Custom Verdict Gates
**Don't:** Modify gate thresholds or logic
**Do:** Use Phase 36's verdict_gates.py ADTs

### 3. CASCADE-AUDIT.md in .planning/
**Don't:** Write CASCADE-AUDIT.md to .planning/ directory
**Do:** Write to repo root (design spec section 7 requirement)

### 4. Skipping Borderline Re-runs
**Don't:** Accept ambiguous verdicts near gate boundaries
**Do:** Re-run with n=200 if within 1 Wilson CI half-width (D-05)

### 5. Implicit Case A/B Decision
**Don't:** Leave decision ambiguous or implied
**Do:** Explicitly state "Case A" or "Case B" with cascade ordering (success criterion 4)

---

## PATTERN MAPPING COMPLETE

All files to create/modified identified with existing analogs and code patterns. This is an execution phase with minimal new patterns - primarily CLI orchestration and report generation.

*Phase: 37-cascade-audit-execution-decision*
*Pattern mapping completed: 2026-05-14*
