"""Tests for AI scoring infrastructure: Migration 2, claude_client cost functions,
Haiku scorer, and pipeline integration."""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from job_finder.web.claude_client import (
    compute_cost,
    record_cost,
    cost_gate,
    get_cost_stats,
    call_claude,
    BudgetExceededError,
)
from job_finder.web.haiku_scorer import build_description_snippet

# ---------------------------------------------------------------------------
# Cost computation tests
# ---------------------------------------------------------------------------

class TestCostComputation:
    """Verify compute_cost math for Haiku and Sonnet pricing."""

    def test_haiku_input_pricing(self):
        # $1.00 per MTok input = $0.000001 per token
        cost = compute_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 1.0) < 1e-9

    def test_haiku_output_pricing(self):
        # $5.00 per MTok output = $0.000005 per token
        cost = compute_cost("claude-haiku-4-5", input_tokens=0, output_tokens=1_000_000)
        assert abs(cost - 5.0) < 1e-9

    def test_haiku_combined(self):
        # 1000 input + 500 output: (1000/1e6)*1.0 + (500/1e6)*5.0
        expected = (1000 / 1_000_000) * 1.0 + (500 / 1_000_000) * 5.0
        cost = compute_cost("claude-haiku-4-5", input_tokens=1000, output_tokens=500)
        assert abs(cost - expected) < 1e-9

    def test_sonnet_input_pricing(self):
        # $3.00 per MTok input
        cost = compute_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 3.0) < 1e-9

    def test_sonnet_output_pricing(self):
        # $15.00 per MTok output
        cost = compute_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000)
        assert abs(cost - 15.0) < 1e-9

    def test_sonnet_combined(self):
        # 1000 input + 500 output: (1000/1e6)*3.0 + (500/1e6)*15.0
        expected = (1000 / 1_000_000) * 3.0 + (500 / 1_000_000) * 15.0
        cost = compute_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=500)
        assert abs(cost - expected) < 1e-9

    def test_zero_tokens(self):
        assert compute_cost("claude-haiku-4-5", 0, 0) == 0.0

# ---------------------------------------------------------------------------
# Cost recording tests
# ---------------------------------------------------------------------------

class TestCostRecording:
    """Verify record_cost inserts to scoring_costs and returns cost."""

    def test_record_cost_inserts_row(self, migrated_db):
        path, conn = migrated_db
        record_cost(conn, job_id="job-1", purpose="haiku_score",
                    model="claude-haiku-4-5", input_tokens=1000, output_tokens=500)
        rows = conn.execute("SELECT * FROM scoring_costs").fetchall()
        assert len(rows) == 1

    def test_record_cost_returns_float(self, migrated_db):
        path, conn = migrated_db
        result = record_cost(conn, job_id="job-1", purpose="haiku_score",
                              model="claude-haiku-4-5", input_tokens=1000, output_tokens=500)
        assert isinstance(result, float)

    def test_record_cost_correct_value(self, migrated_db):
        path, conn = migrated_db
        expected = compute_cost("claude-haiku-4-5", 1000, 500)
        result = record_cost(conn, job_id="job-1", purpose="haiku_score",
                              model="claude-haiku-4-5", input_tokens=1000, output_tokens=500)
        assert abs(result - expected) < 1e-9

    def test_record_cost_stores_correct_fields(self, migrated_db):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        record_cost(conn, job_id="job-42", purpose="sonnet_eval",
                    model="claude-sonnet-4-6", input_tokens=2000, output_tokens=800)
        row = conn.execute("SELECT * FROM scoring_costs").fetchone()
        assert row["job_id"] == "job-42"
        assert row["purpose"] == "sonnet_eval"
        assert row["model"] == "claude-sonnet-4-6"
        assert row["input_tokens"] == 2000
        assert row["output_tokens"] == 800
        assert row["cost_usd"] > 0

    def test_record_cost_stores_timestamp(self, migrated_db):
        path, conn = migrated_db
        conn.row_factory = sqlite3.Row
        record_cost(conn, job_id=None, purpose="test",
                    model="claude-haiku-4-5", input_tokens=100, output_tokens=50)
        row = conn.execute("SELECT * FROM scoring_costs").fetchone()
        # Timestamp should be a non-empty string
        assert row["timestamp"] and len(row["timestamp"]) > 0

# ---------------------------------------------------------------------------
# Cost gate tests
# ---------------------------------------------------------------------------

class TestCostGate:
    """Verify cost_gate correctly gates Sonnet at budget and always allows Haiku."""

    @pytest.fixture
    def gate_config(self):
        return {"scoring": {"monthly_budget_usd": 10.0}}

    def test_haiku_always_allowed_when_under_budget(self, migrated_db, gate_config):
        path, conn = migrated_db
        assert cost_gate(conn, gate_config, "haiku") is True

    def test_haiku_always_allowed_when_over_budget(self, migrated_db, gate_config):
        path, conn = migrated_db
        # Insert spend exceeding budget
        _insert_cost_row(conn, cost_usd=50.0)
        assert cost_gate(conn, gate_config, "haiku") is True

    def test_sonnet_allowed_when_under_budget(self, migrated_db, gate_config):
        path, conn = migrated_db
        _insert_cost_row(conn, cost_usd=5.0)  # Under $10 budget
        assert cost_gate(conn, gate_config, "sonnet") is True

    def test_sonnet_blocked_when_at_budget(self, migrated_db, gate_config):
        path, conn = migrated_db
        _insert_cost_row(conn, cost_usd=10.0)  # Exactly at $10 budget
        assert cost_gate(conn, gate_config, "sonnet") is False

    def test_sonnet_blocked_when_over_budget(self, migrated_db, gate_config):
        path, conn = migrated_db
        _insert_cost_row(conn, cost_usd=15.0)  # Over $10 budget
        assert cost_gate(conn, gate_config, "sonnet") is False

    def test_sonnet_uses_default_budget_when_config_missing(self, migrated_db):
        path, conn = migrated_db
        # No scoring key in config -> default $25.0 budget
        _insert_cost_row(conn, cost_usd=5.0)
        assert cost_gate(conn, {}, "sonnet") is True

    def test_sonnet_only_counts_current_month(self, migrated_db, gate_config):
        path, conn = migrated_db
        # Insert last month's spend (exceeds budget) but this month should be 0
        last_month = (datetime.now(timezone.utc) - timedelta(days=35)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "test", "claude-sonnet-4-6", 100, 50, 50.0, last_month),
        )
        conn.commit()
        # This month's spend is 0 -- should be allowed
        assert cost_gate(conn, gate_config, "sonnet") is True

# ---------------------------------------------------------------------------
# Cost statistics tests
# ---------------------------------------------------------------------------

class TestCostStats:
    """Verify get_cost_stats returns correct aggregations."""

    def test_returns_required_keys(self, migrated_db):
        path, conn = migrated_db
        stats = get_cost_stats(conn)
        assert "today" in stats
        assert "week" in stats
        assert "month" in stats
        assert "projected_monthly" in stats
        assert "by_feature" in stats

    def test_empty_db_returns_zeros(self, migrated_db):
        path, conn = migrated_db
        stats = get_cost_stats(conn)
        assert stats["today"] == 0.0
        assert stats["week"] == 0.0
        assert stats["month"] == 0.0

    def test_today_aggregation(self, migrated_db):
        path, conn = migrated_db
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "haiku_score", "claude-haiku-4-5", 100, 50, 0.05, now_str),
        )
        conn.commit()
        stats = get_cost_stats(conn)
        assert abs(stats["today"] - 0.05) < 1e-9

    def test_month_aggregation(self, migrated_db):
        path, conn = migrated_db
        # Insert rows for two different days this month
        today = datetime.now(timezone.utc)
        day1 = today.strftime("%Y-%m-%dT%H:%M:%SZ")
        day2 = (today - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ts, cost in [(day1, 0.10), (day2, 0.20)]:
            conn.execute(
                "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (None, "test", "claude-haiku-4-5", 100, 50, cost, ts),
            )
        conn.commit()
        stats = get_cost_stats(conn)
        assert abs(stats["month"] - 0.30) < 1e-9

    def test_by_feature_groups_by_purpose(self, migrated_db):
        path, conn = migrated_db
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Insert two purposes with multiple rows
        for purpose, cost in [("haiku_score", 0.01), ("haiku_score", 0.02),
                               ("sonnet_eval", 0.10)]:
            conn.execute(
                "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (None, purpose, "claude-haiku-4-5", 100, 50, cost, now_str),
            )
        conn.commit()
        stats = get_cost_stats(conn)
        by_purpose = {f["purpose"]: f for f in stats["by_feature"]}
        assert "haiku_score" in by_purpose
        assert "sonnet_eval" in by_purpose
        assert by_purpose["haiku_score"]["calls"] == 2
        assert abs(by_purpose["haiku_score"]["spend"] - 0.03) < 1e-9
        assert by_purpose["sonnet_eval"]["calls"] == 1

    def test_projected_monthly_calculation(self, migrated_db):
        """projected_monthly = month_spend / days_elapsed * 30."""
        path, conn = migrated_db
        # Insert a cost row for today
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "test", "claude-haiku-4-5", 100, 50, 6.0, now_str),
        )
        conn.commit()
        stats = get_cost_stats(conn)
        # Projected should be > 0 and finite (we don't assert exact value as days_elapsed varies)
        assert stats["projected_monthly"] >= 0.0
        assert stats["projected_monthly"] < 1_000_000  # sanity bound

# ---------------------------------------------------------------------------
# BudgetExceededError tests
# ---------------------------------------------------------------------------

class TestBudgetExceededError:
    """Verify call_claude raises BudgetExceededError when gate fails."""

    def test_raises_when_budget_exceeded(self, migrated_db, mock_anthropic_client):
        path, conn = migrated_db
        config = {"scoring": {"monthly_budget_usd": 0.0}}  # zero budget -- always blocked
        # Insert any spend > 0
        _insert_cost_row(conn, cost_usd=0.01)
        with pytest.raises(BudgetExceededError):
            call_claude(
                client=mock_anthropic_client,
                model="claude-sonnet-4-6",
                system="You are helpful.",
                messages=[{"role": "user", "content": "test"}],
                output_schema=None,
                conn=conn,
                job_id="job-1",
                purpose="test",
                config=config,
            )

    def test_haiku_succeeds_even_at_zero_budget(self, migrated_db, mock_anthropic_client):
        path, conn = migrated_db
        config = {"scoring": {"monthly_budget_usd": 0.0}}
        _insert_cost_row(conn, cost_usd=999.0)
        # Should not raise for haiku
        result, cost = call_claude(
            client=mock_anthropic_client,
            model="claude-haiku-4-5",
            system="You are helpful.",
            messages=[{"role": "user", "content": "test"}],
            output_schema=None,
            conn=conn,
            job_id="job-2",
            purpose="test",
            config=config,
        )
        assert isinstance(cost, float)
        assert cost >= 0.0

# ---------------------------------------------------------------------------
# Haiku scorer tests
# ---------------------------------------------------------------------------

class TestHaikuScorer:
    """Verify score_job_haiku() structured output, prompt content, and error handling."""

    _HAIKU_RESPONSE = {
        "score": 72,
        "summary": "Good title match, remote location fits, salary meets floor",
        "title_fit": "strong",
        "location_fit": "remote",
        "salary_meets_floor": True,
    }

    @pytest.fixture(autouse=True)
    def mock_call_claude(self):
        """Patch call_claude at haiku_scorer import to return structured Haiku output."""
        with patch("job_finder.web.haiku_scorer.call_claude",
                   return_value=(self._HAIKU_RESPONSE, 0.001)) as mock:
            self._mock = mock
            yield mock

    @pytest.fixture
    def sample_job_row(self):
        return {
            "dedup_key": "acme|senior data scientist|remote",
            "title": "Senior Data Scientist",
            "company": "Acme Corp",
            "location": "Remote",
            "salary_min": 160000,
            "salary_max": 220000,
            "description": "Build ML models at Acme Corp. " * 30,  # long description
        }

    @pytest.fixture
    def sample_profile(self):
        return {
            "target_titles": ["Senior Data Scientist", "Staff Data Scientist"],
            "target_locations": ["Remote", "San Francisco"],
            "min_salary": 150000,
            "industries": ["SaaS", "tech"],
            "skills": ["Python", "SQL", "causal inference"],
        }

    @pytest.fixture
    def scoring_config(self):
        return {
            "scoring": {
                "haiku_threshold": 55,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            }
        }

    def test_score_job_haiku_calls_correct_model(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """score_job_haiku must call call_claude with model='claude-haiku-4-5'."""
        from job_finder.web.haiku_scorer import score_job_haiku
        path, conn = migrated_db
        result = score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        # Verify messages.create was called once
        assert self._mock.call_count == 1
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5"

    def test_score_job_haiku_uses_haiku_schema(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """score_job_haiku must pass HAIKU_SCHEMA as the tool input_schema."""
        from job_finder.web.haiku_scorer import score_job_haiku, HAIKU_SCHEMA
        path, conn = migrated_db
        result = score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["output_schema"] == HAIKU_SCHEMA

    def test_score_job_haiku_returns_structured_result(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """score_job_haiku must return a ScoringResult with success status and data dict."""
        from job_finder.web.haiku_scorer import score_job_haiku
        path, conn = migrated_db
        scoring_result = score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        assert scoring_result is not None
        assert scoring_result.status == "success"
        result = scoring_result.data
        assert "score" in result
        assert "summary" in result
        assert "title_fit" in result
        assert "location_fit" in result
        assert "salary_meets_floor" in result
        assert isinstance(result["score"], int)
        assert 0 <= result["score"] <= 100
        assert isinstance(result["summary"], str)

    def test_score_job_haiku_prompt_contains_job_fields(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """Prompt must include job title, company, location, salary, and description snippet."""
        from job_finder.web.haiku_scorer import score_job_haiku
        path, conn = migrated_db
        score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        prompt_text = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        assert "Senior Data Scientist" in prompt_text
        assert "Acme Corp" in prompt_text
        assert "Remote" in prompt_text
        # Salary should be in prompt
        assert "160000" in prompt_text or "160,000" in prompt_text

    def test_score_job_haiku_prompt_contains_profile_fields(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """Prompt must include candidate's target titles, locations, and min salary."""
        from job_finder.web.haiku_scorer import score_job_haiku
        path, conn = migrated_db
        score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        prompt_text = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        assert "Senior Data Scientist" in prompt_text or "Staff Data Scientist" in prompt_text
        assert "150000" in prompt_text or "150,000" in prompt_text

    def test_score_job_haiku_purpose_is_haiku_score(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """call_claude must be called with purpose='haiku_score'."""
        from job_finder.web.haiku_scorer import score_job_haiku
        path, conn = migrated_db
        score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["purpose"] == "haiku_score"

    def test_score_job_haiku_description_uses_expanded_snippet(
        self, migrated_db, sample_profile, scoring_config
    ):
        """Description passes through build_description_snippet (not raw [:500] truncation).

        The prompt must contain the skill keyword summary injected by
        build_description_snippet, not the raw description truncated at 500 chars.
        """
        from job_finder.web.haiku_scorer import score_job_haiku
        long_description = "X" * 3000
        job_row = {
            "dedup_key": "co|job|loc",
            "title": "Data Scientist",
            "company": "Co",
            "location": "Remote",
            "salary_min": 100000,
            "salary_max": 150000,
            "description": long_description,
        }
        path, conn = migrated_db
        score_job_haiku(job_row, sample_profile, conn, scoring_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        prompt_text = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        # The prompt must include the skill keyword summary (marker of build_description_snippet)
        assert (
            "[Skill keyword matches" in prompt_text
            or "[No candidate skill keywords found" in prompt_text
        )
        # The description portion must be <= 2000 chars (enforced by build_description_snippet)
        # (we can't check exact length of description_snippet in the full prompt, but the
        # full 3000-char raw description must NOT appear verbatim)
        assert long_description not in prompt_text

    def test_score_job_haiku_handles_missing_salary(
        self, migrated_db, sample_profile, scoring_config
    ):
        """score_job_haiku with None salary fields must still return a valid result."""
        from job_finder.web.haiku_scorer import score_job_haiku
        job_row = {
            "dedup_key": "co|job|loc",
            "title": "Data Scientist",
            "company": "Co",
            "location": "Remote",
            "salary_min": None,
            "salary_max": None,
            "description": "Some job description",
        }
        path, conn = migrated_db
        scoring_result = score_job_haiku(job_row, sample_profile, conn, scoring_config)
        # Must return a result (not raise, not None)
        assert scoring_result is not None
        assert scoring_result.status == "success"
        assert "score" in scoring_result.data

    def test_score_job_haiku_handles_budget_exceeded_gracefully(
        self, migrated_db, sample_job_row, sample_profile, scoring_config
    ):
        """score_job_haiku must return ScoringResult with budget_exceeded status.

        Note: Haiku never actually hits budget cap, but the function must be
        defensive against unexpected budget errors.
        """
        from job_finder.web.haiku_scorer import score_job_haiku
        from job_finder.web.claude_client import BudgetExceededError

        self._mock.side_effect = BudgetExceededError("Budget exceeded")
        path, conn = migrated_db
        result = score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)
        assert result.status == "budget_exceeded"
        assert result.data is None

# ---------------------------------------------------------------------------
# build_description_snippet tests
# ---------------------------------------------------------------------------

class TestBuildDescriptionSnippet:
    """Verify build_description_snippet() output shape, skill summaries, and extraction."""

    def test_empty_description_returns_empty(self):
        """Empty description string returns empty string."""
        result = build_description_snippet("", ["Python", "SQL"])
        assert result == ""

    def test_short_description_returns_full_text_plus_keyword_summary(self):
        """Short description (< 1200 chars) returns full text plus a keyword summary."""
        desc = "We need a Python developer with SQL experience."
        result = build_description_snippet(desc, ["Python", "SQL"])
        assert desc in result
        # Must include the keyword summary marker
        assert "[Skill keyword matches" in result

    def test_long_description_capped_at_2000_chars(self):
        """Output for 3000-char description must be <= 2000 chars."""
        desc = "A" * 3000
        result = build_description_snippet(desc, ["Python"])
        assert len(result) <= 2000

    def test_skill_matches_from_full_description_included_in_summary(self):
        """Skill keyword counts from the FULL description appear in the summary."""
        # Description has 'Python' twice and 'SQL' once in Requirements section
        desc = "Python developer needed. " * 50 + " Requirements: Python, SQL"
        result = build_description_snippet(desc, ["Python", "SQL"])
        assert "Python" in result
        assert "[Skill keyword matches" in result
        # Python appears > 1 time so should show count
        assert "x)" in result

    def test_no_matching_skills_produces_explicit_no_match_message(self):
        """When no profile skills appear in description, produce the explicit message."""
        desc = "Java developer for backend services, Spring Boot experience required."
        result = build_description_snippet(desc, ["Python", "SQL"])
        assert "[No candidate skill keywords found in full posting text]" in result

    def test_skill_matching_is_case_insensitive(self):
        """'python' in description matches 'Python' in profile_skills."""
        desc = "Experience with python and sql preferred."
        result = build_description_snippet(desc, ["Python", "SQL"])
        assert "[Skill keyword matches" in result
        assert "Python" in result

    def test_requirements_section_extracted_when_present(self):
        """When description > 1200 chars and has a qualifications section after 800, extract it."""
        intro = "Great company that does amazing things. " * 25  # ~975 chars of intro
        requirements = "\n\nQualifications\n- 5+ years Python\n- SQL proficiency\n- A/B testing"
        desc = intro + requirements + (" padding " * 100)
        result = build_description_snippet(desc, [])
        # Requirements section should be extracted and appended
        assert "Qualifications" in result or "Python" in result

    def test_description_under_1200_chars_skips_requirements_extraction(self):
        """Descriptions < 1200 chars do not trigger requirements section extraction."""
        desc = "We need a Python developer. Requirements: Python, SQL experience required."
        # Under 1200 chars - requirements extraction path is not triggered
        result = build_description_snippet(desc, ["Python"])
        # Should still work, just no [...Requirements section:] marker
        assert result  # not empty
        assert len(result) <= 2000

    def test_empty_profile_skills_produces_no_match_message(self):
        """Empty profile_skills list always produces 'no keyword matches' message."""
        desc = "Python SQL data science machine learning."
        result = build_description_snippet(desc, [])
        assert "[No candidate skill keywords found in full posting text]" in result

# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------

class TestHaikuPipelineIntegration:
    """Verify that run_ingestion() triggers Haiku scoring and writes to DB."""

    @pytest.fixture
    def pipeline_config(self, tmp_path):
        """Config with both sources disabled (no external calls) and scoring enabled."""
        return {
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
            },
            "scoring": {
                "haiku_threshold": 55,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
        }

    def _make_mock_score_job_haiku(self, score: int = 72):
        """Return a mock score_job_haiku function that returns a ScoringResult."""
        from job_finder.web.scoring_types import ScoringResult

        def _mock(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            return ScoringResult(
                data={
                    "score": score,
                    "summary": f"Mock score {score}",
                    "title_fit": "strong" if score >= 70 else "partial",
                    "location_fit": "remote",
                    "salary_meets_floor": True,
                },
                status="success",
            )
        return _mock

    def test_haiku_scoring_runs_after_ingestion(self, migrated_db, pipeline_config):
        """After run_ingestion, new jobs must have haiku_score populated in DB."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        # Patch score_job_haiku to return a fixed score, and the sources to return one job
        test_job = Job(
            title="Senior Data Scientist",
            company="TestCo",
            location="Remote",
            source="test",
            source_url="https://example.com/job/1",
            source_id="test-1",
            salary_min=160000,
            salary_max=220000,
            description="Test job description",
        )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=self._make_mock_score_job_haiku(72)), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[test_job]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        # Verify the job was scored
        assert summary["haiku_scored"] == 1
        # Verify haiku_score was written to DB
        row = conn.execute(
            "SELECT haiku_score, haiku_summary FROM jobs WHERE company = 'TestCo'"
        ).fetchone()
        assert row is not None
        assert row["haiku_score"] == 72
        assert "Mock score" in row["haiku_summary"]

    def test_sonnet_queue_contains_high_scoring_jobs(self, migrated_db, pipeline_config):
        """Jobs with haiku_score >= threshold must appear in summary['sonnet_queue']."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        test_job = Job(
            title="Senior Data Scientist",
            company="HighScoreCo",
            location="Remote",
            source="test",
            source_url="https://example.com/job/2",
            source_id="test-2",
            salary_min=180000,
            salary_max=250000,
            description="Great job",
        )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=self._make_mock_score_job_haiku(80)), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[test_job]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        assert summary["sonnet_queued"] == 1
        assert len(summary["sonnet_queue"]) == 1

    def test_sonnet_queue_excludes_low_scoring_jobs(self, migrated_db, pipeline_config):
        """Jobs with haiku_score < threshold must NOT appear in summary['sonnet_queue']."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        test_job = Job(
            title="Junior Data Analyst",
            company="LowScoreCo",
            location="Office",
            source="test",
            source_url="https://example.com/job/3",
            source_id="test-3",
            salary_min=80000,
            salary_max=100000,
            description="Entry-level job",
        )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=self._make_mock_score_job_haiku(40)), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[test_job]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        assert summary["sonnet_queued"] == 0
        assert summary["sonnet_queue"] == []

    def test_haiku_scoring_continues_on_per_job_error(self, migrated_db, pipeline_config):
        """If one job's Haiku scoring fails, the next job is still scored."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        job1 = Job(
            title="Data Scientist",
            company="CompanyA",
            location="Remote",
            source="test",
            source_url="https://example.com/job/a",
            source_id="test-a",
        )
        job2 = Job(
            title="Data Engineer",
            company="CompanyB",
            location="Remote",
            source="test",
            source_url="https://example.com/job/b",
            source_id="test-b",
        )

        call_count = {"n": 0}

        def flaky_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ScoringResult(data=None, status="error")  # First job fails
            return ScoringResult(
                data={
                    "score": 65,
                    "summary": "Good match",
                    "title_fit": "partial",
                    "location_fit": "remote",
                    "salary_meets_floor": True,
                },
                status="success",
            )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=flaky_score), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[job1, job2]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        # Both jobs were attempted; 1 was successfully scored
        assert summary["haiku_scored"] == 1
        assert call_count["n"] == 2  # Both jobs were attempted

# ---------------------------------------------------------------------------
# Exclusion filter integration tests (Plan 27-01 Task 3)
# ---------------------------------------------------------------------------

class TestExclusionFilterIntegration:
    """Verify exclusion filter is wired into scoring_runner.run_haiku_scoring."""

    @pytest.fixture
    def pipeline_config(self, tmp_path):
        """Config with both sources disabled, exclusions configured."""
        return {
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
            },
            "scoring": {
                "haiku_threshold": 42,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
            "profile": {
                "min_salary": 150000,
                "exclusions": {
                    "title_keywords": ["junior"],
                    "companies": [],
                },
            },
        }

    def test_excluded_job_skips_haiku_in_pipeline(self, migrated_db, pipeline_config):
        """Job with excluded title must skip Haiku and have no haiku_score in DB."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        excluded_job = Job(
            title="Junior Data Analyst",
            company="ExcludedCo",
            location="Remote",
            source="test",
            source_url="https://example.com/job/exc1",
            source_id="exc-1",
            salary_min=80000,
            salary_max=100000,
            description="Entry level position.",
        )

        score_call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            score_call_count["n"] += 1
            return ScoringResult(
                data={"score": 72, "summary": "Good", "title_fit": "strong",
                      "location_fit": "remote", "salary_meets_floor": True},
                status="success",
            )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=mock_score), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[excluded_job]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        # score_job_haiku must NOT have been called for the excluded job
        assert score_call_count["n"] == 0
        assert summary["haiku_scored"] == 0

        # DB must have no haiku_score for the excluded job
        row = conn.execute(
            "SELECT haiku_score FROM jobs WHERE company = 'ExcludedCo'"
        ).fetchone()
        assert row is not None
        assert row["haiku_score"] is None

    def test_non_excluded_job_proceeds_to_haiku(self, migrated_db, pipeline_config):
        """Job with non-excluded title must proceed to score_job_haiku."""
        from job_finder.web.pipeline_runner import run_ingestion
        from job_finder.models import Job

        path, conn = migrated_db

        good_job = Job(
            title="Senior Data Scientist",
            company="GoodCo",
            location="Remote",
            source="test",
            source_url="https://example.com/job/good1",
            source_id="good-1",
            salary_min=160000,
            salary_max=220000,
            description="Build ML models at GoodCo.",
        )

        score_call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            score_call_count["n"] += 1
            return ScoringResult(
                data={"score": 75, "summary": "Good match", "title_fit": "strong",
                      "location_fit": "remote", "salary_meets_floor": True},
                status="success",
            )

        with patch("job_finder.web.scoring_runner.score_job_haiku",
                   side_effect=mock_score), \
             patch("job_finder.web.pipeline_runner._fetch_gmail", return_value=[good_job]), \
             patch("job_finder.web.pipeline_runner._fetch_serpapi", return_value=[]):
            summary = run_ingestion(path, pipeline_config)

        # score_job_haiku must have been called for the non-excluded job
        assert score_call_count["n"] == 1
        assert summary["haiku_scored"] == 1

# ---------------------------------------------------------------------------
# Sonnet evaluator tests (Plan 02-03 Task 1)
# ---------------------------------------------------------------------------

class TestSonnetEvaluator:
    """Verify evaluate_job_sonnet() produces fit analysis and handles edge cases."""

    _SONNET_RESPONSE = {
        "score": 82,
        "summary": "Strong match -- A/B testing experience aligns with role requirements.",
        "fit_analysis": {
            "strengths": ["A/B testing experience", "Python proficiency"],
            "gaps": ["No healthcare domain experience"],
            "talking_points": ["Led experimentation platform", "Causal inference work"],
            "resume_priority_skills": ["causal inference", "Python", "A/B testing"],
        },
    }

    @pytest.fixture(autouse=True)
    def mock_call_claude(self):
        """Patch call_claude at sonnet_evaluator import to return structured Sonnet output."""
        with patch("job_finder.web.sonnet_evaluator.call_claude",
                   return_value=(self._SONNET_RESPONSE, 0.005)) as mock:
            self._mock = mock
            yield mock

    @pytest.fixture
    def job_with_jd(self):
        """Job row dict with a full job description."""
        return {
            "dedup_key": "acme|senior-data-scientist|remote",
            "title": "Senior Data Scientist",
            "company": "Acme Analytics",
            "location": "Remote",
            "salary_min": 180000,
            "salary_max": 250000,
            "jd_full": (
                "We are looking for a Senior Data Scientist to join our team. "
                "You will design and run A/B experiments, build causal inference models, "
                "and work with Python and SQL daily. 5+ years experience required."
            ),
        }

    @pytest.fixture
    def sample_profile(self):
        """Sample experience profile."""
        return {
            "positions": [
                {
                    "title": "Senior Data Scientist",
                    "company": "Prev Corp",
                    "skills": ["Python", "SQL", "causal inference"],
                    "achievements": ["Led experimentation platform", "Grew team by 3x"],
                }
            ],
            "skills": ["Python", "SQL", "causal inference", "A/B testing"],
        }

    @pytest.fixture
    def sonnet_config(self):
        return {
            "scoring": {
                "haiku_threshold": 55,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            }
        }

    def test_calls_call_claude_with_sonnet_model(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """evaluate_job_sonnet must call call_claude with model='claude-sonnet-4-6'."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet, SONNET_SCHEMA
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        assert self._mock.call_count == 1
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-6"

    def test_calls_with_sonnet_schema(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """evaluate_job_sonnet must pass SONNET_SCHEMA as the tool input_schema."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet, SONNET_SCHEMA
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["output_schema"] == SONNET_SCHEMA

    def test_returns_dict_with_score_summary_fit_analysis(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """evaluate_job_sonnet must return ScoringResult with score, summary, fit_analysis."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        scoring_result = evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        assert scoring_result is not None
        assert scoring_result.status == "success"
        result = scoring_result.data
        assert "score" in result
        assert "summary" in result
        assert "fit_analysis" in result

    def test_fit_analysis_has_required_keys(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """fit_analysis must include strengths, gaps, talking_points, resume_priority_skills."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        scoring_result = evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        assert scoring_result is not None
        fa = scoring_result.data["fit_analysis"]
        assert "strengths" in fa
        assert "gaps" in fa
        assert "talking_points" in fa
        assert "resume_priority_skills" in fa
        # All should be lists
        assert isinstance(fa["strengths"], list)
        assert isinstance(fa["gaps"], list)
        assert isinstance(fa["talking_points"], list)
        assert isinstance(fa["resume_priority_skills"], list)

    def test_prompt_includes_jd_full_text(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """Prompt must include the full job description text."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        assert "A/B experiments" in user_content or "causal inference" in user_content

    def test_prompt_includes_job_metadata(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """Prompt must include job title, company, location."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        assert "Senior Data Scientist" in user_content
        assert "Acme Analytics" in user_content

    def test_prompt_includes_experience_profile(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """Prompt must include candidate profile positions and skills."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        assert "Python" in user_content

    def test_returns_none_when_jd_full_is_none(
        self, sample_profile, sonnet_config, migrated_db
    ):
        """evaluate_job_sonnet must return None when jd_full is None (Sonnet requires JD)."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        job_no_jd = {
            "dedup_key": "co|job|loc",
            "title": "Data Scientist",
            "company": "Co",
            "location": "Remote",
            "jd_full": None,
        }
        result = evaluate_job_sonnet(job_no_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        assert result.status == "skipped"
        assert result.data is None
        # Must NOT have called the API
        assert self._mock.call_count == 0

    def test_returns_none_on_budget_exceeded_error(
        self, sample_profile, sonnet_config, migrated_db, job_with_jd
    ):
        """evaluate_job_sonnet must return ScoringResult with budget_exceeded status."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        from job_finder.web.claude_client import BudgetExceededError
        path, conn = migrated_db

        self._mock.side_effect = BudgetExceededError("Budget exceeded")

        result = evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        assert result.status == "budget_exceeded"
        assert result.data is None

    def test_records_cost_with_purpose_sonnet_eval(
        self, job_with_jd, sample_profile, sonnet_config, migrated_db
    ):
        """Cost row must be recorded with purpose='sonnet_eval'."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db
        evaluate_job_sonnet(job_with_jd, experience_profile=sample_profile, conn=conn, config=sonnet_config)
        call_kwargs = self._mock.call_args.kwargs
        assert call_kwargs["purpose"] == "sonnet_eval"

# ---------------------------------------------------------------------------
# Sonnet pipeline integration tests (Plan 02-03 Task 2)
# ---------------------------------------------------------------------------

class TestSonnetPipelineIntegration:
    """Verify run_sonnet_evaluation() uses pre-enriched jd_full, runs Sonnet, persists results.

    As of Phase 10, jd_full is populated by enrich_job BEFORE Haiku scoring.
    run_sonnet_evaluation no longer fetches JD — it relies on jd_full already
    being present in the jobs table from the enrichment pipeline.
    """

    @pytest.fixture
    def pipeline_config(self):
        """Config with Sonnet model and no external sources."""
        return {
            "scoring": {
                "haiku_threshold": 55,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False, "api_key": ""},
            },
        }

    @pytest.fixture
    def job_with_jd(self, migrated_db):
        """Insert a job with jd_full already populated (enriched before Haiku scoring)."""
        path, conn = migrated_db
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, haiku_score, jd_full)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "acme|senior-ds|remote",
                "Senior Data Scientist",
                "Acme Analytics",
                "Remote",
                '["test"]',
                '["https://example.com/jobs/senior-ds"]',
                "2026-03-10T00:00:00",
                "2026-03-10T00:00:00",
                0,
                "{}",
                "unreviewed",
                72,
                "Full job description text for Senior Data Scientist at Acme Analytics.",
            ),
        )
        conn.commit()
        return migrated_db

    @pytest.fixture
    def mock_sonnet_result(self):
        """Standard Sonnet evaluation result for mocking (ScoringResult NamedTuple)."""
        from job_finder.web.scoring_types import ScoringResult
        return ScoringResult(
            data={
                "score": 85,
                "summary": "Excellent match -- strong A/B testing background aligns well.",
                "fit_analysis": {
                    "strengths": ["A/B testing expertise", "Python proficiency"],
                    "gaps": ["No healthcare background"],
                    "talking_points": ["Led experimentation platform"],
                    "resume_priority_skills": ["causal inference", "Python"],
                },
            },
            status="success",
        )

    def test_uses_existing_jd_full_for_sonnet(self, job_with_jd, pipeline_config, mock_sonnet_result):
        """run_sonnet_evaluation uses pre-populated jd_full (set by enrich_job before Haiku).

        Phase 10: JD fetching moved to enrich_job before Haiku scoring. Sonnet
        no longer fetches JDs — it relies on jd_full already being in the DB.
        """
        from job_finder.web.scoring_runner import run_sonnet_evaluation

        path, conn = job_with_jd

        with patch("job_finder.web.scoring_runner.evaluate_job_sonnet", return_value=mock_sonnet_result):
            count = run_sonnet_evaluation(["acme|senior-ds|remote"], pipeline_config, path)

        assert count == 1
        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = 'acme|senior-ds|remote'"
        ).fetchone()
        assert row is not None
        assert row["jd_full"] == "Full job description text for Senior Data Scientist at Acme Analytics."

    def test_writes_sonnet_score_to_db(self, job_with_jd, pipeline_config, mock_sonnet_result):
        """run_sonnet_evaluation must write sonnet_score to jobs table."""
        from job_finder.web.scoring_runner import run_sonnet_evaluation

        path, conn = job_with_jd

        with patch("job_finder.web.scoring_runner.evaluate_job_sonnet", return_value=mock_sonnet_result):
            count = run_sonnet_evaluation(["acme|senior-ds|remote"], pipeline_config, path)

        assert count == 1
        row = conn.execute(
            "SELECT sonnet_score FROM jobs WHERE dedup_key = 'acme|senior-ds|remote'"
        ).fetchone()
        assert row["sonnet_score"] == 85

    def test_writes_fit_analysis_as_json(self, job_with_jd, pipeline_config, mock_sonnet_result):
        """run_sonnet_evaluation must write fit_analysis as valid JSON string."""
        from job_finder.web.scoring_runner import run_sonnet_evaluation

        path, conn = job_with_jd

        with patch("job_finder.web.scoring_runner.evaluate_job_sonnet", return_value=mock_sonnet_result):
            run_sonnet_evaluation(["acme|senior-ds|remote"], pipeline_config, path)

        row = conn.execute(
            "SELECT fit_analysis FROM jobs WHERE dedup_key = 'acme|senior-ds|remote'"
        ).fetchone()
        assert row["fit_analysis"] is not None
        parsed = json.loads(row["fit_analysis"])
        assert "strengths" in parsed
        assert "gaps" in parsed

    def test_skips_job_when_no_jd_available(self, migrated_db, pipeline_config):
        """Job with no jd_full -> Sonnet skipped, sonnet_score remains NULL.

        Phase 10: No JD fetch attempt — job is skipped if jd_full is absent after
        the enrichment pipeline ran before Haiku scoring.
        """
        from job_finder.web.scoring_runner import run_sonnet_evaluation

        path, conn = migrated_db
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "corp|job|loc", "Job", "Corp", "Remote",
                '["test"]', '[]',  # No source URLs, no jd_full
                "2026-03-10T00:00:00", "2026-03-10T00:00:00", 0, '{}', "unreviewed",
            ),
        )
        conn.commit()

        count = run_sonnet_evaluation(["corp|job|loc"], pipeline_config, path)

        assert count == 0
        row = conn.execute("SELECT sonnet_score FROM jobs WHERE dedup_key='corp|job|loc'").fetchone()
        assert row["sonnet_score"] is None

    def test_budget_exceeded_skips_gracefully(self, job_with_jd, pipeline_config):
        """Budget exceeded during Sonnet -> graceful skip, haiku_score preserved."""
        from job_finder.web.scoring_runner import run_sonnet_evaluation
        from job_finder.web.claude_client import BudgetExceededError

        path, conn = job_with_jd

        with patch("job_finder.web.scoring_runner.evaluate_job_sonnet", return_value=None):
            count = run_sonnet_evaluation(["acme|senior-ds|remote"], pipeline_config, path)

        assert count == 0
        # haiku_score must be unchanged
        row = conn.execute(
            "SELECT haiku_score, sonnet_score FROM jobs WHERE dedup_key='acme|senior-ds|remote'"
        ).fetchone()
        assert row["haiku_score"] == 72
        assert row["sonnet_score"] is None

# ---------------------------------------------------------------------------
# Sonnet candidate preferences tests (Plan 27-02 Task 2)
# ---------------------------------------------------------------------------

class TestSonnetPreferences:
    """Verify evaluate_job_sonnet injects Candidate Preferences from config.yaml.

    Sonnet should evaluate both "can do" (experience) AND "wants to do" (preferences)
    based on the target_titles, target_locations, min_salary, and industries in config.
    """

    _SONNET_RESPONSE = {
        "score": 82,
        "summary": "Strong match.",
        "fit_analysis": {
            "strengths": ["Python proficiency"],
            "gaps": ["No healthcare experience"],
            "talking_points": ["Led experimentation platform"],
            "resume_priority_skills": ["Python", "A/B testing"],
        },
    }

    @pytest.fixture(autouse=True)
    def mock_call_claude(self):
        """Patch call_claude at sonnet_evaluator import to return structured Sonnet output."""
        with patch("job_finder.web.sonnet_evaluator.call_claude",
                   return_value=(self._SONNET_RESPONSE, 0.005)) as mock:
            self._mock = mock
            yield mock

    @pytest.fixture
    def job_with_jd(self):
        """Job row with a full job description."""
        return {
            "dedup_key": "pref|test|remote",
            "title": "Senior Data Scientist",
            "company": "PreferenceCo",
            "location": "Remote",
            "salary_min": 180000,
            "salary_max": 250000,
            "jd_full": "We are looking for a Senior Data Scientist. Python and SQL required.",
        }

    @pytest.fixture
    def sample_experience_profile(self):
        """Sample experience profile (separate from config preferences)."""
        return {
            "positions": [
                {
                    "title": "Senior Data Scientist",
                    "company": "Prev Corp",
                    "skills": ["Python", "SQL"],
                    "achievements": ["Led ML platform"],
                }
            ],
            "skills": ["Python", "SQL", "A/B testing"],
        }

    @pytest.fixture
    def config_with_preferences(self):
        """Config with profile preferences section."""
        return {
            "scoring": {
                "haiku_threshold": 42,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
            "profile": {
                "target_titles": ["Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": ["Tech"],
            },
        }

    def test_sonnet_prompt_contains_preferences_section(
        self, job_with_jd, sample_experience_profile,
        config_with_preferences, migrated_db
    ):
        """Prompt must include ## Candidate Preferences section with all preference fields."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db

        evaluate_job_sonnet(
            job_with_jd, sample_experience_profile, conn,
            config_with_preferences
        )

        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))

        assert "## Candidate Preferences" in user_content
        assert "**Target Titles:** Data Scientist" in user_content
        assert "**Target Locations:** Remote" in user_content
        assert "**Minimum Salary:** $150,000" in user_content
        assert "**Target Industries:** Tech" in user_content

    def test_sonnet_prompt_handles_empty_preferences(
        self, job_with_jd, sample_experience_profile, migrated_db
    ):
        """Prompt shows 'Not specified' for all fields when config has empty profile section."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db

        empty_config = {
            "scoring": {
                "haiku_threshold": 42,
                "monthly_budget_usd": 25.0,
                "models": {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-4-6"},
            },
            "profile": {},  # empty profile section
        }

        evaluate_job_sonnet(
            job_with_jd, sample_experience_profile, conn, empty_config
        )

        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))

        assert "## Candidate Preferences" in user_content
        # All fields should say "Not specified"
        assert user_content.count("Not specified") >= 4

    def test_sonnet_prompt_includes_preference_evaluation_instruction(
        self, job_with_jd, sample_experience_profile,
        config_with_preferences, migrated_db
    ):
        """Evaluation instruction must mention preference alignment."""
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        path, conn = migrated_db

        evaluate_job_sonnet(
            job_with_jd, sample_experience_profile, conn,
            config_with_preferences
        )

        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))

        assert "preference alignment" in user_content

# ---------------------------------------------------------------------------
# Stale detection tests (Plan 02-04 Task 1)
# ---------------------------------------------------------------------------

class TestStaleDetection:
    """Verify run_stale_detection marks/clears stale jobs and auto-archives correctly."""

    def _insert_job(self, conn, dedup_key, last_seen, pipeline_status="discovered"):
        """Helper to insert a job row with a specific last_seen date."""
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status, is_stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_key, "Test Job", "Test Co", "Remote",
                '["test"]', '["https://example.com"]',
                "2026-01-01T00:00:00", last_seen,
                0, "{}", "unreviewed", pipeline_status, 0,
            ),
        )
        conn.commit()

    def test_marks_14_day_old_job_as_stale(self, migrated_db):
        """A job not seen for 14+ days should be marked is_stale=1."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        # last_seen 15 days ago
        last_seen = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "stale-job-15d", last_seen)

        result = run_stale_detection(path)

        assert result["stale_marked"] == 1
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'stale-job-15d'"
        ).fetchone()
        assert row["is_stale"] == 1

    def test_recent_job_not_marked_stale(self, migrated_db):
        """A job seen within 14 days should NOT be marked stale."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        # last_seen 5 days ago
        last_seen = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "fresh-job-5d", last_seen)

        result = run_stale_detection(path)

        assert result["stale_marked"] == 0
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 'fresh-job-5d'"
        ).fetchone()
        assert row["is_stale"] == 0

    def test_clears_stale_flag_for_re_seen_job(self, migrated_db):
        """A previously stale job that is now recently seen should have is_stale cleared."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        # Insert as stale (is_stale=1) but recently seen
        recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, pipeline_status, is_stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "re-seen-job", "Test", "Corp", "Remote",
                '["test"]', '[]',
                "2026-01-01T00:00:00", recent,
                0, "{}", "unreviewed", "discovered", 1,  # was stale
            ),
        )
        conn.commit()

        result = run_stale_detection(path)

        assert result["stale_cleared"] == 1
        row = conn.execute(
            "SELECT is_stale FROM jobs WHERE dedup_key = 're-seen-job'"
        ).fetchone()
        assert row["is_stale"] == 0

    def test_archives_30_day_discovered_job(self, migrated_db):
        """A discovered job not seen for 30+ days should be auto-archived."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        last_seen = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "old-discovered-job", last_seen, pipeline_status="discovered")

        result = run_stale_detection(path)

        assert result["archived"] == 1
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'old-discovered-job'"
        ).fetchone()
        assert row["pipeline_status"] == "archived"

    def test_archives_30_day_reviewing_job(self, migrated_db):
        """A reviewing job not seen for 30+ days should be auto-archived."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        last_seen = (datetime.now() - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "old-reviewing-job", last_seen, pipeline_status="reviewing")

        result = run_stale_detection(path)

        assert result["archived"] >= 1
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'old-reviewing-job'"
        ).fetchone()
        assert row["pipeline_status"] == "archived"

    def test_does_not_archive_applied_job(self, migrated_db):
        """An applied job (active pipeline stage) must NEVER be auto-archived."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        last_seen = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "applied-job", last_seen, pipeline_status="applied")

        run_stale_detection(path)

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'applied-job'"
        ).fetchone()
        assert row["pipeline_status"] == "applied"  # must NOT be archived

    def test_does_not_archive_offer_job(self, migrated_db):
        """An offer-stage job must NEVER be auto-archived."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        last_seen = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
        self._insert_job(conn, "offer-job", last_seen, pipeline_status="offer")

        run_stale_detection(path)

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'offer-job'"
        ).fetchone()
        assert row["pipeline_status"] == "offer"  # must NOT be archived

    def test_returns_correct_counts(self, migrated_db):
        """run_stale_detection returns dict with stale_marked, stale_cleared, archived."""
        from job_finder.web.stale_detector import run_stale_detection

        path, conn = migrated_db
        result = run_stale_detection(path)

        assert isinstance(result, dict)
        assert "stale_marked" in result
        assert "stale_cleared" in result
        assert "archived" in result

# ---------------------------------------------------------------------------
# Borderline re-evaluation band tests (Plan 27-02 Task 1)
# ---------------------------------------------------------------------------

class TestBorderlineReeval:
    """Verify the borderline re-evaluation band (C2) in run_haiku_scoring.

    Jobs scoring 42-54 on the initial Haiku call get a second Haiku call with
    max_chars=4000 before the Sonnet decision.
    """

    @pytest.fixture
    def pipeline_config(self):
        """Config with haiku_threshold=42 (lowered from 55 in Plan 27-01)."""
        return {
            "sources": {
                "gmail": {"enabled": False},
                "serpapi": {"enabled": False},
            },
            "scoring": {
                "haiku_threshold": 42,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
            "profile": {
                "min_salary": 150000,
                "exclusions": {
                    "title_keywords": [],
                    "companies": [],
                },
            },
        }

    def _insert_test_job(self, conn, dedup_key="borderline|job|remote"):
        """Insert a test job row into the jobs table."""
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_key, "Borderline Role", "TestCo", "Remote",
                '["test"]', '["https://example.com/job/1"]',
                "2026-03-10T00:00:00", "2026-03-10T00:00:00",
                0, "{}", "unreviewed",
            ),
        )
        conn.commit()

    def test_borderline_job_gets_reeval_call(self, migrated_db, pipeline_config):
        """Job with initial haiku_score=48 (in 42-54 band) triggers second score_job_haiku
        call with max_chars=4000, purpose='haiku_reeval'. Final DB score is re-eval score."""
        from job_finder.web.scoring_runner import run_haiku_scoring

        path, conn = migrated_db
        dedup_key = "borderline|job|remote"
        self._insert_test_job(conn, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Initial call: borderline score
                return ScoringResult(
                    data={"score": 48, "summary": "Initial borderline", "title_fit": "partial",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )
            else:
                # Re-eval call: higher score
                return ScoringResult(
                    data={"score": 60, "summary": "Re-eval improved", "title_fit": "strong",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )

        with patch("job_finder.web.scoring_runner.score_job_haiku", side_effect=mock_score):
            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], pipeline_config, path)

        # score_job_haiku must be called twice (initial + re-eval)
        assert call_count["n"] == 2
        # Final haiku_score in DB should be the re-eval score (60, not 48)
        row = conn.execute(
            "SELECT haiku_score, haiku_summary FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        assert row["haiku_score"] == 60
        assert "Re-eval improved" in row["haiku_summary"]
        # Job should be in sonnet_queue (60 >= 42)
        assert dedup_key in sonnet_queue

    def test_borderline_job_filtered_after_reeval(self, migrated_db, pipeline_config):
        """Job with initial=48, re-eval=38: haiku_score saved as 38, NOT in sonnet_queue."""
        from job_finder.web.scoring_runner import run_haiku_scoring

        path, conn = migrated_db
        dedup_key = "borderline|filtered|remote"
        self._insert_test_job(conn, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ScoringResult(
                    data={"score": 48, "summary": "Initial borderline", "title_fit": "partial",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )
            else:
                # Re-eval: drops below threshold
                return ScoringResult(
                    data={"score": 38, "summary": "Re-eval confirms weak fit", "title_fit": "weak",
                          "location_fit": "other", "salary_meets_floor": False},
                    status="success",
                )

        with patch("job_finder.web.scoring_runner.score_job_haiku", side_effect=mock_score):
            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], pipeline_config, path)

        # Two calls were made
        assert call_count["n"] == 2
        # Final score is re-eval score (38)
        row = conn.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        assert row["haiku_score"] == 38
        # NOT in sonnet_queue (38 < 42)
        assert dedup_key not in sonnet_queue

    def test_above_band_skips_reeval(self, migrated_db, pipeline_config):
        """Job with initial haiku_score=65 (above 54 band ceiling) goes directly to
        sonnet_queue with no re-eval call."""
        from job_finder.web.scoring_runner import run_haiku_scoring

        path, conn = migrated_db
        dedup_key = "above|band|remote"
        self._insert_test_job(conn, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            return ScoringResult(
                data={"score": 65, "summary": "Good match above band", "title_fit": "strong",
                      "location_fit": "remote", "salary_meets_floor": True},
                status="success",
            )

        with patch("job_finder.web.scoring_runner.score_job_haiku", side_effect=mock_score):
            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], pipeline_config, path)

        # Only one call (no re-eval)
        assert call_count["n"] == 1
        # Job is in sonnet_queue
        assert dedup_key in sonnet_queue

    def test_below_threshold_skips_reeval(self, migrated_db, pipeline_config):
        """Job with initial haiku_score=30 (below threshold 42) is filtered with no re-eval."""
        from job_finder.web.scoring_runner import run_haiku_scoring

        path, conn = migrated_db
        dedup_key = "below|threshold|remote"
        self._insert_test_job(conn, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            return ScoringResult(
                data={"score": 30, "summary": "Poor fit", "title_fit": "reject",
                      "location_fit": "other", "salary_meets_floor": False},
                status="success",
            )

        with patch("job_finder.web.scoring_runner.score_job_haiku", side_effect=mock_score):
            sonnet_queue, haiku_scored = run_haiku_scoring([dedup_key], pipeline_config, path)

        # Only one call (no re-eval below threshold)
        assert call_count["n"] == 1
        # NOT in sonnet_queue
        assert dedup_key not in sonnet_queue

# ---------------------------------------------------------------------------
# Batch Haiku borderline re-eval tests (Plan 28-02)
# ---------------------------------------------------------------------------

class TestBatchHaikuBorderlineReeval:
    """Verify borderline re-eval band (42-54) works in dashboard batch scoring path.

    _run_batch_haiku_bg should apply the same logic as pipeline_runner._run_haiku_scoring:
    jobs scoring 42-54 get a second score_job_haiku call with max_chars=4000.
    """

    @pytest.fixture
    def batch_config(self):
        """Config with haiku_threshold=42."""
        return {
            "scoring": {
                "haiku_threshold": 42,
                "monthly_budget_usd": 25.0,
                "models": {
                    "haiku": "claude-haiku-4-5",
                    "sonnet": "claude-sonnet-4-6",
                },
            },
            "profile": {
                "min_salary": 150000,
                "exclusions": {
                    "title_keywords": [],
                    "companies": [],
                },
            },
        }

    def _setup_db(self, db_path: str, dedup_key: str = "batch|borderline|remote") -> int:
        """Insert an unscored job and a batch_score_sessions row; return session_id."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_key, "Batch Borderline Role", "BatchCo", "Remote",
                '["test"]', '["https://example.com/job/b1"]',
                "2026-03-10T00:00:00", "2026-03-10T00:00:00",
                0, "{}", "unreviewed",
            ),
        )
        conn.execute(
            """INSERT INTO batch_score_sessions
               (session_type, status, total, scored, skipped, started_at)
               VALUES ('haiku', 'running', 1, 0, 0, '2026-03-10T00:00:00')"""
        )
        session_id = conn.execute(
            "SELECT id FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        conn.commit()
        conn.close()
        return session_id

    def test_batch_borderline_triggers_reeval(self, migrated_db, batch_config):
        """Job scoring 48 in batch Haiku triggers second call with max_chars=4000."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = migrated_db
        dedup_key = "batch|borderline|remote"
        session_id = self._setup_db(path, dedup_key)

        call_args_list = []

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_args_list.append({"max_chars": max_chars, "purpose": purpose})
            if len(call_args_list) == 1:
                return ScoringResult(
                    data={"score": 48, "summary": "Initial borderline", "title_fit": "partial",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )
            else:
                return ScoringResult(
                    data={"score": 62, "summary": "Re-eval improved", "title_fit": "strong",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )

        with patch("job_finder.web.haiku_scorer.score_job_haiku", side_effect=mock_score), \
             patch("job_finder.web.scoring_orchestrator.load_scoring_profile", return_value={}):
            _run_batch_haiku_bg(path, session_id, batch_config)

        # score_job_haiku must be called twice
        assert len(call_args_list) == 2
        # Second call must use max_chars=4000 and purpose="haiku_reeval"
        assert call_args_list[1]["max_chars"] == 4000
        assert call_args_list[1]["purpose"] == "haiku_reeval"
        # DB haiku_score is the re-eval score (62, not 48)
        row = conn.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        assert row["haiku_score"] == 62

    def test_batch_above_band_no_reeval(self, migrated_db, batch_config):
        """Job scoring 70 in batch Haiku does NOT trigger re-eval."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = migrated_db
        dedup_key = "batch|above|band"
        session_id = self._setup_db(path, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            return ScoringResult(
                data={"score": 70, "summary": "Strong match", "title_fit": "strong",
                      "location_fit": "remote", "salary_meets_floor": True},
                status="success",
            )

        with patch("job_finder.web.haiku_scorer.score_job_haiku", side_effect=mock_score), \
             patch("job_finder.web.scoring_orchestrator.load_scoring_profile", return_value={}):
            _run_batch_haiku_bg(path, session_id, batch_config)

        # score_job_haiku must be called exactly once (no re-eval)
        assert call_count["n"] == 1

    def test_batch_borderline_reeval_overwrites_score(self, migrated_db, batch_config):
        """Re-eval score replaces initial borderline score in jobs table."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_haiku_bg

        path, conn = migrated_db
        dedup_key = "batch|overwrite|remote"
        session_id = self._setup_db(path, dedup_key)

        call_count = {"n": 0}

        def mock_score(job_row, profile, conn, config, max_chars=2000, purpose="haiku_score"):
            from job_finder.web.scoring_types import ScoringResult
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ScoringResult(
                    data={"score": 45, "summary": "Initial", "title_fit": "partial",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )
            else:
                return ScoringResult(
                    data={"score": 58, "summary": "Re-eval score", "title_fit": "strong",
                          "location_fit": "remote", "salary_meets_floor": True},
                    status="success",
                )

        with patch("job_finder.web.haiku_scorer.score_job_haiku", side_effect=mock_score), \
             patch("job_finder.web.scoring_orchestrator.load_scoring_profile", return_value={}):
            _run_batch_haiku_bg(path, session_id, batch_config)

        # jobs.haiku_score must be the re-eval score (58), not the initial (45)
        row = conn.execute(
            "SELECT haiku_score, haiku_summary FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        assert row["haiku_score"] == 58
        assert "Re-eval score" in row["haiku_summary"]

# ---------------------------------------------------------------------------
# Filter tests (Plan 02-04 Task 1)
# ---------------------------------------------------------------------------

class TestFilteredJobsSorting:
    """Verify COALESCE sort: sonnet_score > haiku_score > heuristic score."""

    def test_coalesce_sort_prefers_sonnet_over_haiku_over_heuristic(self, migrated_db):
        """Sort by 'score' uses COALESCE(sonnet_score, haiku_score, score) DESC."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        # Job 1: sonnet_score=85 (highest AI score)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, sonnet_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j1", "Sonnet Job", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", 85.0),
        )
        # Job 2: haiku_score=60, no sonnet
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, haiku_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j2", "Haiku Job", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 3.0, "{}", "unreviewed", 60.0),
        )
        # Job 3: heuristic score=7.5 only, no AI scores
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("j3", "Heuristic Job", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 7.5, "{}", "unreviewed"),
        )
        conn.commit()

        jobs = get_filtered_jobs(conn, sort_by="score", sort_dir="DESC")
        dedup_keys = [j["dedup_key"] for j in jobs]

        # j1 (sonnet=85) > j2 (haiku=60) > j3 (heuristic=7.5)
        j1_idx = dedup_keys.index("j1")
        j2_idx = dedup_keys.index("j2")
        j3_idx = dedup_keys.index("j3")
        assert j1_idx < j2_idx < j3_idx

class TestHideStaleFilter:
    """Verify hide_stale=True excludes stale jobs from results."""

    def test_hide_stale_excludes_stale_jobs(self, migrated_db):
        """When hide_stale=True, is_stale=1 jobs are excluded."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        # Fresh job
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, is_stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("fresh-job", "Fresh", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", 0),
        )
        # Stale job
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, is_stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stale-job", "Stale", "Co", "Remote", '[]', '[]',
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", 5.0, "{}", "unreviewed", 1),
        )
        conn.commit()

        jobs_with_stale = get_filtered_jobs(conn, hide_stale=False)
        jobs_without_stale = get_filtered_jobs(conn, hide_stale=True)

        all_keys_with = [j["dedup_key"] for j in jobs_with_stale]
        all_keys_without = [j["dedup_key"] for j in jobs_without_stale]

        assert "stale-job" in all_keys_with
        assert "fresh-job" in all_keys_with
        assert "stale-job" not in all_keys_without
        assert "fresh-job" in all_keys_without

    def test_hide_stale_false_includes_all_jobs(self, migrated_db):
        """When hide_stale=False (default), stale jobs are included."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, is_stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stale-j2", "Old Job", "Co", "Remote", '[]', '[]',
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", 5.0, "{}", "unreviewed", 1),
        )
        conn.commit()

        jobs = get_filtered_jobs(conn)  # default hide_stale=False
        keys = [j["dedup_key"] for j in jobs]
        assert "stale-j2" in keys

# ---------------------------------------------------------------------------
# Score display tests (Plan 02-04 Task 1)
# ---------------------------------------------------------------------------

class TestScoreDisplay:
    """Tests for score display backend: COALESCE sort and relative_date filter."""

    def test_relative_date_format_pattern(self):
        """relative_date returns string matching 'Mon D (X ago)' pattern."""
        import re
        from job_finder.web.blueprints.jobs import relative_date

        # 7 days ago
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT12:00:00")
        result = relative_date(seven_days_ago)
        assert re.match(r"^[A-Z][a-z]{2} \d{1,2} \(.+\)$", result), (
            f"Expected pattern 'Mon D (...)' but got: {result}"
        )

    def test_relative_date_returns_dash_for_none(self):
        """relative_date(None) returns '---'."""
        from job_finder.web.blueprints.jobs import relative_date

        assert relative_date(None) == "---"

    def test_relative_date_returns_dash_for_empty_string(self):
        """relative_date('') returns '---'."""
        from job_finder.web.blueprints.jobs import relative_date

        assert relative_date("") == "---"

    def test_relative_date_exactly_7_days_ago(self):
        """relative_date with ISO date exactly 7 days ago produces '... (1w ago)'."""
        from job_finder.web.blueprints.jobs import relative_date

        # Use midnight to ensure stable 7-day delta
        seven_days_ago = (datetime.now() - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        iso_str = seven_days_ago.strftime("%Y-%m-%dT%H:%M:%S")
        result = relative_date(iso_str)
        assert "(1w ago)" in result, f"Expected '(1w ago)' in result, got: {result}"

    def test_relative_date_today(self):
        """relative_date with today's date produces '... (today)'."""
        from job_finder.web.blueprints.jobs import relative_date

        today_iso = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        result = relative_date(today_iso)
        assert "(today)" in result, f"Expected '(today)' in result, got: {result}"

    def test_relative_date_recent_days(self):
        """relative_date with 3 days ago produces '... (3d ago)'."""
        from job_finder.web.blueprints.jobs import relative_date

        three_days_ago = (datetime.now() - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        result = relative_date(three_days_ago)
        assert "(3d ago)" in result, f"Expected '(3d ago)' in result, got: {result}"

    def test_relative_date_months_ago(self):
        """relative_date with 60 days ago produces '... (2mo ago)'."""
        from job_finder.web.blueprints.jobs import relative_date

        sixty_days_ago = (datetime.now() - timedelta(days=60)).replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        result = relative_date(sixty_days_ago)
        assert "(2mo ago)" in result, f"Expected '(2mo ago)' in result, got: {result}"

    def test_sort_by_score_uses_coalesce(self, migrated_db):
        """get_filtered_jobs sort_by='score' uses COALESCE(sonnet_score, haiku_score, score)."""
        from job_finder.db import get_filtered_jobs

        path, conn = migrated_db

        # Job with sonnet_score=85
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, sonnet_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "Top Sonnet", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 5.0, "{}", "unreviewed", 85.0),
        )
        # Job with haiku_score=60 (no sonnet)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, haiku_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s2", "Mid Haiku", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 3.0, "{}", "unreviewed", 60.0),
        )
        # Job with only heuristic score=7.5 (no AI scores)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s3", "Low Heuristic", "Co", "Remote", '[]', '[]',
             "2026-03-01T00:00:00", "2026-03-01T00:00:00", 7.5, "{}", "unreviewed"),
        )
        conn.commit()

        jobs = get_filtered_jobs(conn, sort_by="score", sort_dir="DESC")
        keys = [j["dedup_key"] for j in jobs]

        # sonnet=85 > haiku=60 > heuristic=7.5
        s1_idx = keys.index("s1")
        s2_idx = keys.index("s2")
        s3_idx = keys.index("s3")
        assert s1_idx < s2_idx < s3_idx

# ---------------------------------------------------------------------------
# Haiku compensation context tests (Plan 07-02 Task 2)
# ---------------------------------------------------------------------------

class TestHaikuCompensationContext:
    """Verify Haiku scoring prompt includes compensation context from comp_data_json."""

    _HAIKU_RESPONSE = {
        "score": 75,
        "summary": "Good match",
        "title_fit": "strong",
        "location_fit": "remote",
        "salary_meets_floor": True,
    }

    @pytest.fixture(autouse=True)
    def mock_call_claude(self):
        """Patch call_claude at haiku_scorer import to return structured Haiku output."""
        with patch("job_finder.web.haiku_scorer.call_claude",
                   return_value=(self._HAIKU_RESPONSE, 0.001)) as mock:
            self._mock = mock
            yield mock

    @pytest.fixture
    def haiku_config(self):
        """Config for haiku scorer tests."""
        return {
            "scoring": {
                "haiku_threshold": 55,
                "monthly_budget_usd": 25.0,
                "models": {"haiku": "claude-haiku-4-5"},
            }
        }

    def test_haiku_prompt_includes_comp_context_when_present(self, migrated_db, haiku_config):
        """Haiku prompt includes Additional Compensation line when comp_data_json is set."""
        import json as _json
        from job_finder.web.haiku_scorer import score_job_haiku

        path, conn = migrated_db

        # Job row with Ashby compensation tier summary
        comp_data = {
            "compensationTierSummary": "Equity 0.01%-0.1%, Bonus 15% target",
            "summaryComponents": [],
        }
        job_row = {
            "dedup_key": "test|comp-job",
            "title": "Senior Data Scientist",
            "company": "TestCo",
            "location": "Remote",
            "salary_min": 200000,
            "salary_max": 280000,
            "description": "Build ML models.",
            "comp_data_json": _json.dumps(comp_data),
        }
        profile = {
            "target_titles": ["data scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "skills": ["Python"],
            "industries": ["Technology"],
        }

        score_job_haiku(job_row, profile, conn, haiku_config)

        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        assert "Additional Compensation" in user_content
        assert "Equity" in user_content or "Bonus" in user_content

    def test_haiku_prompt_gracefully_handles_missing_comp_json(self, migrated_db, haiku_config):
        """Haiku prompt works without error when comp_data_json is null/absent."""
        from job_finder.web.haiku_scorer import score_job_haiku

        path, conn = migrated_db

        # Job row without comp_data_json
        job_row = {
            "dedup_key": "test|no-comp-job",
            "title": "Data Engineer",
            "company": "TestCo",
            "location": "Remote",
            "salary_min": 150000,
            "salary_max": 200000,
            "description": "Build pipelines.",
            "comp_data_json": None,
        }
        profile = {
            "target_titles": ["data engineer"],
            "target_locations": ["Remote"],
            "min_salary": 120000,
            "skills": ["Python", "SQL"],
            "industries": [],
        }

        result = score_job_haiku(job_row, profile, conn, haiku_config)

        # Should succeed without error
        assert result is not None
        assert result.status == "success"
        assert "score" in result.data

        # Prompt should NOT have "Additional Compensation" line
        call_kwargs = self._mock.call_args.kwargs
        messages = call_kwargs["messages"]
        user_content = " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        assert "Additional Compensation" not in user_content

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_cost_row(conn: sqlite3.Connection, cost_usd: float, purpose: str = "test",
                     model: str = "claude-sonnet-4-6") -> None:
    """Insert a cost row into scoring_costs with a current-month timestamp."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, purpose, model, 100, 50, cost_usd, now_str),
    )
    conn.commit()
