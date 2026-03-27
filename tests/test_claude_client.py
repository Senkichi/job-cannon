"""Tests for claude_client.py — record_cost provider parameter."""

import pytest

from job_finder.web.claude_client import record_cost


def test_record_cost_default_provider(migrated_db):
    """record_cost() with no provider arg inserts provider='anthropic'."""
    _, conn = migrated_db
    record_cost(conn, "job1", "haiku_score", "claude-haiku-4-5", 100, 50)
    row = conn.execute(
        "SELECT provider FROM scoring_costs WHERE job_id = ?", ("job1",)
    ).fetchone()
    assert row is not None
    assert row[0] == "anthropic"


def test_record_cost_explicit_provider(migrated_db):
    """record_cost() with provider='gemini' inserts provider='gemini'."""
    _, conn = migrated_db
    record_cost(conn, "job2", "haiku_score", "gemini-2.0-flash", 100, 50, provider="gemini")
    row = conn.execute(
        "SELECT provider FROM scoring_costs WHERE job_id = ?", ("job2",)
    ).fetchone()
    assert row is not None
    assert row[0] == "gemini"
