"""Test schema_valid telemetry population for Phase 35."""

import pytest
from job_finder.web.model_provider import ModelResult, _maybe_record_cost
from job_finder.web.claude_client import record_cost


def test_maybe_record_cost_includes_schema_valid(migrated_db):
    """Test that _maybe_record_cost populates schema_valid in INSERT."""
    path, conn = migrated_db
    result = ModelResult(
        data={"test": "data"},
        cost_usd=0.0,
        input_tokens=10,
        output_tokens=20,
        model="test-model",
        provider="ollama",
        schema_valid=True,
    )
    _maybe_record_cost(result, conn, job_id="test-job", purpose="test-purpose")
    
    row = conn.execute(
        "SELECT schema_valid FROM scoring_costs WHERE purpose = ?",
        ("test-purpose",)
    ).fetchone()
    assert row is not None
    assert row[0] == 1  # True as integer


def test_maybe_record_cost_schema_valid_false(migrated_db):
    """Test that _maybe_record_cost handles schema_valid=False."""
    path, conn = migrated_db
    result = ModelResult(
        data={"test": "data"},
        cost_usd=0.0,
        input_tokens=10,
        output_tokens=20,
        model="test-model",
        provider="ollama",
        schema_valid=False,
    )
    _maybe_record_cost(result, conn, job_id="test-job", purpose="test-purpose")
    
    row = conn.execute(
        "SELECT schema_valid FROM scoring_costs WHERE purpose = ?",
        ("test-purpose",)
    ).fetchone()
    assert row is not None
    assert row[0] == 0  # False as integer


def test_record_cost_includes_schema_valid(migrated_db):
    """Test that record_cost populates schema_valid in INSERT."""
    path, conn = migrated_db
    record_cost(
        conn,
        job_id="test-job",
        purpose="test-purpose",
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=20,
        provider="anthropic",
        schema_valid=True,
    )
    
    row = conn.execute(
        "SELECT schema_valid FROM scoring_costs WHERE purpose = ?",
        ("test-purpose",)
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_record_cost_schema_valid_false(migrated_db):
    """Test that record_cost handles schema_valid=False."""
    path, conn = migrated_db
    record_cost(
        conn,
        job_id="test-job",
        purpose="test-purpose",
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=20,
        provider="anthropic",
        schema_valid=False,
    )
    
    row = conn.execute(
        "SELECT schema_valid FROM scoring_costs WHERE purpose = ?",
        ("test-purpose",)
    ).fetchone()
    assert row is not None
    assert row[0] == 0
