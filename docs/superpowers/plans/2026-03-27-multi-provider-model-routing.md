# Multi-Provider Model Routing & Evaluation Framework — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all AI model calls configurable to route through Anthropic, Gemini, or Ollama via config.yaml, with an evaluation framework to benchmark alternatives.

**Architecture:** Adapter pattern — a `call_model()` dispatcher resolves logical tier names ("sonnet") to provider+model via config, delegates to provider-specific adapters (Anthropic, Gemini, Ollama), validates structured output with retry/fallback, and records costs. Existing `call_claude()` stays untouched as the Anthropic adapter's backend.

**Tech Stack:** Python 3.13, google-genai (Gemini SDK), jsonschema (validation), scipy (eval metrics), requests (Ollama REST), existing anthropic SDK.

**Spec:** `docs/superpowers/specs/2026-03-27-multi-provider-model-routing-design.md`

---

## Chunk 1: Foundation — Dependencies, DB Migration, Core Types

### Task 1: Install new dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add new packages to requirements.txt**

Add these three lines to `requirements.txt` (alphabetical insertion):

```
google-genai>=1.0.0
jsonschema>=4.0.0
scipy>=1.10.0
```

- [ ] **Step 2: Install dependencies**

Run: `uv pip install -r requirements.txt`
Expected: All packages install successfully.

- [ ] **Step 3: Verify imports work**

Run: `uv run python -c "import google.genai; import jsonschema; import scipy; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add google-genai, jsonschema, scipy dependencies for multi-provider support"
```

---

### Task 2: DB migration — add provider column to scoring_costs

**Files:**
- Modify: `job_finder/web/db_migrate.py` (after line 412, append to MIGRATIONS list)
- Test: `tests/test_db_migrate.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_db_migrate.py`, add:

```python
def test_migration_18_adds_provider_column(migrated_db):
    """Migration 18 adds provider column to scoring_costs."""
    _path, conn = migrated_db
    # Check column exists
    cursor = conn.execute("PRAGMA table_info(scoring_costs)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "provider" in columns, "provider column missing from scoring_costs"

    # Check default value
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
        "VALUES ('test', 'test', 'test-model', 100, 50, 0.01, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    row = conn.execute("SELECT provider FROM scoring_costs WHERE job_id = 'test'").fetchone()
    assert row[0] == "anthropic", f"Expected default 'anthropic', got '{row[0]}'"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_migrate.py::test_migration_18_adds_provider_column -v`
Expected: FAIL — `provider` column does not exist yet.

- [ ] **Step 3: Add migration 18 to MIGRATIONS list**

In `job_finder/web/db_migrate.py`, after the migration 17 block (line 412, before the closing `]` of the MIGRATIONS list), add:

```python
    # Migration 18: Add provider column to scoring_costs table.
    # Tracks which provider (anthropic, gemini, ollama) handled each API call.
    # Default 'anthropic' for all existing rows (pre-multi-provider).
    [
        "ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic'",
    ],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db_migrate.py::test_migration_18_adds_provider_column -v`
Expected: PASS

- [ ] **Step 5: Run all migration tests**

Run: `uv run pytest tests/test_db_migrate.py -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/db_migrate.py tests/test_db_migrate.py
git commit -m "feat: add migration 18 — provider column on scoring_costs"
```

---

### Task 3: ModelResult dataclass and BaseProvider ABC

**Files:**
- Create: `job_finder/web/model_provider.py`
- Create: `job_finder/web/providers/__init__.py`
- Test: `tests/test_model_provider.py`

- [ ] **Step 1: Write the failing test for ModelResult**

Create `tests/test_model_provider.py`:

```python
"""Tests for model_provider dispatcher and core types."""

import pytest


def test_model_result_fields():
    """ModelResult has all required fields."""
    from job_finder.web.model_provider import ModelResult

    result = ModelResult(
        data={"score": 75},
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.01
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.model == "claude-sonnet-4-6"
    assert result.provider == "anthropic"
    assert result.schema_valid is True


def test_base_provider_is_abstract():
    """BaseProvider cannot be instantiated directly."""
    from job_finder.web.model_provider import BaseProvider

    with pytest.raises(TypeError):
        BaseProvider()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_provider.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create providers package**

Create `job_finder/web/providers/__init__.py` (empty file):

```python
```

- [ ] **Step 4: Create model_provider.py with core types**

Create `job_finder/web/model_provider.py`:

```python
"""Multi-provider model dispatcher.

Routes AI model calls to provider-specific adapters (Anthropic, Gemini, Ollama)
based on config.yaml. Drop-in replacement for call_claude().
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Result from a provider adapter call."""

    data: dict
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    schema_valid: bool


class BaseProvider(ABC):
    """Abstract base for provider adapters."""

    @abstractmethod
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a model call and return structured result."""
        ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_provider.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/model_provider.py job_finder/web/providers/__init__.py tests/test_model_provider.py
git commit -m "feat: add ModelResult dataclass and BaseProvider ABC"
```

---

### Task 4: Config resolution logic

**Files:**
- Modify: `job_finder/web/model_provider.py`
- Modify: `job_finder/config.py` (add DEFAULT_PROVIDER constant)
- Test: `tests/test_model_provider.py`

- [ ] **Step 1: Write failing tests for config resolution**

Add to `tests/test_model_provider.py`:

```python
def test_resolve_provider_from_config():
    """Resolves provider + model from providers config section."""
    from job_finder.web.model_provider import resolve_provider_config

    config = {
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
        "providers": {
            "sonnet": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "fallback": "anthropic",
            },
        },
    }
    result = resolve_provider_config("sonnet", config)
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-2.0-flash"
    assert result["fallback"] == "anthropic"


def test_resolve_provider_missing_falls_back_to_anthropic():
    """Missing providers section defaults to Anthropic with scoring.models name."""
    from job_finder.web.model_provider import resolve_provider_config

    config = {
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
    }
    result = resolve_provider_config("sonnet", config)
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-4-6"
    assert result["fallback"] is None


def test_resolve_provider_model_missing_uses_scoring_models():
    """providers.sonnet exists but model omitted — falls back to scoring.models."""
    from job_finder.web.model_provider import resolve_provider_config

    config = {
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
        "providers": {
            "sonnet": {"provider": "ollama"},
        },
    }
    result = resolve_provider_config("sonnet", config)
    assert result["provider"] == "ollama"
    assert result["model"] == "claude-sonnet-4-6"


def test_resolve_provider_no_config_uses_defaults():
    """Empty config uses DEFAULT_MODEL_* constants."""
    from job_finder.web.model_provider import resolve_provider_config

    result = resolve_provider_config("sonnet", {})
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-4-6"


def test_resolve_provider_haiku_tier():
    """Haiku tier resolves correctly."""
    from job_finder.web.model_provider import resolve_provider_config

    config = {
        "scoring": {"models": {"haiku": "claude-haiku-4-5"}},
        "providers": {
            "haiku": {"provider": "ollama", "model": "llama3.2:8b"},
        },
    }
    result = resolve_provider_config("haiku", config)
    assert result["provider"] == "ollama"
    assert result["model"] == "llama3.2:8b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_provider.py::test_resolve_provider_from_config -v`
Expected: FAIL — `resolve_provider_config` not defined.

- [ ] **Step 3: Implement resolve_provider_config()**

Add to `job_finder/web/model_provider.py`:

```python
from job_finder.config import (
    DEFAULT_MODEL_HAIKU,
    DEFAULT_MODEL_OPUS,
    DEFAULT_MODEL_SONNET,
)

# Map tier names to their DEFAULT_MODEL_* fallbacks
_DEFAULT_MODELS: dict[str, str] = {
    "haiku": DEFAULT_MODEL_HAIKU,
    "sonnet": DEFAULT_MODEL_SONNET,
    "opus": DEFAULT_MODEL_OPUS,
}


def resolve_provider_config(tier: str, config: dict) -> dict:
    """Resolve a logical tier name to provider + model + fallback.

    Resolution order:
    1. providers.<tier> section in config
    2. scoring.models.<tier> for model name
    3. DEFAULT_MODEL_* constants

    Returns dict with keys: provider, model, fallback (or None).
    """
    providers_cfg = config.get("providers", {})
    scoring_models = config.get("scoring", {}).get("models", {})
    default_model = _DEFAULT_MODELS.get(tier, DEFAULT_MODEL_SONNET)

    tier_cfg = providers_cfg.get(tier, {})
    provider = tier_cfg.get("provider", "anthropic")
    model = tier_cfg.get("model") or scoring_models.get(tier) or default_model
    fallback = tier_cfg.get("fallback")

    return {"provider": provider, "model": model, "fallback": fallback}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/model_provider.py tests/test_model_provider.py
git commit -m "feat: add resolve_provider_config() for tier-to-provider mapping"
```

---

### Task 5: Schema validation module

**Files:**
- Modify: `job_finder/web/model_provider.py`
- Test: `tests/test_model_provider.py`

- [ ] **Step 1: Write failing tests for schema validation**

Add to `tests/test_model_provider.py`:

```python
def test_validate_output_valid():
    """Valid output passes schema validation."""
    from job_finder.web.model_provider import validate_output

    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer"},
            "summary": {"type": "string"},
        },
        "required": ["score", "summary"],
    }
    result = {"score": 75, "summary": "Good match"}
    errors = validate_output(result, schema)
    assert errors == []


def test_validate_output_missing_required():
    """Missing required field returns errors."""
    from job_finder.web.model_provider import validate_output

    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer"},
            "summary": {"type": "string"},
        },
        "required": ["score", "summary"],
    }
    result = {"score": 75}
    errors = validate_output(result, schema)
    assert len(errors) > 0
    assert any("summary" in e for e in errors)


def test_validate_output_wrong_type():
    """Wrong type returns errors."""
    from job_finder.web.model_provider import validate_output

    schema = {
        "type": "object",
        "properties": {"score": {"type": "integer"}},
        "required": ["score"],
    }
    result = {"score": "not a number"}
    errors = validate_output(result, schema)
    assert len(errors) > 0


def test_validate_output_no_schema_always_passes():
    """None schema means no validation — always passes."""
    from job_finder.web.model_provider import validate_output

    errors = validate_output({"anything": "goes"}, None)
    assert errors == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_provider.py::test_validate_output_valid -v`
Expected: FAIL — `validate_output` not defined.

- [ ] **Step 3: Implement validate_output()**

Add to `job_finder/web/model_provider.py`:

```python
import jsonschema


def validate_output(result: dict, schema: dict | None) -> list[str]:
    """Validate a result dict against a JSON Schema.

    Returns list of human-readable error strings. Empty list means valid.
    Returns empty list if schema is None (no validation).
    """
    if schema is None:
        return []

    validator = jsonschema.Draft7Validator(schema)
    return [error.message for error in validator.iter_errors(result)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/model_provider.py tests/test_model_provider.py
git commit -m "feat: add validate_output() for JSON Schema validation"
```

---

## Chunk 2: Provider Adapters

### Task 6: Anthropic adapter

**Files:**
- Create: `job_finder/web/providers/anthropic_provider.py`
- Test: `tests/test_anthropic_provider.py`

The Anthropic adapter wraps the existing `call_claude()` API call logic — specifically the `client.messages.create()` call, response parsing, and cost computation. It does NOT call `call_claude()` directly (that would double-record costs). Instead, it extracts the same logic.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_provider.py`:

```python
"""Tests for Anthropic provider adapter."""

import json
from unittest.mock import MagicMock

import pytest


def test_anthropic_provider_call_returns_model_result():
    """AnthropicProvider.call() returns a well-formed ModelResult."""
    from job_finder.web.model_provider import ModelResult
    from job_finder.web.providers.anthropic_provider import AnthropicProvider

    # Mock the Anthropic client
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = {"score": 75, "summary": "Good match"}
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    provider = AnthropicProvider(client=mock_client)
    result = provider.call(
        model="claude-sonnet-4-6",
        system="You are a helper.",
        messages=[{"role": "user", "content": "Hello"}],
        output_schema={"type": "object", "properties": {"score": {"type": "integer"}}},
    )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75, "summary": "Good match"}
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-4-6"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cost_usd > 0


def test_anthropic_provider_text_response():
    """AnthropicProvider handles text responses (no output_schema)."""
    from job_finder.web.providers.anthropic_provider import AnthropicProvider

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({"text": "Hello world"})
    # Remove .input attribute so hasattr check falls through to text path
    del mock_response.content[0].input
    mock_response.usage.input_tokens = 50
    mock_response.usage.output_tokens = 20

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    provider = AnthropicProvider(client=mock_client)
    result = provider.call(
        model="claude-haiku-4-5",
        system="You are a helper.",
        messages=[{"role": "user", "content": "Hello"}],
        output_schema=None,
    )

    assert result.data == {"text": "Hello world"}
    assert result.provider == "anthropic"


def test_anthropic_provider_uses_tool_choice_for_structured_output():
    """When output_schema is set, uses Anthropic tool-choice mechanism."""
    from job_finder.web.providers.anthropic_provider import AnthropicProvider

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = {"score": 80}
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    provider = AnthropicProvider(client=mock_client)
    provider.call(
        model="claude-sonnet-4-6",
        system="test",
        messages=[{"role": "user", "content": "test"}],
        output_schema=schema,
    )

    call_kwargs = mock_client.messages.create.call_args
    assert "tools" in call_kwargs.kwargs
    assert call_kwargs.kwargs["tools"][0]["name"] == "output"
    assert call_kwargs.kwargs["tool_choice"] == {"type": "tool", "name": "output"}


def test_anthropic_provider_cost_computation():
    """Cost is computed using MODEL_PRICING."""
    from job_finder.web.providers.anthropic_provider import AnthropicProvider

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = {"score": 80}
    mock_response.usage.input_tokens = 1_000_000  # 1M input tokens
    mock_response.usage.output_tokens = 0

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    provider = AnthropicProvider(client=mock_client)
    result = provider.call(
        model="claude-sonnet-4-6",
        system="test",
        messages=[{"role": "user", "content": "test"}],
    )
    # Sonnet input price is $3/M tokens
    assert abs(result.cost_usd - 3.0) < 0.01
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_anthropic_provider.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement AnthropicProvider**

Create `job_finder/web/providers/anthropic_provider.py`:

```python
"""Anthropic provider adapter.

Wraps the Anthropic messages API (client.messages.create) and translates
responses into ModelResult. Mirrors the existing call_claude() logic without
the cost recording or budget gating (those are handled by call_model()).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from job_finder.web.claude_client import MODEL_PRICING, compute_cost
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0


class AnthropicProvider(BaseProvider):
    """Adapter for Anthropic Claude API."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Call Anthropic messages API."""
        if self._client is None:
            raise ValueError("AnthropicProvider requires an Anthropic client")

        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        call_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "timeout": effective_timeout,
        }

        if output_schema is not None:
            call_kwargs["tools"] = [
                {
                    "name": "output",
                    "description": "Structured output",
                    "input_schema": output_schema,
                }
            ]
            call_kwargs["tool_choice"] = {"type": "tool", "name": "output"}

        response = self._client.messages.create(**call_kwargs)

        input_tokens: int = response.usage.input_tokens
        output_tokens: int = response.usage.output_tokens
        cost_usd = compute_cost(model, input_tokens, output_tokens)

        if not response.content:
            raise RuntimeError("Anthropic returned empty response content")

        content = response.content[0]
        if output_schema is not None and hasattr(content, "input"):
            data = content.input
        else:
            text = content.text if hasattr(content, "text") else str(content)
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, AttributeError):
                data = {"text": text}

        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="anthropic",
            schema_valid=True,  # Anthropic tool-choice is reliable
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_anthropic_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/providers/anthropic_provider.py tests/test_anthropic_provider.py
git commit -m "feat: add AnthropicProvider adapter"
```

---

### Task 7: Gemini adapter

**Files:**
- Create: `job_finder/web/providers/gemini_provider.py`
- Test: `tests/test_gemini_provider.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gemini_provider.py`:

```python
"""Tests for Gemini provider adapter."""

import json
from unittest.mock import MagicMock, patch

import pytest


def test_gemini_provider_call_returns_model_result():
    """GeminiProvider.call() returns well-formed ModelResult."""
    from job_finder.web.model_provider import ModelResult
    from job_finder.web.providers.gemini_provider import GeminiProvider

    mock_response = MagicMock()
    mock_response.text = json.dumps({"score": 75, "summary": "Good match"})
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50

    with patch("job_finder.web.providers.gemini_provider.genai") as mock_genai:
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response
        mock_genai.Client.return_value.models = mock_model

        provider = GeminiProvider(api_key="test-key")
        result = provider.call(
            model="gemini-2.0-flash",
            system="You are a helper.",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 75, "summary": "Good match"}
    assert result.provider == "gemini"
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_gemini_provider_structured_output_uses_json_mode():
    """When output_schema is set, uses response_mime_type and response_schema."""
    from job_finder.web.providers.gemini_provider import GeminiProvider

    mock_response = MagicMock()
    mock_response.text = json.dumps({"score": 80})
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50

    with patch("job_finder.web.providers.gemini_provider.genai") as mock_genai:
        mock_model = MagicMock()
        mock_model.generate_content.return_value = mock_response
        mock_genai.Client.return_value.models = mock_model

        schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
        provider = GeminiProvider(api_key="test-key")
        provider.call(
            model="gemini-2.0-flash",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            output_schema=schema,
        )

        call_args = mock_model.generate_content.call_args
        config = call_args.kwargs.get("config") or call_args[1].get("config")
        assert config.response_mime_type == "application/json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gemini_provider.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement GeminiProvider**

Create `job_finder/web/providers/gemini_provider.py`:

```python
"""Gemini provider adapter.

Uses the google-genai SDK to call Gemini models. Free tier: 15 RPM, 1M TPM.
Structured output via response_mime_type + response_schema.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from google import genai
from google.genai import types

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0
_MAX_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF_SECONDS = 5.0


class GeminiProvider(BaseProvider):
    """Adapter for Google Gemini API."""

    def __init__(self, api_key: str | None = None) -> None:
        if not api_key:
            raise ValueError(
                "GeminiProvider requires an API key. "
                "Set GEMINI_API_KEY env var or providers.gemini.api_key_env in config."
            )
        self._client = genai.Client(api_key=api_key)

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Call Gemini API."""
        # Build contents from messages (Gemini uses 'parts' format)
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg["content"])],
            ))

        # Build generation config
        gen_config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )

        if output_schema is not None:
            gen_config.response_mime_type = "application/json"
            gen_config.response_schema = output_schema

        # Call with rate limit retry
        response = self._call_with_retry(model, contents, gen_config)

        # Extract token counts
        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count or 0
        output_tokens = usage.candidates_token_count or 0

        # Parse response
        text = response.text or ""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, AttributeError):
            data = {"text": text}

        return ModelResult(
            data=data,
            cost_usd=0.0,  # Free tier
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="gemini",
            schema_valid=True,  # Validated by caller
        )

    def _call_with_retry(
        self,
        model: str,
        contents: list,
        config: types.GenerateContentConfig,
    ) -> Any:
        """Call Gemini API with rate limit retry (429 handling)."""
        for attempt in range(_MAX_RATE_LIMIT_RETRIES):
            try:
                return self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if "429" in str(exc) and attempt < _MAX_RATE_LIMIT_RETRIES - 1:
                    wait = _RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1)
                    logger.warning("Gemini rate limited, retrying in %.1fs", wait)
                    time.sleep(wait)
                    continue
                raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gemini_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/providers/gemini_provider.py tests/test_gemini_provider.py
git commit -m "feat: add GeminiProvider adapter"
```

---

### Task 8: Ollama adapter

**Files:**
- Create: `job_finder/web/providers/ollama_provider.py`
- Test: `tests/test_ollama_provider.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ollama_provider.py`:

```python
"""Tests for Ollama provider adapter."""

import json
from unittest.mock import MagicMock, patch

import pytest


def test_ollama_provider_call_returns_model_result():
    """OllamaProvider.call() returns well-formed ModelResult."""
    from job_finder.web.model_provider import ModelResult
    from job_finder.web.providers.ollama_provider import OllamaProvider

    mock_json_response = {
        "message": {"content": json.dumps({"score": 70, "summary": "Decent match"})},
        "prompt_eval_count": 100,
        "eval_count": 50,
    }

    with patch("job_finder.web.providers.ollama_provider.requests") as mock_requests:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_json_response
        mock_requests.post.return_value = mock_resp

        # Skip health check
        mock_health = MagicMock()
        mock_health.status_code = 200
        mock_requests.get.return_value = mock_health

        provider = OllamaProvider(base_url="http://localhost:11434")
        result = provider.call(
            model="qwen2.5:32b",
            system="You are a helper.",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert isinstance(result, ModelResult)
    assert result.data == {"score": 70, "summary": "Decent match"}
    assert result.provider == "ollama"
    assert result.cost_usd == 0.0
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_ollama_provider_embeds_schema_in_system_prompt():
    """When output_schema is set, embeds schema as instructions in system prompt."""
    from job_finder.web.providers.ollama_provider import OllamaProvider

    mock_json_response = {
        "message": {"content": json.dumps({"score": 80})},
        "prompt_eval_count": 100,
        "eval_count": 50,
    }

    with patch("job_finder.web.providers.ollama_provider.requests") as mock_requests:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_json_response
        mock_requests.post.return_value = mock_resp

        mock_health = MagicMock()
        mock_health.status_code = 200
        mock_requests.get.return_value = mock_health

        schema = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
        provider = OllamaProvider(base_url="http://localhost:11434")
        provider.call(
            model="qwen2.5:32b",
            system="You are a helper.",
            messages=[{"role": "user", "content": "test"}],
            output_schema=schema,
        )

        call_args = mock_requests.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert '"format": "json"' in json.dumps(body) or body.get("format") == "json"
        # Schema should appear in system message
        system_msg = [m for m in body["messages"] if m["role"] == "system"]
        assert len(system_msg) == 1
        assert "score" in system_msg[0]["content"]


def test_ollama_provider_health_check_fails():
    """OllamaProvider raises if Ollama is not running."""
    from job_finder.web.providers.ollama_provider import OllamaProvider

    with patch("job_finder.web.providers.ollama_provider.requests") as mock_requests:
        mock_requests.get.side_effect = ConnectionError("Connection refused")

        with pytest.raises(ConnectionError, match="Ollama"):
            OllamaProvider(base_url="http://localhost:11434")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ollama_provider.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement OllamaProvider**

Create `job_finder/web/providers/ollama_provider.py`:

```python
"""Ollama provider adapter.

Uses Ollama's native /api/chat endpoint via requests.
Structured output via "format": "json" + schema in system prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # Ollama can be slow on CPU


class OllamaProvider(BaseProvider):
    """Adapter for Ollama local models."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url.rstrip("/")
        self._health_check()

    def _health_check(self) -> None:
        """Verify Ollama is running."""
        try:
            resp = requests.get(f"{self._base_url}/api/tags", timeout=5)
            resp.raise_for_status()
        except Exception as exc:
            raise ConnectionError(
                f"Ollama not reachable at {self._base_url}. "
                f"Is Ollama running? Start it with: ollama serve"
            ) from exc

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Call Ollama /api/chat endpoint."""
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Build system prompt with optional schema instructions
        system_content = system
        if output_schema is not None:
            schema_str = json.dumps(output_schema, indent=2)
            system_content = (
                f"{system}\n\n"
                f"IMPORTANT: You MUST respond with valid JSON matching this schema:\n"
                f"```json\n{schema_str}\n```\n"
                f"Output ONLY the JSON object, no other text."
            )

        # Build Ollama messages format
        ollama_messages = [{"role": "system", "content": system_content}]
        for msg in messages:
            ollama_messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        body: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }

        if output_schema is not None:
            body["format"] = "json"

        resp = requests.post(
            f"{self._base_url}/api/chat",
            json=body,
            timeout=effective_timeout,
        )
        resp.raise_for_status()
        result = resp.json()

        # Parse response
        text = result.get("message", {}).get("content", "")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, AttributeError):
            data = {"text": text}

        input_tokens = result.get("prompt_eval_count", 0)
        output_tokens = result.get("eval_count", 0)

        return ModelResult(
            data=data,
            cost_usd=0.0,  # Local — always free
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="ollama",
            schema_valid=True,  # Validated by caller
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ollama_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/providers/ollama_provider.py tests/test_ollama_provider.py
git commit -m "feat: add OllamaProvider adapter"
```

---

## Chunk 3: Dispatcher — call_model() and Cost Tracking

### Task 9: call_model() dispatcher

This is the central function that replaces `call_claude()` at all call sites. It resolves config, gates budget, dispatches to the right adapter, validates output, handles retry/fallback, records costs, and returns `tuple[dict, float]`.

**Files:**
- Modify: `job_finder/web/model_provider.py`
- Test: `tests/test_model_provider.py`

- [ ] **Step 1: Write failing tests for call_model()**

Add to `tests/test_model_provider.py`:

```python
import json
import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch

from job_finder.web.model_provider import ModelResult


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear provider cache between tests to prevent cross-contamination."""
    from job_finder.web.model_provider import clear_provider_cache
    clear_provider_cache()
    yield
    clear_provider_cache()


@pytest.fixture
def test_db():
    """Temp DB with scoring_costs table including provider column."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE scoring_costs ("
        "id INTEGER PRIMARY KEY, job_id TEXT, purpose TEXT, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
        "timestamp TEXT, provider TEXT DEFAULT 'anthropic')"
    )
    conn.commit()
    yield path, conn
    conn.close()
    os.remove(path)


def test_call_model_anthropic_default(test_db):
    """call_model() routes to Anthropic when no providers config."""
    from job_finder.web.model_provider import call_model, ModelResult

    _path, conn = test_db
    config = {"scoring": {"models": {"sonnet": "claude-sonnet-4-6"}}}

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = {"score": 75, "summary": "Good"}
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    result, cost = call_model(
        model="sonnet",
        system="test",
        messages=[{"role": "user", "content": "test"}],
        conn=conn,
        config=config,
        client=mock_client,
        purpose="test_purpose",
    )

    assert result == {"score": 75, "summary": "Good"}
    assert cost > 0

    # Verify cost was recorded with provider column
    row = conn.execute("SELECT provider FROM scoring_costs").fetchone()
    assert row["provider"] == "anthropic"


def test_call_model_free_provider_bypasses_budget(test_db):
    """Free providers skip cost_gate even when Anthropic budget is exhausted."""
    from job_finder.web.model_provider import call_model

    _path, conn = test_db
    # Insert enough costs to exceed budget
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES ('x', 'x', 'x', 0, 0, 100.0, '2026-03-27T00:00:00Z', 'anthropic')"
    )
    conn.commit()

    config = {
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}, "monthly_budget_usd": 25.0},
        "providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}},
    }

    with patch("job_finder.web.model_provider._get_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.call.return_value = ModelResult(
            data={"score": 70}, cost_usd=0.0, input_tokens=100,
            output_tokens=50, model="gemini-2.0-flash", provider="gemini",
            schema_valid=True,
        )
        mock_get.return_value = mock_provider

        # Should NOT raise BudgetExceededError
        result, cost = call_model(
            model="sonnet",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            conn=conn,
            config=config,
            purpose="test",
        )
        assert result == {"score": 70}
        assert cost == 0.0


def test_call_model_schema_validation_retry(test_db):
    """call_model retries once on schema validation failure."""
    from job_finder.web.model_provider import call_model, ModelResult

    _path, conn = test_db
    config = {
        "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
        "providers": {"sonnet": {"provider": "gemini", "model": "gemini-2.0-flash"}},
    }

    # First call returns invalid schema, second returns valid
    bad_result = ModelResult(
        data={"wrong_field": "oops"}, cost_usd=0.0, input_tokens=50,
        output_tokens=20, model="gemini-2.0-flash", provider="gemini",
        schema_valid=True,
    )
    good_result = ModelResult(
        data={"score": 75, "summary": "Match"}, cost_usd=0.0, input_tokens=50,
        output_tokens=20, model="gemini-2.0-flash", provider="gemini",
        schema_valid=True,
    )

    with patch("job_finder.web.model_provider._get_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.call.side_effect = [bad_result, good_result]
        mock_get.return_value = mock_provider

        schema = {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "summary": {"type": "string"}},
            "required": ["score", "summary"],
        }

        result, cost = call_model(
            model="sonnet",
            system="test",
            messages=[{"role": "user", "content": "test"}],
            conn=conn,
            config=config,
            purpose="test",
            output_schema=schema,
        )
        assert result == {"score": 75, "summary": "Match"}
        assert mock_provider.call.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_provider.py::test_call_model_anthropic_default -v`
Expected: FAIL — `call_model` not defined.

- [ ] **Step 3: Implement call_model() and _get_provider()**

Add to `job_finder/web/model_provider.py`:

```python
import os
import sqlite3
from typing import Any

from job_finder.web.claude_client import (
    BudgetExceededError,
    ClaudeContext,
    cost_gate,
    record_cost,
)


# Provider instances cached per-config to avoid re-creating on every call
_provider_cache: dict[str, BaseProvider] = {}

# Free providers that bypass budget gating
_FREE_PROVIDERS = {"gemini", "ollama"}


def clear_provider_cache() -> None:
    """Clear the provider cache. Call from test fixtures to prevent cross-contamination."""
    _provider_cache.clear()


def _get_provider(
    provider_name: str,
    config: dict,
    client: Any | None = None,
) -> BaseProvider:
    """Get or create a provider adapter instance."""
    if provider_name == "anthropic":
        from job_finder.web.providers.anthropic_provider import AnthropicProvider
        # Anthropic provider uses the passed-in client (or creates one)
        if client is None:
            try:
                import anthropic
                client = anthropic.Anthropic()
            except ImportError:
                raise RuntimeError("anthropic package required for Anthropic provider")
        return AnthropicProvider(client=client)

    if provider_name == "gemini":
        cache_key = "gemini"
        if cache_key not in _provider_cache:
            from job_finder.web.providers.gemini_provider import GeminiProvider
            providers_cfg = config.get("providers", {})
            gemini_cfg = providers_cfg.get("gemini", {})
            api_key_env = gemini_cfg.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(api_key_env)
            _provider_cache[cache_key] = GeminiProvider(api_key=api_key)
        return _provider_cache[cache_key]

    if provider_name == "ollama":
        cache_key = "ollama"
        if cache_key not in _provider_cache:
            from job_finder.web.providers.ollama_provider import OllamaProvider
            providers_cfg = config.get("providers", {})
            ollama_cfg = providers_cfg.get("ollama", {})
            base_url = ollama_cfg.get("base_url", "http://localhost:11434")
            _provider_cache[cache_key] = OllamaProvider(base_url=base_url)
        return _provider_cache[cache_key]

    raise ValueError(f"Unknown provider: {provider_name}")


def call_model(
    model: str = "",
    system: str = "",
    messages: list[dict] | None = None,
    output_schema: dict | None = None,
    conn: sqlite3.Connection | None = None,
    job_id: str | None = None,
    purpose: str = "",
    config: dict | None = None,
    max_tokens: int = 1024,
    timeout: float | None = None,
    client: Any | None = None,
    *,
    ctx: ClaudeContext | None = None,
) -> tuple[dict, float]:
    """Route a model call to the configured provider.

    Drop-in replacement for call_claude(). Resolves the logical tier name
    (e.g. "sonnet") to a provider + model via config, dispatches to the
    adapter, validates structured output, retries once on failure, and
    optionally falls back to Anthropic.

    Returns:
        Tuple of (parsed_result: dict, cost_usd: float).

    Raises:
        BudgetExceededError: If cost_gate blocks an Anthropic call.
    """
    # Resolve context
    if ctx is not None:
        client = ctx.client
        conn = ctx.conn
        config = ctx.config

    if config is None:
        config = {}
    if conn is None:
        raise ValueError("call_model requires a database connection")

    # Resolve tier → provider + model
    tier = model  # "sonnet", "haiku", "opus"
    resolved = resolve_provider_config(tier, config)
    provider_name = resolved["provider"]
    model_id = resolved["model"]
    fallback = resolved["fallback"]

    # Budget gate: skip for free providers
    if provider_name not in _FREE_PROVIDERS:
        gate_tier = "haiku" if "haiku" in tier else "sonnet"
        if not cost_gate(conn, config, gate_tier):
            raise BudgetExceededError(
                f"Budget cap reached. {tier} calls paused. Model: {model_id}"
            )

    # Dispatch to provider
    provider = _get_provider(provider_name, config, client=client)
    result = provider.call(
        model=model_id,
        system=system,
        messages=messages or [],
        output_schema=output_schema,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    # Schema validation + retry
    if output_schema is not None:
        errors = validate_output(result.data, output_schema)
        if errors:
            logger.warning(
                "Schema validation failed for %s/%s (purpose=%s): %s. Retrying.",
                provider_name, model_id, purpose, errors[:3],
            )
            # Retry with error feedback
            retry_messages = list(messages or [])
            retry_messages.append({
                "role": "assistant",
                "content": json.dumps(result.data),
            })
            retry_messages.append({
                "role": "user",
                "content": (
                    f"Your previous response had schema errors: {errors}. "
                    f"Please output valid JSON matching the required schema."
                ),
            })
            result = provider.call(
                model=model_id,
                system=system,
                messages=retry_messages,
                output_schema=output_schema,
                max_tokens=max_tokens,
                timeout=timeout,
            )

            errors = validate_output(result.data, output_schema)
            if errors and fallback and fallback != provider_name:
                logger.warning(
                    "Retry failed for %s/%s. Falling back to %s.",
                    provider_name, model_id, fallback,
                )
                fallback_resolved = resolve_provider_config(tier, config)
                fallback_model = (
                    config.get("scoring", {}).get("models", {}).get(tier)
                    or _DEFAULT_MODELS.get(tier, DEFAULT_MODEL_SONNET)
                )
                fallback_provider = _get_provider(fallback, config, client=client)
                result = fallback_provider.call(
                    model=fallback_model,
                    system=system,
                    messages=messages or [],
                    output_schema=output_schema,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )

    # Record cost using the existing record_cost() with provider param.
    # For Anthropic, record_cost() recomputes cost from MODEL_PRICING (authoritative).
    # For free providers, pass cost_usd=0.0 directly — they have no pricing table entry.
    if result.provider in _FREE_PROVIDERS:
        # Free provider: record tokens for eval but cost is $0
        from job_finder.json_utils import utc_now_iso
        timestamp = utc_now_iso()
        conn.execute(
            "INSERT INTO scoring_costs "
            "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, purpose, result.model, result.input_tokens, result.output_tokens,
             0.0, timestamp, result.provider),
        )
        conn.commit()
        cost_usd = 0.0
    else:
        # Anthropic: use existing record_cost() which computes cost from MODEL_PRICING
        cost_usd = record_cost(
            conn, job_id, purpose, result.model,
            result.input_tokens, result.output_tokens, provider=result.provider,
        )

    return result.data, cost_usd
```

Also add the missing `import json` at the top of the file if not already there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model_provider.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `uv run pytest tests/ -x --timeout=60`
Expected: All existing tests still pass (no changes to existing code yet).

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/model_provider.py tests/test_model_provider.py
git commit -m "feat: add call_model() dispatcher with retry, fallback, and cost recording"
```

---

### Task 10: Update record_cost() to accept provider parameter

The existing `record_cost()` in `claude_client.py` needs to accept an optional `provider` parameter so `call_claude()` callers that haven't migrated yet still work.

**Files:**
- Modify: `job_finder/web/claude_client.py` (lines 99-128)
- Test: existing tests via `uv run pytest tests/ -x`

- [ ] **Step 1: Update record_cost() signature**

In `job_finder/web/claude_client.py`, modify `record_cost()` to accept an optional `provider` parameter:

Change the function signature from:
```python
def record_cost(
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
```

To:
```python
def record_cost(
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    provider: str = "anthropic",
) -> float:
```

And update the INSERT statement from:
```python
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp),
    )
```

To:
```python
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider),
    )
```

- [ ] **Step 2: Run tests to verify nothing breaks**

Run: `uv run pytest tests/ -x --timeout=60`
Expected: All pass. The default `provider="anthropic"` preserves existing behavior.

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/claude_client.py
git commit -m "feat: add provider parameter to record_cost() (default 'anthropic')"
```

---

### Task 11: Update cost stats for provider grouping

**Files:**
- Modify: `job_finder/web/claude_client.py` (get_cost_stats function)
- Test: `tests/test_claude_client.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_claude_client.py` (or the appropriate cost stats test file):

```python
def test_get_cost_stats_includes_by_provider(migrated_db):
    """get_cost_stats returns by_provider breakdown."""
    _path, conn = migrated_db
    from job_finder.web.claude_client import get_cost_stats, record_cost
    from job_finder.json_utils import utc_now_iso

    # Anthropic call — use record_cost which computes pricing from MODEL_PRICING
    record_cost(conn, "job-1", "sonnet_eval", "claude-sonnet-4-6", 1000, 500, "anthropic")

    # Gemini call — insert directly since record_cost doesn't know Gemini pricing
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("job-2", "sonnet_eval", "gemini-2.0-flash", 1000, 500, 0.0, utc_now_iso(), "gemini"),
    )
    conn.commit()

    stats = get_cost_stats(conn)
    assert "by_provider" in stats
    providers = {p["provider"]: p["cost"] for p in stats["by_provider"]}
    assert "anthropic" in providers
    assert "gemini" in providers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude_client.py::test_get_cost_stats_includes_by_provider -v`
Expected: FAIL — `by_provider` key missing.

- [ ] **Step 3: Add by_provider grouping to get_cost_stats()**

In `job_finder/web/claude_client.py`, in the `get_cost_stats()` function, add a `by_provider` query after the existing `by_feature` query. Find where `by_feature` is computed and add:

```python
    # By provider breakdown
    by_provider_rows = conn.execute(
        "SELECT COALESCE(provider, 'anthropic') as provider, SUM(cost_usd) as cost "
        "FROM scoring_costs WHERE timestamp >= ? GROUP BY provider ORDER BY cost DESC",
        (month_start,),
    ).fetchall()
    by_provider = [{"provider": row[0], "cost": row[1]} for row in by_provider_rows]
```

Add `"by_provider": by_provider` to the returned dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_claude_client.py::test_get_cost_stats_includes_by_provider -v`
Expected: PASS.

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -x --timeout=60`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/claude_client.py tests/test_claude_client.py
git commit -m "feat: add by_provider cost breakdown to get_cost_stats()"
```

---

## Chunk 4: Caller Migration

### Task 12: Migrate core scoring callers (haiku_scorer, sonnet_evaluator)

These are the highest-traffic callers. Each migration follows the same pattern:
1. Change `from job_finder.web.claude_client import call_claude` → `from job_finder.web.model_provider import call_model`
2. Change `call_claude(model=model, ...)` → `call_model(model="<tier>", ...)`
3. Remove the `config.get("scoring", {}).get("models", {}).get(...)` model resolution (call_model handles it)
4. Keep `ctx=` parameter passing (call_model supports it)

**Files:**
- Modify: `job_finder/web/haiku_scorer.py`
- Modify: `job_finder/web/sonnet_evaluator.py`
- Test: existing tests via `uv run pytest tests/test_haiku_scorer.py tests/test_sonnet_evaluator.py -v`

- [ ] **Step 1: Migrate haiku_scorer.py**

In `job_finder/web/haiku_scorer.py`:

1. Change import (around line 24):
   - From: `from job_finder.web.claude_client import call_claude, ClaudeContext, BudgetExceededError`
   - To: `from job_finder.web.claude_client import ClaudeContext, BudgetExceededError`
   and add: `from job_finder.web.model_provider import call_model`

2. In `score_job_haiku()`, remove the model resolution block (around lines 273-278):
   ```python
   # DELETE these lines:
   model = (
       config.get("scoring", {})
       .get("models", {})
       .get("haiku", DEFAULT_MODEL_HAIKU)
   )
   ```

3. Change the call_claude invocation (around line 283):
   - From:
     ```python
     result, cost_usd = call_claude(
         model=model,
         ...
         ctx=ctx or ClaudeContext(client=client, conn=conn, config=config),
     )
     ```
   - To:
     ```python
     result, cost_usd = call_model(
         model="haiku",
         ...
         ctx=ctx or ClaudeContext(client=client, conn=conn, config=config),
     )
     ```

- [ ] **Step 2: Migrate sonnet_evaluator.py**

Same pattern as haiku_scorer.py:

1. Change import: add `from job_finder.web.model_provider import call_model`
2. Remove model resolution block (around lines 140-143)
3. Change `call_claude(model=model, ...)` → `call_model(model="sonnet", ...)`

- [ ] **Step 3: Update test mocks**

In test files that mock `call_claude` for haiku/sonnet, update the mock target:
- From: `@patch("job_finder.web.haiku_scorer.call_claude")`
- To: `@patch("job_finder.web.haiku_scorer.call_model")`

Same for sonnet_evaluator tests.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_haiku_scorer.py tests/test_sonnet_evaluator.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/haiku_scorer.py job_finder/web/sonnet_evaluator.py tests/
git commit -m "feat: migrate haiku_scorer and sonnet_evaluator to call_model()"
```

---

### Task 13: Migrate remaining call_claude() callers

Mechanical migration of all other files that call `call_claude()`. Same pattern as Task 12.

**Files to migrate (each follows the same 3-step pattern):**

| File | Tier | Remove model resolution? | Notes |
|------|------|-------------------------|-------|
| `enrichment_tiers.py` | haiku + sonnet | Yes (lines 293-296, 466-469) | Two call sites |
| `resume_generator.py` | sonnet | Yes (lines 271-274) | |
| `resume_multi_version.py` | haiku + sonnet | Yes (lines 46-49, 160-163, 360-363) | Three call_claude + three anthropic.Anthropic() |
| `resume_feedback.py` | sonnet | No (uses hardcoded DEFAULT_MODEL_SONNET) | Also has anthropic.Anthropic() |
| `interview_prep.py` | opus | Yes (lines 229-230) | Also has anthropic.Anthropic() |
| `rejection_analyzer.py` | opus | Yes (lines 188-191) | Also has anthropic.Anthropic() |
| `resume_validator.py` | sonnet | Yes (lines 167-170, 246-249) | Two call sites + two anthropic.Anthropic() |
| `resume_style_guide.py` | sonnet | Yes (lines 163-166) | Three call sites + two anthropic.Anthropic() |
| `description_reformatter.py` | haiku | Yes (lines 89-92) | Also has anthropic.Anthropic() in background runner |
| `careers_scraper.py` | haiku | No (uses hardcoded DEFAULT_MODEL_HAIKU) | Two call sites |
| `blueprints/resume_review.py` | haiku | Yes (lines 158-161) | Also has anthropic.Anthropic() |
| `blueprints/profile_recommendations.py` | haiku | Yes (lines 104-107) | Two call sites + two anthropic.Anthropic() |
| `scoring_evaluator.py` (root) | opus | Check actual usage | Also has anthropic.Anthropic() |

For each file:

- [ ] **Step 1: Change import** — add `from job_finder.web.model_provider import call_model`, remove `call_claude` from the claude_client import if it was the only thing imported from there.

- [ ] **Step 2: Replace call_claude() calls** — change `call_claude(model=model, ...)` → `call_model(model="<tier>", ...)`. Remove model resolution blocks that read from `config.get("scoring", {}).get("models", {})`.

- [ ] **Step 3: Replace anthropic.Anthropic() calls** — for files that create `anthropic.Anthropic()` just to pass to `call_claude()`, remove the client creation. `call_model()` handles client creation internally via the adapter. If the client is used for other purposes (not just AI calls), keep it but don't pass it to `call_model()`.

- [ ] **Step 4: Update corresponding test mocks** — change `@patch("...call_claude")` → `@patch("...call_model")`.

- [ ] **Step 5: Run tests for each migrated file**

Run: `uv run pytest tests/ -x --timeout=120`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add job_finder/ tests/ scoring_evaluator.py
git commit -m "feat: migrate all remaining callers from call_claude() to call_model()"
```

---

### Task 14: Migrate direct anthropic.Anthropic() in blueprints/orchestrators

Files that create `anthropic.Anthropic()` and pass the client to functions that now use `call_model()` internally. These files need their client creation removed since the provider layer handles it.

**Files:**
- `job_finder/web/scoring_runner.py` — creates `anthropic.Anthropic()` at line 66, passes to scorer functions
- `job_finder/web/blueprints/guidelines.py` — creates `anthropic.Anthropic()` at line 88
- `job_finder/web/blueprints/resume.py` — imports anthropic, calls `_generate_resume_background()`

For each:

- [ ] **Step 1: Remove client creation** — delete `client = anthropic.Anthropic()` lines and `import anthropic` if no longer used.

- [ ] **Step 2: Update function calls** — functions that previously received a `client` parameter now don't need it (call_model handles it). Remove the `client=client` argument from calls to migrated functions.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/ -x --timeout=120`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/scoring_runner.py job_finder/web/blueprints/
git commit -m "feat: remove direct anthropic.Anthropic() calls from orchestrators"
```

---

### Task 15: Update config.example.yaml

**Files:**
- Modify: `config.example.yaml`

- [ ] **Step 1: Add providers section to config.example.yaml**

After the `scoring:` section, add:

```yaml
# --- Provider routing (optional) ---
# Route model tiers to alternative providers. Omit this section entirely
# to use Anthropic for everything (default behavior).
#
# providers:
#   # Per-tier routing: which provider handles each model tier
#   sonnet:
#     provider: gemini          # "anthropic" | "gemini" | "ollama"
#     model: gemini-2.0-flash   # provider-specific model name
#     fallback: anthropic       # optional: fall back on failure
#   haiku:
#     provider: anthropic       # keep Haiku on Anthropic (already cheap)
#   opus:
#     provider: anthropic
#
#   # Provider connection settings
#   gemini:
#     api_key_env: GEMINI_API_KEY  # env var name containing API key
#   ollama:
#     base_url: http://localhost:11434
```

- [ ] **Step 2: Commit**

```bash
git add config.example.yaml
git commit -m "docs: add providers section to config.example.yaml"
```

---

## Chunk 5: Evaluation Framework

### Task 16: Job sampling and prompt reconstruction

The eval framework needs to sample jobs from the DB that have existing Sonnet results, then reconstruct the same prompts that were originally sent.

**Prerequisite note:** The spec calls for extracting prompt-building functions from scorer modules as separable units. For v1 of the eval framework, `benchmark.py` reconstructs the Sonnet eval prompt by importing the system prompt constant and rebuilding the user message from job data (mirrors `sonnet_evaluator.py` logic). This means prompt format changes in `sonnet_evaluator.py` must be reflected in `benchmark.py`. A future improvement is to extract `build_sonnet_prompt()` as a public function in `sonnet_evaluator.py` and import it from both places.

**Files:**
- Create: `job_finder/eval/__init__.py`
- Create: `job_finder/eval/sample.py`
- Test: `tests/test_eval_sample.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_sample.py`:

```python
"""Tests for evaluation framework job sampling."""

import sqlite3
import tempfile
import os

import pytest


@pytest.fixture
def eval_db():
    """DB with scored jobs for benchmarking."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from job_finder.web.db_migrate import run_migrations
    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Insert sample jobs with Sonnet scores
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description, jd_full, "
            "haiku_score, sonnet_score, fit_analysis, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"job-{i}", f"Engineer {i}", f"Company {i}", "Remote",
                f"Description {i}", f"Full JD for job {i}",
                60 + i, 70 + i, '{"strengths": ["a"], "gaps": ["b"]}', "test",
            ),
        )
    conn.commit()
    yield path, conn
    conn.close()
    os.remove(path)


def test_sample_scored_jobs(eval_db):
    """sample_scored_jobs returns N jobs that have sonnet_score."""
    from job_finder.eval.sample import sample_scored_jobs

    _path, conn = eval_db
    jobs = sample_scored_jobs(conn, purpose="sonnet_eval", n=3)
    assert len(jobs) == 3
    assert all(job["sonnet_score"] is not None for job in jobs)


def test_sample_scored_jobs_insufficient():
    """Returns all available when fewer than N exist."""
    from job_finder.eval.sample import sample_scored_jobs

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from job_finder.web.db_migrate import run_migrations
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, description, "
        "sonnet_score, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("job-0", "Eng", "Co", "Remote", "Desc", 75, "test"),
    )
    conn.commit()

    jobs = sample_scored_jobs(conn, purpose="sonnet_eval", n=10)
    assert len(jobs) == 1

    conn.close()
    os.remove(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_sample.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create eval package and sample module**

Create `job_finder/eval/__init__.py` (empty):
```python
```

Create `job_finder/eval/sample.py`:

```python
"""Job sampling for evaluation benchmarks.

Samples jobs from the database that already have Sonnet results,
and provides prompt reconstruction for re-running with candidate models.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any


def sample_scored_jobs(
    conn: sqlite3.Connection,
    purpose: str = "sonnet_eval",
    n: int = 30,
) -> list[dict]:
    """Sample N jobs that have existing Sonnet evaluation results.

    Returns jobs as dicts with all columns needed for prompt reconstruction.
    Samples randomly to avoid bias from recent/early jobs.
    """
    # Sample jobs that have existing Sonnet evaluation results.
    # All purposes use sonnet_score as the ground-truth indicator since
    # the benchmark compares candidate output against Sonnet's output.
    rows = conn.execute(
        "SELECT * FROM jobs WHERE sonnet_score IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()

    return [dict(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_sample.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/eval/__init__.py job_finder/eval/sample.py tests/test_eval_sample.py
git commit -m "feat: add eval sampling module for benchmark job selection"
```

---

### Task 17: Metrics computation

**Files:**
- Create: `job_finder/eval/compare.py`
- Test: `tests/test_eval_compare.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_compare.py`:

```python
"""Tests for evaluation metrics computation."""

import pytest


def test_compute_score_metrics():
    """Computes correlation and delta metrics from score pairs."""
    from job_finder.eval.compare import compute_score_metrics

    reference_scores = [80, 70, 60, 90, 50]
    candidate_scores = [78, 72, 58, 88, 52]

    metrics = compute_score_metrics(reference_scores, candidate_scores)

    assert "mean_delta" in metrics
    assert "median_delta" in metrics
    assert "std_delta" in metrics
    assert "correlation" in metrics
    assert "rank_agreement" in metrics
    assert abs(metrics["mean_delta"]) < 5  # Small deltas expected
    assert metrics["correlation"] > 0.9  # Highly correlated scores


def test_compute_score_metrics_identical():
    """Identical scores produce perfect metrics."""
    from job_finder.eval.compare import compute_score_metrics

    scores = [80, 70, 60, 90, 50]
    metrics = compute_score_metrics(scores, scores)

    assert metrics["mean_delta"] == 0.0
    assert metrics["correlation"] == pytest.approx(1.0, abs=0.01)
    assert metrics["rank_agreement"] == pytest.approx(1.0, abs=0.01)


def test_compute_threshold_agreement():
    """Computes pass/fail agreement at a given threshold."""
    from job_finder.eval.compare import compute_threshold_agreement

    ref = [80, 40, 60, 30, 70]
    cand = [78, 42, 58, 32, 68]
    # At threshold 55: ref passes [80,60,70], cand passes [78,58,68] — same jobs pass
    agreement = compute_threshold_agreement(ref, cand, threshold=55)
    assert agreement == 1.0  # Perfect agreement


def test_compute_schema_metrics():
    """Computes schema adherence and retry rates."""
    from job_finder.eval.compare import compute_schema_metrics

    results = [
        {"schema_valid": True, "retried": False, "fell_back": False},
        {"schema_valid": True, "retried": True, "fell_back": False},
        {"schema_valid": False, "retried": True, "fell_back": True},
        {"schema_valid": True, "retried": False, "fell_back": False},
    ]
    metrics = compute_schema_metrics(results)
    assert metrics["adherence_rate"] == 0.75
    assert metrics["retry_rate"] == 0.5
    assert metrics["fallback_rate"] == 0.25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_compare.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement compare.py**

Create `job_finder/eval/compare.py`:

```python
"""Metrics computation for model evaluation benchmarks."""

from __future__ import annotations

import statistics

from scipy import stats


def compute_score_metrics(
    reference: list[int | float],
    candidate: list[int | float],
) -> dict:
    """Compute score correlation and delta metrics."""
    if len(reference) < 2:
        return {
            "mean_delta": 0.0,
            "median_delta": 0.0,
            "std_delta": 0.0,
            "correlation": 0.0,
            "rank_agreement": 0.0,
        }

    deltas = [c - r for r, c in zip(reference, candidate)]

    pearson_r, _ = stats.pearsonr(reference, candidate)
    spearman_rho, _ = stats.spearmanr(reference, candidate)

    return {
        "mean_delta": statistics.mean(deltas),
        "median_delta": statistics.median(deltas),
        "std_delta": statistics.stdev(deltas) if len(deltas) > 1 else 0.0,
        "correlation": float(pearson_r),
        "rank_agreement": float(spearman_rho),
    }


def compute_threshold_agreement(
    reference: list[int | float],
    candidate: list[int | float],
    threshold: int = 55,
) -> float:
    """Compute % of jobs where ref and candidate agree on pass/fail."""
    if not reference:
        return 0.0

    agreements = sum(
        1 for r, c in zip(reference, candidate)
        if (r >= threshold) == (c >= threshold)
    )
    return agreements / len(reference)


def compute_schema_metrics(results: list[dict]) -> dict:
    """Compute schema adherence, retry, and fallback rates."""
    if not results:
        return {"adherence_rate": 0.0, "retry_rate": 0.0, "fallback_rate": 0.0}

    n = len(results)
    return {
        "adherence_rate": sum(1 for r in results if r["schema_valid"]) / n,
        "retry_rate": sum(1 for r in results if r["retried"]) / n,
        "fallback_rate": sum(1 for r in results if r["fell_back"]) / n,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_compare.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/eval/compare.py tests/test_eval_compare.py
git commit -m "feat: add evaluation metrics computation (correlation, schema adherence)"
```

---

### Task 18: Report generation and verdict

**Files:**
- Create: `job_finder/eval/report.py`
- Test: `tests/test_eval_report.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_report.py`:

```python
"""Tests for evaluation report generation."""

import pytest


def test_compute_verdict_suitable():
    """High metrics produce SUITABLE verdict."""
    from job_finder.eval.report import compute_verdict

    metrics = {
        "scores": {"correlation": 0.92, "rank_agreement": 0.88},
        "schema": {"adherence_rate": 0.97, "fallback_rate": 0.03},
    }
    verdict = compute_verdict(metrics)
    assert verdict["recommendation"] == "SUITABLE"
    assert len(verdict["reasons"]) > 0


def test_compute_verdict_marginal():
    """Mid-range metrics produce MARGINAL verdict."""
    from job_finder.eval.report import compute_verdict

    metrics = {
        "scores": {"correlation": 0.78, "rank_agreement": 0.72},
        "schema": {"adherence_rate": 0.82, "fallback_rate": 0.15},
    }
    verdict = compute_verdict(metrics)
    assert verdict["recommendation"] == "MARGINAL"


def test_compute_verdict_not_recommended():
    """Low metrics produce NOT_RECOMMENDED verdict."""
    from job_finder.eval.report import compute_verdict

    metrics = {
        "scores": {"correlation": 0.55, "rank_agreement": 0.50},
        "schema": {"adherence_rate": 0.60, "fallback_rate": 0.35},
    }
    verdict = compute_verdict(metrics)
    assert verdict["recommendation"] == "NOT_RECOMMENDED"


def test_build_report_structure():
    """build_report produces a complete report dict."""
    from job_finder.eval.report import build_report

    report = build_report(
        reference_model="claude-sonnet-4-6",
        candidate_model="gemini-2.0-flash",
        purpose="sonnet_eval",
        sample_size=10,
        score_metrics={"mean_delta": -2.0, "correlation": 0.90, "rank_agreement": 0.85,
                       "median_delta": -1.5, "std_delta": 3.0},
        schema_metrics={"adherence_rate": 0.95, "retry_rate": 0.10, "fallback_rate": 0.05},
        quality_metrics={"avg_summary_length": {"ref": 150, "cand": 140}},
        performance_metrics={"avg_latency_ms": {"ref": 2300, "cand": 1800}},
        per_job_results=[],
    )

    assert "benchmark" in report
    assert report["benchmark"]["reference"] == "claude-sonnet-4-6"
    assert report["benchmark"]["candidate"] == "gemini-2.0-flash"
    assert "scores" in report
    assert "schema" in report
    assert "verdict" in report
    assert report["verdict"]["recommendation"] in ("SUITABLE", "MARGINAL", "NOT_RECOMMENDED")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_report.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement report.py**

Create `job_finder/eval/report.py`:

```python
"""Evaluation report generation and verdict computation."""

from __future__ import annotations

from datetime import datetime, timezone

# Verdict thresholds (configurable defaults)
_SUITABLE = {"correlation": 0.85, "schema": 0.90, "rank": 0.80, "fallback": 0.10}
_MARGINAL = {"correlation": 0.70, "schema": 0.75, "rank": 0.65, "fallback": 0.25}


def compute_verdict(metrics: dict) -> dict:
    """Compute recommendation verdict from metrics."""
    corr = metrics["scores"]["correlation"]
    rank = metrics["scores"]["rank_agreement"]
    schema = metrics["schema"]["adherence_rate"]
    fallback = metrics["schema"]["fallback_rate"]

    reasons = []
    warnings = []

    # Check SUITABLE thresholds
    suitable = True
    if corr >= _SUITABLE["correlation"]:
        reasons.append(f"Score correlation {corr:.2f} exceeds {_SUITABLE['correlation']} threshold")
    else:
        suitable = False
        warnings.append(f"Score correlation {corr:.2f} below {_SUITABLE['correlation']} threshold")

    if schema >= _SUITABLE["schema"]:
        reasons.append(f"Schema adherence {schema:.0%} exceeds {_SUITABLE['schema']:.0%} threshold")
    else:
        suitable = False
        warnings.append(f"Schema adherence {schema:.0%} below {_SUITABLE['schema']:.0%} threshold")

    if rank >= _SUITABLE["rank"]:
        reasons.append(f"Rank agreement {rank:.2f} exceeds {_SUITABLE['rank']} threshold")
    else:
        suitable = False
        warnings.append(f"Rank agreement {rank:.2f} below {_SUITABLE['rank']} threshold")

    if fallback <= _SUITABLE["fallback"]:
        reasons.append(f"Fallback rate {fallback:.0%} within {_SUITABLE['fallback']:.0%} threshold")
    else:
        suitable = False
        warnings.append(f"Fallback rate {fallback:.0%} exceeds {_SUITABLE['fallback']:.0%} threshold")

    if suitable:
        return {"recommendation": "SUITABLE", "reasons": reasons, "warnings": warnings}

    # Check MARGINAL thresholds
    marginal = (
        corr >= _MARGINAL["correlation"]
        and schema >= _MARGINAL["schema"]
        and rank >= _MARGINAL["rank"]
        and fallback <= _MARGINAL["fallback"]
    )

    if marginal:
        return {"recommendation": "MARGINAL", "reasons": reasons, "warnings": warnings}

    return {"recommendation": "NOT_RECOMMENDED", "reasons": reasons, "warnings": warnings}


def build_report(
    reference_model: str,
    candidate_model: str,
    purpose: str,
    sample_size: int,
    score_metrics: dict,
    schema_metrics: dict,
    quality_metrics: dict,
    performance_metrics: dict,
    per_job_results: list[dict],
) -> dict:
    """Build a complete benchmark report."""
    metrics = {"scores": score_metrics, "schema": schema_metrics}
    verdict = compute_verdict(metrics)

    return {
        "benchmark": {
            "date": datetime.now(timezone.utc).isoformat(),
            "reference": reference_model,
            "candidate": candidate_model,
            "purpose": purpose,
            "sample_size": sample_size,
        },
        "scores": score_metrics,
        "schema": schema_metrics,
        "quality": quality_metrics,
        "performance": performance_metrics,
        "verdict": verdict,
        "per_job": per_job_results,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_report.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/eval/report.py tests/test_eval_report.py
git commit -m "feat: add evaluation report generation with SUITABLE/MARGINAL/NOT_RECOMMENDED verdicts"
```

---

### Task 19: Benchmark CLI entry point

**Files:**
- Create: `job_finder/eval/benchmark.py`
- Test: `tests/test_eval_benchmark.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_benchmark.py`:

```python
"""Tests for benchmark CLI orchestrator."""

import json
import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def benchmark_db():
    """DB with scored jobs and scoring_costs for benchmarking."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from job_finder.web.db_migrate import run_migrations
    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description, jd_full, "
            "haiku_score, sonnet_score, fit_analysis, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"job-{i}", f"Engineer {i}", f"Company {i}", "Remote",
                f"Short desc {i}", f"Full detailed JD for engineer position {i}",
                60 + i, 70 + i,
                json.dumps({"strengths": ["a"], "gaps": ["b"], "talking_points": ["c"], "resume_priority_skills": ["d"]}),
                "test",
            ),
        )
    conn.commit()
    yield path, conn
    conn.close()
    os.remove(path)


def test_run_benchmark_produces_report(benchmark_db, tmp_path):
    """run_benchmark orchestrates sampling, calling, comparing, and returns a report."""
    from job_finder.eval.benchmark import run_benchmark
    from job_finder.web.model_provider import ModelResult

    db_path, conn = benchmark_db

    # Mock the candidate provider
    with patch("job_finder.eval.benchmark._call_candidate") as mock_call:
        mock_call.return_value = ModelResult(
            data={"score": 72, "summary": "Good match", "fit_analysis": {
                "strengths": ["x"], "gaps": ["y"],
                "talking_points": ["z"], "resume_priority_skills": ["w"],
            }},
            cost_usd=0.0, input_tokens=100, output_tokens=50,
            model="gemini-2.0-flash", provider="gemini", schema_valid=True,
        )

        report = run_benchmark(
            db_path=db_path,
            candidate="gemini:gemini-2.0-flash",
            purpose="sonnet_eval",
            sample_size=3,
            config={"profile": {"target_titles": ["Engineer"], "target_locations": ["Remote"],
                                "skills": [], "min_salary": None, "industries": []},
                    "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}, "haiku_threshold": 55},
                    "sources": {}, "db": {"path": db_path}},
            output_dir=str(tmp_path),
        )

    assert report["benchmark"]["candidate"] == "gemini-2.0-flash"
    assert report["verdict"]["recommendation"] in ("SUITABLE", "MARGINAL", "NOT_RECOMMENDED")
    assert len(report["per_job"]) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_eval_benchmark.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement benchmark.py**

Create `job_finder/eval/benchmark.py`:

```python
"""Evaluation benchmark CLI and orchestrator.

Usage:
    uv run python -m job_finder.eval.benchmark \\
        --candidate gemini:gemini-2.0-flash \\
        --purpose sonnet_eval \\
        --sample 30
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from job_finder.eval.compare import (
    compute_schema_metrics,
    compute_score_metrics,
    compute_threshold_agreement,
)
from job_finder.eval.report import build_report
from job_finder.eval.sample import sample_scored_jobs
from job_finder.web.model_provider import ModelResult, _get_provider, validate_output

logger = logging.getLogger(__name__)


def _call_candidate(
    provider_name: str,
    model: str,
    system: str,
    messages: list[dict],
    output_schema: dict | None,
    config: dict,
) -> ModelResult:
    """Call a candidate model via its provider adapter."""
    provider = _get_provider(provider_name, config)
    return provider.call(
        model=model,
        system=system,
        messages=messages,
        output_schema=output_schema,
    )


def run_benchmark(
    db_path: str,
    candidate: str,
    purpose: str,
    sample_size: int,
    config: dict,
    output_dir: str = "eval_results",
) -> dict:
    """Run an evaluation benchmark.

    Args:
        db_path: Path to the SQLite database.
        candidate: "provider:model" string (e.g., "gemini:gemini-2.0-flash").
        purpose: Purpose label to benchmark (e.g., "sonnet_eval").
        sample_size: Number of jobs to sample.
        config: Application config dict.
        output_dir: Directory for output reports.

    Returns:
        Benchmark report dict.
    """
    provider_name, model = candidate.split(":", 1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # 1. Sample jobs
        jobs = sample_scored_jobs(conn, purpose=purpose, n=sample_size)
        if not jobs:
            raise ValueError(f"No jobs with Sonnet scores found for purpose '{purpose}'")

        logger.info("Sampled %d jobs for benchmark", len(jobs))

        # 2. Run candidate on each job and collect results
        reference_scores = []
        candidate_scores = []
        per_job_results = []
        schema_results = []
        latencies = []

        # Import schema and prompt builder based on purpose
        from job_finder.web.sonnet_evaluator import SONNET_SCHEMA, _SYSTEM_PROMPT

        experience_profile = _load_experience_profile(config)

        for job in jobs:
            ref_score = job.get("sonnet_score", 0)
            reference_scores.append(ref_score)

            # Build the same prompt that would have been sent
            system = _SYSTEM_PROMPT
            messages = _build_sonnet_eval_messages(job, experience_profile, config)

            # Call candidate
            start = time.time()
            try:
                result = _call_candidate(
                    provider_name=provider_name,
                    model=model,
                    system=system,
                    messages=messages,
                    output_schema=SONNET_SCHEMA,
                    config=config,
                )
                latency_ms = (time.time() - start) * 1000

                # Validate schema
                errors = validate_output(result.data, SONNET_SCHEMA)
                schema_valid = len(errors) == 0
                retried = False
                fell_back = False

                cand_score = result.data.get("score", 0)
                candidate_scores.append(cand_score)
                latencies.append(latency_ms)

                per_job_results.append({
                    "job_id": job.get("dedup_key"),
                    "reference_score": ref_score,
                    "candidate_score": cand_score,
                    "delta": cand_score - ref_score,
                    "schema_valid": schema_valid,
                    "latency_ms": round(latency_ms),
                })
                schema_results.append({
                    "schema_valid": schema_valid,
                    "retried": retried,
                    "fell_back": fell_back,
                })

            except Exception as exc:
                logger.warning("Candidate failed on job %s: %s", job.get("dedup_key"), exc)
                candidate_scores.append(0)
                schema_results.append({"schema_valid": False, "retried": False, "fell_back": False})
                per_job_results.append({
                    "job_id": job.get("dedup_key"),
                    "reference_score": ref_score,
                    "candidate_score": 0,
                    "error": str(exc),
                })

        # 3. Compute metrics
        score_metrics = compute_score_metrics(reference_scores, candidate_scores)
        haiku_threshold = config.get("scoring", {}).get("haiku_threshold", 55)
        score_metrics["threshold_agreement"] = compute_threshold_agreement(
            reference_scores, candidate_scores, threshold=haiku_threshold,
        )
        schema_metrics = compute_schema_metrics(schema_results)

        # Quality metrics (summary length, fit analysis depth)
        quality_metrics = _compute_quality_metrics(per_job_results)

        # Performance metrics
        performance_metrics = {
            "avg_latency_ms": {
                "cand": round(sum(latencies) / len(latencies)) if latencies else 0,
            },
            "total_cost": {"ref": 0.0, "cand": 0.0},  # TODO: compute from DB
        }

        # 4. Build report
        report = build_report(
            reference_model=config.get("scoring", {}).get("models", {}).get("sonnet", "claude-sonnet-4-6"),
            candidate_model=model,
            purpose=purpose,
            sample_size=len(jobs),
            score_metrics=score_metrics,
            schema_metrics=schema_metrics,
            quality_metrics=quality_metrics,
            performance_metrics=performance_metrics,
            per_job_results=per_job_results,
        )

        # 5. Save report
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        safe_model = model.replace("/", "-").replace(":", "-")
        filename = f"{Path(output_dir)}/{report['benchmark']['date'][:10]}_{safe_model}_{purpose}.json"
        with open(filename, "w") as f:
            json.dump(report, f, indent=2)

        logger.info("Report saved to %s", filename)
        logger.info("Verdict: %s", report["verdict"]["recommendation"])

        return report

    finally:
        conn.close()


def _load_experience_profile(config: dict) -> dict:
    """Load the experience profile JSON."""
    from job_finder.config import DEFAULT_PROFILE_PATH

    profile_path = config.get("profile", {}).get("profile_path", DEFAULT_PROFILE_PATH)
    try:
        with open(profile_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": [], "skills": [], "education": []}


def _build_sonnet_eval_messages(job: dict, profile: dict, config: dict) -> list[dict]:
    """Reconstruct the Sonnet eval prompt messages for a job."""
    from job_finder.web.sonnet_evaluator import format_salary_range

    title = job.get("title", "Unknown Title")
    company = job.get("company", "Unknown Company")
    location = job.get("location", "Unknown Location")
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    salary_str = format_salary_range(salary_min, salary_max)
    jd_full = job.get("jd_full", "")

    positions = profile.get("positions", [])
    skills = profile.get("skills", [])
    education = profile.get("education", [])

    positions_text = ""
    for pos in positions[:5]:
        positions_text += f"\n  - {pos.get('title', '')} at {pos.get('company', '')} ({pos.get('dates', '')})"

    skills_text = ", ".join(skills[:20]) if skills else "Not specified"

    profile_prefs = config.get("profile", {})
    pref_titles = ", ".join(profile_prefs.get("target_titles", [])) or "Not specified"
    pref_locations = ", ".join(profile_prefs.get("target_locations", [])) or "Not specified"
    pref_salary = profile_prefs.get("min_salary")
    pref_salary_str = f"${pref_salary:,}" if pref_salary else "Not specified"
    pref_industries = ", ".join(profile_prefs.get("industries", [])) or "Not specified"

    ed_text = ""
    if education:
        for ed in education:
            ed_text += f"\n  - {ed.get('degree', '')} — {ed.get('institution', '')} ({ed.get('graduation', '')})"
    else:
        ed_text = "\n  Not specified"

    user_message = (
        f"## Full Job Description\n\n"
        f"**Title:** {title}\n"
        f"**Company:** {company}\n"
        f"**Location:** {location}\n"
        f"**Salary:** {salary_str}\n\n"
        f"{jd_full}\n\n---\n\n"
        f"## Candidate Experience Profile\n\n"
        f"**Key Skills:** {skills_text}\n"
        f"**Positions:**{positions_text}\n\n"
        f"**Education:**{ed_text}\n\n"
        f"## Candidate Preferences\n\n"
        f"**Target Titles:** {pref_titles}\n"
        f"**Target Locations:** {pref_locations}\n"
        f"**Minimum Salary:** {pref_salary_str}\n"
        f"**Target Industries:** {pref_industries}\n\n"
        f"Evaluate the candidate's fit for this role. Consider both competency match "
        f"(skills, experience) AND preference alignment (title, location, salary, industry). "
        f"Provide structured output."
    )

    return [{"role": "user", "content": user_message}]


def _compute_quality_metrics(per_job_results: list[dict]) -> dict:
    """Compute qualitative output comparison metrics."""
    # Basic quality metric: how many jobs got valid scores
    scored = [r for r in per_job_results if "candidate_score" in r and "error" not in r]
    return {
        "jobs_scored": len(scored),
        "jobs_errored": len(per_job_results) - len(scored),
    }


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Benchmark a candidate model against Sonnet")
    parser.add_argument("--candidate", required=True, action="append",
                        help="provider:model (e.g., gemini:gemini-2.0-flash)")
    parser.add_argument("--purpose", default="sonnet_eval",
                        help="Purpose label to benchmark (default: sonnet_eval)")
    parser.add_argument("--sample", type=int, default=30,
                        help="Number of jobs to sample (default: 30)")
    parser.add_argument("--output-dir", default="eval_results",
                        help="Output directory for reports (default: eval_results)")
    args = parser.parse_args()

    from job_finder.config import load_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = load_config()
    db_path = config.get("db", {}).get("path", "jobs.db")

    for candidate in args.candidate:
        logger.info("=== Benchmarking %s (purpose=%s, n=%d) ===", candidate, args.purpose, args.sample)
        report = run_benchmark(
            db_path=db_path,
            candidate=candidate,
            purpose=args.purpose,
            sample_size=args.sample,
            config=config,
            output_dir=args.output_dir,
        )
        verdict = report["verdict"]
        logger.info("Verdict: %s", verdict["recommendation"])
        for reason in verdict["reasons"]:
            logger.info("  + %s", reason)
        for warning in verdict.get("warnings", []):
            logger.info("  ! %s", warning)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_benchmark.py -v`
Expected: All PASS.

- [ ] **Step 5: Add eval_results/ to .gitignore**

Add to `.gitignore`:
```
eval_results/
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/ -x --timeout=120`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add job_finder/eval/benchmark.py tests/test_eval_benchmark.py .gitignore
git commit -m "feat: add benchmark CLI for evaluating candidate models against Sonnet baseline"
```

---

## Chunk 6: Final Integration and Verification

### Task 20: Update existing test mocks

After migrating all callers from `call_claude()` to `call_model()`, existing tests that mock `call_claude` at the module level need their mock targets updated.

**Files:**
- Modify: All test files that use `@patch("job_finder.web.*.call_claude")`

- [ ] **Step 1: Find all test files with call_claude mocks**

Run: `grep -r "call_claude" tests/ --include="*.py" -l`

For each file found, update the mock target:
- From: `@patch("job_finder.web.<module>.call_claude")`
- To: `@patch("job_finder.web.<module>.call_model")`

Also update any `from job_finder.web.claude_client import call_claude` in test files to `from job_finder.web.model_provider import call_model`.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/ -x --timeout=120`
Expected: All 1359+ tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test: update all test mocks from call_claude to call_model"
```

---

### Task 21: End-to-end verification

- [ ] **Step 1: Verify backwards compatibility (no providers section)**

Start the app with existing `config.yaml` (no `providers` section) and verify:
- Jobs page loads
- Dashboard shows cost stats
- Costs page works

Run: `uv run python run.py` and test in browser at localhost:5000.

- [ ] **Step 2: Verify Gemini routing (if API key available)**

Add to `config.yaml`:
```yaml
providers:
  sonnet:
    provider: gemini
    model: gemini-2.0-flash
    fallback: anthropic
  gemini:
    api_key_env: GEMINI_API_KEY
```

Set `GEMINI_API_KEY` env var and trigger a Sonnet-tier call (score a job).

- [ ] **Step 3: Run a benchmark**

Run: `uv run python -m job_finder.eval.benchmark --candidate gemini:gemini-2.0-flash --purpose sonnet_eval --sample 5`

Verify report is generated in `eval_results/`.

- [ ] **Step 4: Final full test suite**

Run: `uv run pytest tests/ --timeout=120`
Expected: All pass.

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration fixes from end-to-end verification"
```
