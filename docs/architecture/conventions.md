# Coding Conventions

This document describes the coding conventions used across `job_finder/` for engineers reading the source. For setup and run instructions, see [docs/SETUP.md](../SETUP.md).

## Naming Patterns

**Files:**
- snake_case for all Python files (e.g., `job_scorer.py`, `pipeline_detector.py`)
- Descriptive names aligned with functionality (not generic like `utils.py`)
- Web module files grouped by feature: `claude_client.py`, `model_provider.py`, `job_scorer.py`, `scoring_orchestrator.py`, `pipeline_detector.py`
- Test files use `test_<module>.py` pattern (e.g., `test_scoring.py`, `test_pipeline_detector.py`)

**Functions:**
- snake_case consistently across all modules
- Descriptive verb-noun pattern: `compute_cost()`, `record_cost()`, `cost_gate()`, `get_cost_stats()`, `call_claude()`
- Helper functions prefixed with underscore: `_build_comp_context()`, `_classify_email()`, `_insert_job()` (test helper)
- Boolean functions often start with verb: `cost_gate()`, `salary_meets_floor()`, but not explicit `is_` prefix

**Variables:**
- snake_case for all local and module-level variables
- Descriptive names: `dedup_key`, `source_urls`, `monthly_budget_usd`, `input_tokens`, `output_tokens`
- Avoid single-letter except in very tight loops (`i` in for loops acceptable)
- Type-hint hints embedded in name when appropriate: `cost_usd`, `timestamp`, `conn` (sqlite3.Connection)

**Classes:**
- PascalCase only for class names: `BudgetExceededError`, `Job`
- Dataclasses used for model types (e.g., `@dataclass class Job`)
- Custom exception: `BudgetExceededError` inherits from `Exception`

**Constants:**
- UPPERCASE_SNAKE_CASE for module-level constants
- Examples: `MODEL_PRICING`, `HAIKU_SCHEMA`, `DEFAULT_MONTHLY_BUDGET_USD`, `ALLOWED_FK_TABLES`
- Regex patterns prefixed with underscore: `_COMPANY_SUFFIXES`, `_TITLE_ABBREVS`

## Code Style

**Formatting:**
- No formatter configured (Black, Ruff, etc. not in use)
- PEP 8 followed implicitly
- Line length observed around 100-120 characters (not rigidly enforced)
- 4-space indentation throughout

**Type Hints:**
- Used on function signatures: `def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:`
- Used on dataclass fields: `def __init__(self, db_path: str = "jobs.db"):`
- Union types use `|` syntax (Python 3.10+): `dict | None`, `str | int`
- Optional types use `Optional[T]` from typing: `Optional[str]`, `Optional[int]`
- Complex types spelled out: `list[dict]`, `dict[str, float]`, `tuple[dict, float]`

**Linting:**
- No linter configuration file present (`.eslintrc`, `.flake8`, `.pylintrc`)
- Code follows PEP 8 conventions without automated enforcement
- No pre-commit hooks configured

## Import Organization

**Order:**
1. Standard library (json, sqlite3, logging, re, datetime, typing, etc.)
2. Third-party libraries (flask, anthropic, google, requests, etc.)
3. Local job_finder imports

**Examples from codebase:**
```python
# job_finder/web/claude_client.py
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from job_finder.config import DEFAULT_MONTHLY_BUDGET_USD

# job_finder/web/job_scorer.py
import logging
from typing import Any

from job_finder.web.model_provider import call_model
from job_finder.web.claude_client import BudgetExceededError
```

**Path Aliases:**
- Absolute imports from `job_finder` package root: `from job_finder.config import load_config`
- Never relative imports (`from . import` not used)
- Web module self-imports: `from job_finder.web.claude_client import call_claude`

**Module Structure:**
- No barrel files; `__init__.py` files are mostly empty
- Each file is self-contained; no multi-module exports from `__init__.py`

## Error Handling

**Pattern: Try-Except with Specific Exceptions**
```python
# job_finder/web/db_helpers.py — safe_json_load utility
try:
    parsed = json.loads(value) if isinstance(value, str) else value
except (ValueError, TypeError):
    return default
```

**Pattern: Custom Exception for Budget Limits**
- `BudgetExceededError` raised when Anthropic paid-fallback calls would exceed the monthly cap (free-provider cascade hops are never budget-gated)
- Defined in `job_finder/web/claude_client.py` (line 34)
- Caught in calling code to decide whether to re-run or skip enrichment

**Pattern: Return None on Parsing Failure**
```python
# job_finder/web/job_scorer.py
def _build_comp_context(job_row: dict) -> str | None:
    """..."""
    comp = json.loads(comp_data_raw) if isinstance(comp_data_raw, str) else comp_data_raw
    if not comp or not isinstance(comp, dict):
        return None  # Safe default for optional data
```

**Pattern: Exception Logging Without Re-raising**
```python
# job_finder/web/claude_client.py (lines 346-354)
except (json.JSONDecodeError, AttributeError):
    result = {"text": str(text)}  # Fallback to plain text response
```

## Logging

**Framework:** `logging` standard library

**Pattern:**
```python
import logging

logger = logging.getLogger(__name__)

def score_job(...) -> ScoringResult:
    """..."""
    logger.info(f"Scoring job {job_id} via cascade")
    try:
        ...
    except BudgetExceededError as e:
        logger.warning(f"Anthropic paid-fallback budget exceeded: {e}")
        return None
```

**Usage:**
- Logger initialized per module: `logger = logging.getLogger(__name__)`
- Levels: `logger.info()` for normal flow, `logger.warning()` for budget/recoverable issues, `logger.error()` for failures
- f-strings used for interpolation: `logger.info(f"Processing {job_id}")`

## Comments

**When to Comment:**
- Explain WHY, not WHAT (code is self-documenting)
- Document design decisions and alternatives: "Location intentionally excluded from dedup_key (Plan ABC)"
- Mark known limitations: "no test coverage for streaming responses"
- Separate complex business logic from implementation details

**Examples:**
```python
# job_finder/models.py (lines 35-36)
"""Normalized key for deduplication.

Uses company+title only (location intentionally excluded per user decision:
same company + same title = same job regardless of location differences).
"""
```

## Docstrings

**Pattern: Module-level and Function-level**

**Module Docstrings:**
- Triple-quoted string at top of file
- Explains purpose, exports, and key concepts
- Example: `job_finder/web/claude_client.py` lines 1-14 document pricing, cost functions, and exceptions

**Function Docstrings:**
- Google-style docstrings (Args, Returns, Raises)
- Used consistently for public functions
- Short one-liner for private helpers or obvious functions

```python
def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a Claude API call.

    Args:
        model: Model identifier, e.g. "claude-haiku-4-5".
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.

    Returns:
        Cost in USD as a float.

    Raises:
        KeyError: If model is not in MODEL_PRICING.
    """
```

## Function Design

**Size:** Functions range from 10-50 lines (no hard limit)
- Short, focused helpers: `compute_cost()` is 5 lines
- Medium complex functions: `record_cost()` is 20 lines
- Larger orchestrators: `call_claude()` is 80 lines (still focused on single API call)

**Parameters:**
- Pass explicit parameters, no kwargs splat except where intentional (`**call_kwargs` in `call_claude`)
- Use type hints for all public functions
- Optional parameters use `Optional[T]` or `| None` syntax
- Default values when safe: `days: int = 30` in `get_daily_cost_breakdown()`

**Return Values:**
- Single return values for most functions
- Tuple returns when multiple related values: `tuple[dict, float]` for `call_claude()`
- None returns for optional/failed operations: `str | None` for `_build_comp_context()`
- No void functions (all return something, even if just implicit None)

## Module Design

**Exports:**
- No `__all__` declarations (all public functions available)
- Private functions prefixed with underscore: `_build_comp_context()`

**Database Pattern:**
- Functions take `conn: sqlite3.Connection` as explicit parameter
- No global connection state
- Caller responsible for opening/closing connection
- Raw SQL parameterized: `execute(..., (params,))`

**Configuration:**
- Functions take `config: dict` as parameter, not reading global state
- Config keys namespaced: `config["scoring"]["monthly_budget_usd"]`
- Fallback defaults provided: `config.get("scoring", {}).get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)`

## Special Patterns

**Input Validation:**
```python
# job_finder/web/dedup_normalizer.py (lines 31-38)
ALLOWED_FK_TABLES: frozenset = frozenset({...})
# Used to validate table names before SQL interpolation
```

**Regex as Module-level Constants:**
```python
# job_finder/web/dedup_normalizer.py
_COMPANY_SUFFIXES = re.compile(r"""...""", re.IGNORECASE | re.VERBOSE)
_TITLE_ABBREVS = [
    (compiled_pattern, replacement_string),
    ...
]
```

**Context Managers (minimal use):**
- No `with` statements observed in primary code
- Fixtures handle setup/teardown in tests
