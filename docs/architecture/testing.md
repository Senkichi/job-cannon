# Testing Patterns

This document describes the testing approach, fixtures, and conventions used in `tests/` for engineers reading the source. For setup and run instructions, see [docs/SETUP.md](../SETUP.md).

## Test Framework

**Runner:**
- pytest 7+ (version not pinned in requirements.txt, but pytest is a dev dependency)
- Config: No `pytest.ini` or `pyproject.toml` config file present
- Run tests: `pytest tests/` (default discovery)

**Assertion Library:**
- Built-in `assert` statements (pytest's assertion rewriting)
- No external assertion library (unittest.mock used for mocking only)

**Run Commands:**
```bash
pytest tests/                              # Run all tests
pytest tests/test_pipeline_detector.py -v  # Run specific test file with verbose output
pytest -x                                  # Stop on first failure
pytest tests/test_scoring.py::TestCostComputation::test_haiku_input_pricing  # Run single test
```

## Test File Organization

**Location:**
- Tests are co-located in `tests/` directory (separate from source)
- All test files in one top-level `tests/` directory (flat structure)
- No `tests/unit/` or `tests/integration/` subdivisions

**Naming:**
- `test_<module>.py` pattern: `test_scoring.py`, `test_pipeline_detector.py`, `test_costs.py`
- Mirror module names from `job_finder/` but without deep subdirectories
- Example: tests for `job_finder/web/claude_client.py` are in `tests/test_costs.py` and `tests/test_scoring.py` (grouped by feature, not file)

**File Count:**
- ~85 test files total
- Large test files: `test_scoring.py`, `test_data_enricher.py`, `test_pipeline_detector.py` (hundreds of lines each)

## Test Structure

**Suite Organization:**
```python
# tests/test_scoring.py
import pytest

class TestCostComputation:
    """Verify compute_cost math for Haiku and Sonnet pricing."""

    def test_haiku_input_pricing(self):
        cost = compute_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 1.0) < 1e-9

    def test_haiku_output_pricing(self):
        cost = compute_cost("claude-haiku-4-5", input_tokens=0, output_tokens=1_000_000)
        assert abs(cost - 5.0) < 1e-9


class TestCostRecording:
    """Verify record_cost inserts to scoring_costs and returns cost."""

    def test_record_cost_inserts_row(self, migrated_db):
        path, conn = migrated_db
        record_cost(conn, job_id="job-1", purpose="haiku_score", ...)
        rows = conn.execute("SELECT * FROM scoring_costs").fetchall()
        assert len(rows) == 1
```

**Patterns:**
- Tests organized in classes by feature/responsibility: `TestCostComputation`, `TestCostRecording`, `TestCostGate`
- Each class groups related test methods (typically 3-8 tests)
- Test method names: `test_<description_of_what_is_tested>`
- Descriptive docstrings on test classes, not usually on individual test methods

**Setup/Teardown:**
- No class-level setup (all fixtures use function-level scope)
- Fixtures handle all initialization and cleanup
- Most tests are stateless: each test gets fresh data from fixture

## Test Structure (Fixtures)

**Shared Fixtures in conftest.py:**

```python
# tests/conftest.py

@pytest.fixture
def migrated_db():
    """Create a temp DB, run ALL migrations (including Migration 2), yield (path, conn).

    This is the standard fixture for all Phase 2 AI scoring tests.
    Closes connection and removes file on teardown.
    """
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield path, conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)
```

**Fixture Scope:**
- Function scope (default) for most fixtures: `@pytest.fixture` (no scope parameter)
- Class scope for shared DB: `@pytest.fixture(scope="class")` used sparingly for pure schema reads
- Session scope not used

**Key Fixtures:**

| Fixture | Purpose | Returns |
|---------|---------|---------|
| `migrated_db` | Temp DB with all migrations | `(path, conn)` tuple |
| `migrated_db_with_jobs` | Temp DB with 3 sample jobs | `(path, conn)` tuple |
| `migrated_db_class` | Shared DB for class scope | `(path, conn)` tuple |
| `sample_db_with_jobs` | Old schema DB (pre-migration) | path to .db file |
| `app` | Flask test app with config | Flask app instance |
| `client` | Flask test client | test_client() |
| `tmp_db_path` | Temp SQLite file path | path string |
| `app_config` | Dict with test config values | config dict |
| `mock_anthropic_client` | Mocked Anthropic client | MagicMock |

## Mocking

**Framework:** `unittest.mock` (standard library)

**Patterns:**

```python
from unittest.mock import MagicMock, patch, call

# Mock Anthropic client response
mock_response = MagicMock()
mock_response.content = [MagicMock()]
mock_response.content[0].text = json.dumps({"score": 75, "summary": "Good match"})
mock_response.usage.input_tokens = 100
mock_response.usage.output_tokens = 50

mock_client = MagicMock()
mock_client.messages.create.return_value = mock_response

# Use in test
from job_finder.web.claude_client import call_claude
result, cost = call_claude(..., client=mock_client, ...)
```

**Injection Pattern:**
- Anthropic client is passed as parameter: `call_claude(..., client=mock_client, ...)`
- Gmail API is passed as parameter or mocked at module level with `@patch`
- SerpAPI is mocked with `@patch("requests.get")`

**What to Mock:**
- External API calls (Anthropic, Gmail, SerpAPI)
- HTTP requests (requests.get)
- File I/O operations (rarely tested, usually mocked)

**What NOT to Mock:**
- Database operations (use real migrated_db fixture instead)
- Date/time (use real datetime, not freezegun)
- JSON parsing (test real behavior)
- String normalization (test real algorithms)

## Fixtures and Factories

**Test Data:**

```python
# tests/conftest.py (lines 245-315)
@pytest.fixture
def migrated_db_with_jobs():
    """Create a temp DB, run ALL migrations (including Migration 3), insert 3 sample jobs."""
    ...
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, user_interest,
             pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "stripe|senior data scientist|remote",
                "Senior Data Scientist",
                "Stripe",
                "Remote",
                '["linkedin"]',
                '["https://www.linkedin.com/jobs/view/1111/"]',
                "1111",
                180000, 240000,
                "Build data products at Stripe.",
                five_days_ago, now, 8.5, '{}', "interested", "reviewing",
            ),
            # ... 2 more rows
        ],
    )
```

**Helper Functions:**
```python
# tests/test_db.py (lines 36-52)
def _insert_job(conn, dedup_key, title="Test Job", company="Test Co",
                location="Remote", pipeline_status="discovered",
                sonnet_score=None, haiku_score=None):
    """Insert a minimal job row for testing."""
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             pipeline_status, first_seen, last_seen, score, score_breakdown,
             user_interest, sonnet_score, haiku_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, title, company, location, '["test"]',
         f'["https://example.com/{dedup_key}"]',
         pipeline_status, now, now, 7.0, '{}', 'unreviewed',
         sonnet_score, haiku_score),
    )
    conn.commit()
```

**Location:**
- Test data fixtures defined in `tests/conftest.py` (shared) or in test file itself (local)
- Helper functions prefixed with `_`: `_insert_job()`, `_insert_cost_rows()`, `_create_test_db_with_job()`

## Coverage

**Requirements:** Not enforced (no coverage config present)

**View Coverage:**
- No pytest-cov or coverage.py configuration
- Coverage not automated or tracked in CI

**Status:** 266+ tests passing as of 2026-03 (per user documentation)

## Test Types

**Unit Tests:**
- Scope: Single function in isolation
- Approach: Mock external dependencies (Anthropic API, database mocked away in isolated unit tests)
- Examples: `TestCostComputation` tests the math of `compute_cost()` with no fixtures needed
- Count: ~15-20 test classes are pure unit tests (no fixtures)

**Integration Tests:**
- Scope: Function + database together
- Approach: Use `migrated_db` fixture (real SQLite DB), real function behavior
- Examples: `TestCostRecording::test_record_cost_inserts_row()` tests that `record_cost()` actually inserts into DB
- Count: ~15-20 test classes test database interaction

**Flask Route Tests:**
- Scope: HTTP endpoint + database
- Approach: Use `client` fixture (test Flask client) + `app` fixture
- Examples: `test_get_costs_returns_200()`, `test_costs_html_contains_canvas()`
- Pattern:
```python
def test_get_costs_returns_200(self, client):
    response = client.get("/costs")
    assert response.status_code == 200

def test_costs_html_contains_canvas(self, client):
    response = client.get("/costs")
    assert b"<canvas" in response.data
```

**E2E Tests:** Not used (no Selenium, Playwright, or similar)

## Common Patterns

**Async Testing:** Not used (app is synchronous Flask, no async features)

**Error Testing:**
```python
# tests/test_scoring.py
def test_sonnet_blocked_when_over_budget(self, migrated_db, gate_config):
    """sonnet calls are blocked when monthly spend >= budget cap."""
    path, conn = migrated_db
    config = gate_config
    config["scoring"]["monthly_budget_usd"] = 0.01  # $0.01 cap

    # Insert costs that exceed budget
    record_cost(conn, "job-1", "sonnet_eval", "claude-sonnet-4-6", 1000, 500)

    # Try to gate another sonnet call
    allowed = cost_gate(conn, config, "sonnet")
    assert allowed is False
```

**Floating-point Precision:**
```python
# tests/test_scoring.py (lines 32, 37, 43, 48, 53, 59)
def test_haiku_input_pricing(self):
    cost = compute_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
    assert abs(cost - 1.0) < 1e-9  # Tolerance for float comparison
```

**List/Dict Assertions:**
```python
# tests/test_costs.py (lines 45-58)
def test_returns_list_of_dicts(self, migrated_db):
    result = get_daily_cost_breakdown(conn)
    assert len(result) == 1
    assert "date" in result[0]
    assert "purpose" in result[0]
    assert "spend" in result[0]
```

**Row Count Assertions:**
```python
# tests/test_scoring.py (line 77)
def test_record_cost_inserts_row(self, migrated_db):
    path, conn = migrated_db
    record_cost(conn, job_id="job-1", ...)
    rows = conn.execute("SELECT * FROM scoring_costs").fetchall()
    assert len(rows) == 1
```

## Test Organization by Phase

**Phase 1 (Foundation):**
- `test_db.py`: Raw DB operations
- `test_parsers.py`: Email parser logic
- `test_dedup_normalizer.py`: Dedup key normalization

**Phase 2 (AI Scoring):**
- `test_scoring.py`: Cost computation, recording, gating
- `test_costs.py`: Cost aggregation routes and views
- Tests use `migrated_db` fixture with Migration 2 schema

**Phase 3 (Pipeline Automation):**
- `test_pipeline_detector.py`: Email classification and job matching
- `test_pipeline.py`: Pipeline status updates
- Tests use `migrated_db` fixture with pipelines table

## Isolation and Cleanup

**Database Isolation:**
- Each test gets fresh DB (tempfile created per test)
- No shared state between tests
- Migrations run fresh per DB (idempotent migrations support this)

**Fixture Cleanup:**
```python
@pytest.fixture
def migrated_db():
    """..."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield path, conn  # Test runs here

    # Cleanup after test
    conn.close()
    if os.path.exists(path):
        os.remove(path)
```
