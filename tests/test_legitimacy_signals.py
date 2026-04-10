"""Tests for ghost job legitimacy signal computation."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from job_finder.web.legitimacy_signals import compute_legitimacy_signals


@pytest.fixture
def mem_db():
    """In-memory SQLite with minimal jobs table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        sources TEXT DEFAULT '[]'
    )""")
    conn.commit()
    return conn


class TestPostingAge:
    def test_age_from_iso_string(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        signals = compute_legitimacy_signals({"first_seen_at": old_date}, None)
        assert signals["posting_age_days"] == 45

    def test_age_over_60_triggers_warning(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=65)).isoformat()
        signals = compute_legitimacy_signals({"first_seen_at": old_date}, None)
        assert "WARNING" in signals["legitimacy_note"]
        assert "65 days old" in signals["legitimacy_note"]

    def test_age_over_30_triggers_note(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        signals = compute_legitimacy_signals({"first_seen_at": old_date}, None)
        assert "Note:" in signals["legitimacy_note"]

    def test_recent_posting_no_age_flag(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        signals = compute_legitimacy_signals(
            {"first_seen_at": recent, "salary_min": 100000}, None
        )
        # No age flag; salary present means no salary flag either
        assert "days old" not in signals["legitimacy_note"]

    def test_missing_date(self):
        signals = compute_legitimacy_signals({}, None)
        assert signals["posting_age_days"] is None

    def test_z_suffix_timestamp(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        signals = compute_legitimacy_signals({"first_seen_at": old_date}, None)
        assert signals["posting_age_days"] == 10


class TestSourceCount:
    def test_single_source(self, mem_db):
        mem_db.execute(
            "INSERT INTO jobs (dedup_key, sources) VALUES (?, ?)",
            ("key1", json.dumps(["linkedin"])),
        )
        mem_db.commit()
        signals = compute_legitimacy_signals({"dedup_key": "key1"}, mem_db)
        assert signals["source_count"] == 1

    def test_multi_source_triggers_flag(self, mem_db):
        mem_db.execute(
            "INSERT INTO jobs (dedup_key, sources) VALUES (?, ?)",
            ("key2", json.dumps(["linkedin", "glassdoor", "serpapi", "thordata"])),
        )
        mem_db.commit()
        signals = compute_legitimacy_signals({"dedup_key": "key2"}, mem_db)
        assert signals["source_count"] == 4
        assert "perpetual repost" in signals["legitimacy_note"]

    def test_no_conn_defaults_to_1(self):
        signals = compute_legitimacy_signals({"dedup_key": "x"}, None)
        assert signals["source_count"] == 1


class TestSalary:
    def test_has_salary(self):
        signals = compute_legitimacy_signals({"salary_min": 120000}, None)
        assert signals["has_salary"] is True
        assert "No salary" not in signals["legitimacy_note"]

    def test_no_salary(self):
        signals = compute_legitimacy_signals({}, None)
        assert signals["has_salary"] is False
        assert "No salary" in signals["legitimacy_note"]


class TestJDSpecificity:
    def test_short_description_flagged(self):
        signals = compute_legitimacy_signals(
            {"description": "Apply now!", "salary_min": 100000}, None
        )
        assert "Very short" in signals["legitimacy_note"]

    def test_empty_description(self):
        signals = compute_legitimacy_signals({"description": ""}, None)
        assert signals["description_length"] == 0

    def test_filler_ratio_computed(self):
        filler_text = (
            "We are a fast-paced environment looking for a self-starter. "
            "This is an exciting opportunity to join a dynamic team. "
            "We offer competitive salary and great benefits. " * 5
        )
        signals = compute_legitimacy_signals(
            {"description": filler_text, "salary_min": 100000}, None
        )
        assert signals["filler_ratio"] > 0


class TestHealthyJob:
    def test_no_flags(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        job = {
            "first_seen_at": recent,
            "salary_min": 150000,
            "description": "x" * 500,  # substantial description
        }
        signals = compute_legitimacy_signals(job, None)
        assert signals["legitimacy_note"] == ""


class TestAllFlags:
    def test_old_no_salary_short_desc(self, mem_db):
        old_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        mem_db.execute(
            "INSERT INTO jobs (dedup_key, sources) VALUES (?, ?)",
            ("multi", json.dumps(["a", "b", "c", "d", "e"])),
        )
        mem_db.commit()
        job = {
            "first_seen_at": old_date,
            "dedup_key": "multi",
            "description": "Apply here",
        }
        signals = compute_legitimacy_signals(job, mem_db)
        note = signals["legitimacy_note"]
        assert "WARNING" in note
        assert "perpetual repost" in note
        assert "Very short" in note
        assert "No salary" in note
