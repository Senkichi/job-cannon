"""Integration test for resume_parser → call_model boundary.

The unit tests in test_resume_parser.py mock `call_model` directly and assert
on the kwargs it receives. That's a useful kwarg-validity check but it does
not exercise call_model itself, which is what hid finding C-1 originally:
test_resume_parser.py was happily green while parse_resume() was actually
broken at the model_provider.call_model() boundary.

This file fills that gap: it mocks at the *provider seam* (OllamaProvider.call)
so the real call_model() is exercised end-to-end with a real (migrated) SQLite
connection and a real config dict that routes "quick" workload to Ollama. If
parse_resume ever drifts back to passing wrong kwargs into call_model, this
test will fail with a TypeError or ValueError at the boundary instead of
silently green.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from job_finder.web.model_provider import ModelResult
from job_finder.web.onboarding.resume_parser import parse_resume


@pytest.fixture
def migrated_db_for_integration():
    """Temp SQLite DB with all migrations applied — needed because call_model's
    _maybe_record_cost writes to scoring_costs."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def integration_config():
    """Minimal config matching the Phase 40 schema (providers.primary +
    fallback_chain as list of provider names).

    Empty fallback_chain is intentional: keeps the cascade single-step so the
    test fails fast if OllamaProvider.call raises, rather than silently
    falling through to a different provider and masking the failure.
    """
    return {
        "providers": {
            "primary": "ollama",
            "fallback_chain": [],
            "overrides": {},
        },
    }


def _make_pdf(tmp_path: Path) -> Path:
    """Create a minimal valid-ish PDF on disk. The actual contents don't matter
    because _extract_text is patched; only the .pdf suffix needs to be real."""
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return pdf


def test_parse_resume_dispatches_through_call_model_to_provider_seam(
    migrated_db_for_integration, integration_config, tmp_path
):
    """End-to-end through call_model: parse_resume → call_model → OllamaProvider.call.

    If parse_resume passes wrong kwargs to call_model (the C-1 bug), this
    test fails inside call_model with TypeError, not silently green.
    """
    expected_profile = {
        "positions": [
            {
                "title": "Eng",
                "company": "X",
                "start_date": "2020",
                "end_date": "2024",
                "description": "d",
            }
        ],
        "skills": ["Python"],
        "education": [],
        "target_roles_suggested": ["Senior Eng"],
        "target_locations_suggested": ["Remote"],
        "salary_range_suggested": {"min": 150000, "max": 200000, "currency": "USD"},
    }

    captured_call = {}

    def fake_provider_call(
        self,
        model,
        system,
        messages,
        output_schema=None,
        max_tokens=1024,
        timeout=None,
        options=None,
    ):
        # Signature matches OllamaProvider.call (positional). call_model invokes
        # adapter.call(model, system, messages, output_schema, max_tokens, timeout)
        # positionally (model_provider.py:699-706).
        captured_call["model"] = model
        captured_call["system"] = system
        captured_call["messages"] = messages
        captured_call["output_schema"] = output_schema
        captured_call["max_tokens"] = max_tokens
        return ModelResult(
            data=expected_profile,
            cost_usd=0.0,
            input_tokens=10,
            output_tokens=20,
            model=model,
            provider="ollama",
            schema_valid=True,
        )

    # Patch the provider seam, not call_model. Patch the bound method on the
    # OllamaProvider class so any instance call_model constructs gets the stub.
    # Also stub the health check — Ollama isn't running on CI runners, and the
    # __init__ probe would raise RuntimeError before our `call` patch ever
    # runs.
    with (
        patch(
            "job_finder.web.providers.ollama_provider.OllamaProvider._check_health",
            lambda self: None,
        ),
        patch(
            "job_finder.web.providers.ollama_provider.OllamaProvider.call",
            fake_provider_call,
        ),
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Senior Engineer at X, Python, Remote",
        ),
    ):
        pdf = _make_pdf(tmp_path)
        result = parse_resume(pdf, conn=migrated_db_for_integration, config=integration_config)

    assert result == expected_profile

    # Sanity: prove the provider was reached with sensible inputs.
    assert captured_call["model"] == "qwen2.5:14b"
    assert isinstance(captured_call["messages"], list)
    assert captured_call["messages"][0]["role"] == "user"
    assert "Senior Engineer" in captured_call["messages"][0]["content"]
    assert captured_call["max_tokens"] == 2048
    assert captured_call["output_schema"] is not None
    assert "positions" in captured_call["output_schema"]["properties"]


def test_parse_resume_records_cost_via_call_model(
    migrated_db_for_integration, integration_config, tmp_path
):
    """Cost recording is a side-effect of call_model that the previous test
    suite couldn't observe (because call_model was mocked). Confirm that a
    real call_model dispatch produces a scoring_costs row with the
    `resume_parse` purpose label, proving cost attribution is plumbed."""
    expected_profile = {
        "positions": [],
        "skills": ["Python"],
        "education": [],
        "target_roles_suggested": [],
        "target_locations_suggested": [],
        "salary_range_suggested": {},
    }

    def fake_provider_call(
        self,
        model,
        system,
        messages,
        output_schema=None,
        max_tokens=1024,
        timeout=None,
        options=None,
    ):
        return ModelResult(
            data=expected_profile,
            cost_usd=0.0,
            input_tokens=10,
            output_tokens=20,
            model=model,
            provider="ollama",
            schema_valid=True,
        )

    with (
        patch(
            "job_finder.web.providers.ollama_provider.OllamaProvider._check_health",
            lambda self: None,
        ),
        patch(
            "job_finder.web.providers.ollama_provider.OllamaProvider.call",
            fake_provider_call,
        ),
        patch(
            "job_finder.web.onboarding.resume_parser._extract_text",
            return_value="Sample text",
        ),
    ):
        pdf = _make_pdf(tmp_path)
        parse_resume(pdf, conn=migrated_db_for_integration, config=integration_config)

    # _maybe_record_cost writes purpose='resume_parse' (set in resume_parser._call_llm).
    row = migrated_db_for_integration.execute(
        "SELECT purpose, provider FROM scoring_costs WHERE purpose = 'resume_parse'"
    ).fetchone()
    assert row is not None, "Expected a scoring_costs row with purpose='resume_parse'"
    assert row["provider"] == "ollama"
