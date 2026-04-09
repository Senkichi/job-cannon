"""Tests for interview prep generation engine (Phase 5 - INTEL-01, INTEL-02).

Covers:
- Migration 5 table/column creation
- generate_interview_prep_background dedup guard
- generate_interview_prep_background content generation
- generate_interview_prep_background budget gating
- SerpAPI company brief fetching
"""

import json
import sqlite3
import tempfile
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.claude_client import MODEL_PRICING


# ---------------------------------------------------------------------------
# Opus Pricing Tests
# ---------------------------------------------------------------------------

class TestOpusPricing:
    """Verify Opus model is in MODEL_PRICING with correct rates."""

    def test_opus_model_in_pricing(self):
        """claude-opus-4-6 is in MODEL_PRICING dict."""
        assert "claude-opus-4-6" in MODEL_PRICING, (
            "claude-opus-4-6 missing from MODEL_PRICING. "
            "Add it to claude_client.py."
        )

    def test_opus_input_price(self):
        """claude-opus-4-6 input price is $5.0 per MTok."""
        assert MODEL_PRICING["claude-opus-4-6"]["input"] == 5.0, (
            f"Expected input=5.0, got {MODEL_PRICING['claude-opus-4-6']['input']}"
        )

    def test_opus_output_price(self):
        """claude-opus-4-6 output price is $25.0 per MTok."""
        assert MODEL_PRICING["claude-opus-4-6"]["output"] == 25.0, (
            f"Expected output=25.0, got {MODEL_PRICING['claude-opus-4-6']['output']}"
        )


# ---------------------------------------------------------------------------
# Helper: create migrated DB with a job row
# ---------------------------------------------------------------------------

def _create_test_db_with_job(dedup_key="acme|senior data scientist|remote"):
    """Create a temp migrated DB with one job row. Returns (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen,
             pipeline_status, jd_full, fit_analysis)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Senior Data Scientist",
            "Acme Corp",
            "Remote",
            "2026-03-01T10:00:00Z",
            "2026-03-11T10:00:00Z",
            "applied",
            "We are looking for a data scientist with 5+ years of experience in ML.",
            json.dumps({"strengths": ["ML experience"], "gaps": ["cloud"], "resume_priority_skills": []}),
        ),
    )
    conn.commit()
    return path, conn


# ---------------------------------------------------------------------------
# Interview Prep Dedup Tests
# ---------------------------------------------------------------------------

class TestInterviewPrepDedup:
    """Dedup guard: skip generation if prep row already exists."""

    def test_skips_when_generating_row_exists(self):
        """generate_interview_prep_background skips if status='generating' exists."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"

        # Insert an existing 'generating' row
        conn.execute(
            "INSERT INTO interview_preps (job_id, status, generated_at) VALUES (?, ?, ?)",
            (dedup_key, "generating", "2026-03-11T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            generate_interview_prep_background(dedup_key, path, config)
            # Should NOT have called the Anthropic API
            mock_anthropic.Anthropic.return_value.messages.create.assert_not_called()

        # Verify no new row was added
        conn2 = sqlite3.connect(path)
        count = conn2.execute(
            "SELECT COUNT(*) FROM interview_preps WHERE job_id = ?", (dedup_key,)
        ).fetchone()[0]
        conn2.close()
        assert count == 1, f"Expected 1 row (dedup), got {count}"

        os.remove(path)

    def test_skips_when_done_row_exists(self):
        """generate_interview_prep_background skips if status='done' exists."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"

        # Insert an existing 'done' row
        conn.execute(
            "INSERT INTO interview_preps (job_id, status, generated_at) VALUES (?, ?, ?)",
            (dedup_key, "done", "2026-03-11T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            generate_interview_prep_background(dedup_key, path, config)
            mock_anthropic.Anthropic.return_value.messages.create.assert_not_called()

        os.remove(path)

    def test_proceeds_when_error_row_exists(self):
        """generate_interview_prep_background proceeds if status='error' (can retry)."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"

        # Insert an existing 'error' row (retry is allowed)
        conn.execute(
            "INSERT INTO interview_preps (job_id, status, generated_at) VALUES (?, ?, ?)",
            (dedup_key, "error", "2026-03-11T10:00:00Z"),
        )
        conn.commit()
        conn.close()

        config = {
            "scoring": {"daily_budget_usd": 25.0, "models": {"opus": "claude-opus-4-6"}},
        }

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = {
            "company_brief": "Acme is a great company.",
            "predicted_questions": [
                {"question": "Q1", "star_story": "Story1", "key_points": ["p1"]},
                {"question": "Q2", "star_story": "Story2", "key_points": ["p2"]},
                {"question": "Q3", "star_story": "Story3", "key_points": ["p3"]},
                {"question": "Q4", "star_story": "Story4", "key_points": ["p4"]},
                {"question": "Q5", "star_story": "Story5", "key_points": ["p5"]},
            ],
            "gap_mitigation": ["Gap1"],
            "questions_to_ask": ["Q for interviewer 1"],
        }
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 1000

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value="Company info."):
                generate_interview_prep_background(dedup_key, path, config)

        # Verify a new 'done' row was inserted
        conn2 = sqlite3.connect(path)
        rows = conn2.execute(
            "SELECT status FROM interview_preps WHERE job_id = ?", (dedup_key,)
        ).fetchall()
        conn2.close()
        statuses = [r[0] for r in rows]
        assert "done" in statuses, f"Expected 'done' status, got: {statuses}"

        os.remove(path)


# ---------------------------------------------------------------------------
# Interview Prep Content Tests
# ---------------------------------------------------------------------------

class TestInterviewPrepContent:
    """Verify all 4 sections are stored on successful generation."""

    def test_generates_all_four_sections(self):
        """generate_interview_prep_background stores all 4 sections as JSON strings."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {
            "scoring": {
                "daily_budget_usd": 25.0,
                "models": {"opus": "claude-opus-4-6"},
            },
        }

        expected_prep = {
            "company_brief": "Acme Corp is a tech innovator with 500 employees.",
            "predicted_questions": [
                {"question": "Tell me about your ML experience", "star_story": "At Acme I led...", "key_points": ["5 years", "production ML"]},
                {"question": "Describe a failed experiment", "star_story": "We tested...", "key_points": ["learned", "iterated"]},
                {"question": "How do you handle ambiguity?", "star_story": "In my last role...", "key_points": ["clarify", "structure"]},
                {"question": "Tell me about A/B testing", "star_story": "I ran 50+ experiments...", "key_points": ["causal", "statistics"]},
                {"question": "What are your weaknesses?", "star_story": "I overcame...", "key_points": ["growth", "feedback"]},
            ],
            "gap_mitigation": ["Address cloud gap by highlighting on-prem Kubernetes.", "Frame lack of NLP as breadth opportunity."],
            "questions_to_ask": ["What is the data team structure?", "How do you measure DS impact?"],
        }

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = expected_prep
        mock_response.usage.input_tokens = 800
        mock_response.usage.output_tokens = 1500

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value="Acme company info"):
                generate_interview_prep_background(dedup_key, path, config)

        conn2 = sqlite3.connect(path)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT * FROM interview_preps WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (dedup_key,),
        ).fetchone()
        conn2.close()

        assert row is not None, "No interview_preps row found"
        assert row["status"] == "done", f"Expected 'done', got {row['status']}"
        assert row["company_brief"] == expected_prep["company_brief"]

        # Verify JSON columns can be parsed
        predicted_q = json.loads(row["predicted_questions"])
        assert len(predicted_q) == 5, f"Expected 5 questions, got {len(predicted_q)}"
        assert predicted_q[0]["question"] == "Tell me about your ML experience"
        assert predicted_q[0]["star_story"] == "At Acme I led..."

        gap_mit = json.loads(row["gap_mitigation"])
        assert isinstance(gap_mit, list), "gap_mitigation should be a list"
        assert len(gap_mit) == 2

        questions = json.loads(row["questions_to_ask"])
        assert isinstance(questions, list), "questions_to_ask should be a list"
        assert len(questions) == 2

        os.remove(path)

    def test_uses_opus_model(self):
        """generate_interview_prep_background calls Anthropic with Opus model."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {
            "scoring": {
                "daily_budget_usd": 25.0,
                "models": {"opus": "claude-opus-4-6"},
            },
        }

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = {
            "company_brief": "Brief.",
            "predicted_questions": [
                {"question": f"Q{i}", "star_story": f"S{i}", "key_points": ["p"]}
                for i in range(5)
            ],
            "gap_mitigation": ["gap"],
            "questions_to_ask": ["q"],
        }
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 1000

        captured_kwargs = {}

        def capture_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.side_effect = capture_create
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value="info"):
                generate_interview_prep_background(dedup_key, path, config)

        assert "claude-opus" in captured_kwargs.get("model", ""), (
            f"Expected Opus model, got: {captured_kwargs.get('model')}"
        )

        os.remove(path)

    def test_uses_own_sqlite_connection(self):
        """generate_interview_prep_background opens its own sqlite3 connection (thread safety)."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {
            "scoring": {
                "daily_budget_usd": 25.0,
                "models": {"opus": "claude-opus-4-6"},
            },
        }

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = {
            "company_brief": "Brief.",
            "predicted_questions": [
                {"question": f"Q{i}", "star_story": f"S{i}", "key_points": ["p"]}
                for i in range(5)
            ],
            "gap_mitigation": [],
            "questions_to_ask": [],
        }
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 1000

        connect_calls = []
        original_connect = sqlite3.connect

        def tracking_connect(db_path, **kwargs):
            connect_calls.append(db_path)
            return original_connect(db_path, **kwargs)

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value="info"):
                with patch("job_finder.web.interview_prep.sqlite3.connect", side_effect=tracking_connect):
                    generate_interview_prep_background(dedup_key, path, config)

        assert len(connect_calls) >= 1, "sqlite3.connect was not called"
        assert path in connect_calls, f"Expected {path} in connect calls: {connect_calls}"

        os.remove(path)

    def test_status_done_on_success(self):
        """generate_interview_prep_background sets status='done' on success."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = {
            "company_brief": "Brief.",
            "predicted_questions": [
                {"question": f"Q{i}", "star_story": f"S{i}", "key_points": ["p"]}
                for i in range(5)
            ],
            "gap_mitigation": [],
            "questions_to_ask": [],
        }
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 1000

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value="info"):
                generate_interview_prep_background(dedup_key, path, config)

        conn2 = sqlite3.connect(path)
        row = conn2.execute(
            "SELECT status FROM interview_preps WHERE job_id = ?", (dedup_key,)
        ).fetchone()
        conn2.close()

        assert row is not None, "No interview_preps row found"
        assert row[0] == "done", f"Expected 'done', got {row[0]}"

        os.remove(path)

    def test_status_error_on_failure(self):
        """generate_interview_prep_background sets status='error' with error_msg on failure."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.side_effect = RuntimeError("API failure")
            with patch("job_finder.web.interview_prep._fetch_company_info", return_value=""):
                generate_interview_prep_background(dedup_key, path, config)

        conn2 = sqlite3.connect(path)
        row = conn2.execute(
            "SELECT status, error_msg FROM interview_preps WHERE job_id = ?", (dedup_key,)
        ).fetchone()
        conn2.close()

        assert row is not None, "No interview_preps row found"
        assert row[0] == "error", f"Expected 'error', got {row[0]}"
        assert row[1] is not None and len(row[1]) > 0, "error_msg should be set on failure"

        os.remove(path)

    def test_serpapi_called_for_company_info(self):
        """_fetch_company_info is called before Opus to provide real company context."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].input = {
            "company_brief": "Brief.",
            "predicted_questions": [
                {"question": f"Q{i}", "star_story": f"S{i}", "key_points": ["p"]}
                for i in range(5)
            ],
            "gap_mitigation": [],
            "questions_to_ask": [],
        }
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 1000

        fetch_calls = []

        def track_fetch(company_name, config):
            fetch_calls.append(company_name)
            return f"Company info for {company_name}"

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
            with patch("job_finder.web.interview_prep._fetch_company_info", side_effect=track_fetch):
                generate_interview_prep_background(dedup_key, path, config)

        assert len(fetch_calls) == 1, f"Expected 1 _fetch_company_info call, got {len(fetch_calls)}"
        assert "Acme Corp" in fetch_calls[0] or fetch_calls[0] == "Acme Corp"

        os.remove(path)

    def test_generate_handles_malformed_opus_response(self):
        """call_model raising on schema mismatch → interview_prep status set to 'error'."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job("acme|malformed|remote")
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch(
            "job_finder.web.interview_prep.call_model",
            side_effect=RuntimeError("Schema validation failed"),
        ):
            with patch(
                "job_finder.web.interview_prep._fetch_company_info", return_value=""
            ):
                generate_interview_prep_background("acme|malformed|remote", path, config)

        conn2 = sqlite3.connect(path)
        row = conn2.execute(
            "SELECT status FROM interview_preps WHERE job_id = ?",
            ("acme|malformed|remote",),
        ).fetchone()
        conn2.close()
        os.remove(path)

        assert row is not None
        assert row[0] == "error"


# ---------------------------------------------------------------------------
# Budget Gating Tests
# ---------------------------------------------------------------------------

class TestInterviewPrepBudget:
    """Budget gating: sets error status when budget exceeded."""

    def test_budget_exceeded_sets_error_status(self):
        """When cost_gate returns False, prep status is set to 'error' with budget message."""
        from job_finder.web.interview_prep import generate_interview_prep_background

        path, conn = _create_test_db_with_job()
        dedup_key = "acme|senior data scientist|remote"
        conn.close()

        config = {"scoring": {"daily_budget_usd": 0.0}}  # Daily budget at zero — always blocked

        with patch("job_finder.web.interview_prep.anthropic") as mock_anthropic:
            generate_interview_prep_background(dedup_key, path, config)
            # API should NOT be called when budget exceeded
            mock_anthropic.Anthropic.return_value.messages.create.assert_not_called()

        conn2 = sqlite3.connect(path)
        row = conn2.execute(
            "SELECT status, error_msg FROM interview_preps WHERE job_id = ?", (dedup_key,)
        ).fetchone()
        conn2.close()

        assert row is not None, "No interview_preps row found"
        assert row[0] == "error", f"Expected 'error' status, got {row[0]}"
        assert row[1] is not None, "error_msg should be set when budget exceeded"
        assert "budget" in row[1].lower(), f"error_msg should mention budget: {row[1]}"

        os.remove(path)


# ---------------------------------------------------------------------------
# _fetch_company_info Tests
# ---------------------------------------------------------------------------

class TestFetchCompanyInfo:
    """Tests for _fetch_company_info SerpAPI integration."""

    def test_returns_snippet_on_success(self):
        """_fetch_company_info returns company info string on successful SerpAPI response."""
        from job_finder.web.interview_prep import _fetch_company_info

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "organic_results": [
                {"snippet": "Acme Corp is a leading tech company founded in 2010."},
                {"snippet": "Acme has 500 employees and is based in San Francisco."},
            ]
        }

        config = {"apis": {"serpapi_key": "test-key"}}

        with patch("job_finder.web.interview_prep.requests.get", return_value=mock_response):
            result = _fetch_company_info("Acme Corp", config)

        assert "Acme" in result or result != "", f"Expected non-empty result, got: {result!r}"

    def test_returns_empty_string_on_failure(self):
        """_fetch_company_info returns empty string when SerpAPI fails."""
        from job_finder.web.interview_prep import _fetch_company_info

        config = {"apis": {"serpapi_key": "test-key"}}

        with patch("job_finder.web.interview_prep.requests.get", side_effect=Exception("Network error")):
            result = _fetch_company_info("Acme Corp", config)

        assert result == "", f"Expected empty string on failure, got: {result!r}"

    def test_returns_empty_string_when_no_serpapi_key(self):
        """_fetch_company_info returns empty string when no SerpAPI key configured."""
        from job_finder.web.interview_prep import _fetch_company_info

        config = {}  # No serpapi key

        result = _fetch_company_info("Acme Corp", config)
        assert result == "", f"Expected empty string when no key, got: {result!r}"


# ---------------------------------------------------------------------------
# Interview Prep Trigger Wiring Tests (05-01-01 / INTEL-01)
# ---------------------------------------------------------------------------


class TestInterviewPrepTrigger:
    """Verify blueprint routes wire daemon thread trigger for 'applied' status.

    The TESTING guard (current_app.config.get("TESTING")) suppresses the thread
    in normal test runs to prevent Windows file lock issues. We temporarily
    disable TESTING=False in the app context to verify the trigger wiring.
    """

    def _make_app_with_job(self, db_path):
        """Create a Flask app with one job in a migrated DB."""
        import sqlite3
        from job_finder.web import create_app
        from job_finder.web.db_migrate import run_migrations

        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, first_seen, last_seen, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "acme|trigger-test|remote",
                "Trigger Test Engineer",
                "Acme Trigger Co",
                "Remote",
                "2026-03-01T10:00:00Z",
                "2026-03-11T10:00:00Z",
                "reviewing",
            ),
        )
        conn.commit()
        conn.close()

        test_config = {
            "db": {"path": db_path},
            "scoring": {"min_score_threshold": 40},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        app = create_app(config=test_config)
        # Explicitly set TESTING=False so the guard does not suppress the thread
        app.config["TESTING"] = False
        return app

    def test_jobs_status_route_triggers_thread_on_applied(self, tmp_db_path):
        """POST /jobs/<key>/status to 'applied' constructs a daemon thread targeting
        generate_interview_prep_background when TESTING guard is disabled."""
        import threading
        from job_finder.web.interview_prep import generate_interview_prep_background

        app = self._make_app_with_job(tmp_db_path)
        client = app.test_client()

        thread_kwargs_captured = []

        def fake_thread(**kwargs):
            thread_kwargs_captured.append(kwargs)
            mock_t = MagicMock()
            mock_t.start = lambda: None
            mock_t.daemon = kwargs.get("daemon", False)
            return mock_t

        with patch("threading.Thread", side_effect=fake_thread):
            response = client.post(
                "/jobs/acme%7Ctrigger-test%7Cremote/status",
                data={"pipeline_status": "applied"},
            )

        assert response.status_code == 200, (
            f"Expected 200 from status update, got {response.status_code}"
        )
        assert len(thread_kwargs_captured) == 1, (
            f"Expected threading.Thread to be called once for 'applied' transition, "
            f"got {len(thread_kwargs_captured)} calls"
        )
        kwargs = thread_kwargs_captured[0]
        assert kwargs.get("target") is generate_interview_prep_background, (
            f"Expected target=generate_interview_prep_background, "
            f"got target={kwargs.get('target')}"
        )
        assert kwargs.get("daemon") is True, (
            f"Thread must be daemon=True, got daemon={kwargs.get('daemon')}"
        )
        # First positional arg must be the dedup_key
        args = kwargs.get("args", ())
        assert args[0] == "acme|trigger-test|remote", (
            f"Expected first arg to be dedup_key, got: {args[0]!r}"
        )

    def test_jobs_status_route_does_not_trigger_thread_for_non_applied(self, tmp_db_path):
        """POST /jobs/<key>/status to 'reviewing' does NOT start a background thread."""
        app = self._make_app_with_job(tmp_db_path)
        client = app.test_client()

        thread_kwargs_captured = []

        def fake_thread(**kwargs):
            thread_kwargs_captured.append(kwargs)
            mock_t = MagicMock()
            mock_t.start = lambda: None
            return mock_t

        with patch("threading.Thread", side_effect=fake_thread):
            response = client.post(
                "/jobs/acme%7Ctrigger-test%7Cremote/status",
                data={"pipeline_status": "reviewing"},
            )

        assert response.status_code == 200
        assert len(thread_kwargs_captured) == 0, (
            f"Expected no thread for non-'applied' status, "
            f"got {len(thread_kwargs_captured)} thread(s)"
        )

    def test_pipeline_move_route_triggers_thread_on_applied(self, tmp_db_path):
        """POST /pipeline/move with new_status='applied' (Kanban drag) constructs
        a daemon thread targeting generate_interview_prep_background."""
        import threading
        from job_finder.web.interview_prep import generate_interview_prep_background

        app = self._make_app_with_job(tmp_db_path)
        client = app.test_client()

        thread_kwargs_captured = []

        def fake_thread(**kwargs):
            thread_kwargs_captured.append(kwargs)
            mock_t = MagicMock()
            mock_t.start = lambda: None
            mock_t.daemon = kwargs.get("daemon", False)
            return mock_t

        with patch("threading.Thread", side_effect=fake_thread):
            response = client.post(
                "/pipeline/move",
                data={
                    "job_id": "acme|trigger-test|remote",
                    "new_status": "applied",
                },
            )

        assert response.status_code == 200, (
            f"Expected 200 from pipeline/move, got {response.status_code}"
        )
        assert len(thread_kwargs_captured) == 1, (
            f"Expected threading.Thread called once for Kanban drag to 'applied', "
            f"got {len(thread_kwargs_captured)} calls"
        )
        kwargs = thread_kwargs_captured[0]
        assert kwargs.get("target") is generate_interview_prep_background, (
            f"Expected target=generate_interview_prep_background, "
            f"got target={kwargs.get('target')}"
        )
        assert kwargs.get("daemon") is True, (
            f"Thread must be daemon=True for Kanban drag trigger"
        )
        args = kwargs.get("args", ())
        assert args[0] == "acme|trigger-test|remote", (
            f"Expected first arg to be job_id, got: {args[0]!r}"
        )


# ---------------------------------------------------------------------------
# Tests: Reusable story extraction and reuse (Task 6)
# ---------------------------------------------------------------------------


class TestExtractReusableStories:
    """extract_reusable_stories: deterministic JSON filtering from predicted_questions."""

    def test_extracts_stories_with_non_empty_star_story(self):
        from job_finder.web.interview_prep import extract_reusable_stories
        questions = [
            {"question": "Tell me about a time...", "star_story": "At Acme, I led...", "key_points": ["leadership"]},
            {"question": "Why this role?", "star_story": "", "key_points": []},
            {"question": "Biggest challenge?", "star_story": "During a migration...", "key_points": ["resilience"]},
        ]
        result = json.loads(extract_reusable_stories(json.dumps(questions)))
        assert len(result) == 2
        assert result[0]["question"] == "Tell me about a time..."
        assert result[1]["question"] == "Biggest challenge?"

    def test_limits_to_5_stories(self):
        from job_finder.web.interview_prep import extract_reusable_stories
        questions = [
            {"question": f"Q{i}", "star_story": f"Story {i}", "key_points": []}
            for i in range(10)
        ]
        result = json.loads(extract_reusable_stories(json.dumps(questions)))
        assert len(result) == 5

    def test_returns_none_for_empty_input(self):
        from job_finder.web.interview_prep import extract_reusable_stories
        assert extract_reusable_stories("[]") is None
        assert extract_reusable_stories(None) is None
        assert extract_reusable_stories("") is None

    def test_returns_none_for_all_empty_star_stories(self):
        from job_finder.web.interview_prep import extract_reusable_stories
        questions = [
            {"question": "Q1", "star_story": "", "key_points": []},
            {"question": "Q2", "star_story": "  ", "key_points": []},
        ]
        assert extract_reusable_stories(json.dumps(questions)) is None

    def test_handles_malformed_json_gracefully(self):
        from job_finder.web.interview_prep import extract_reusable_stories
        assert extract_reusable_stories("not json") is None
        assert extract_reusable_stories("{not an array}") is None


class TestReusableStoryStorage:
    """Completed prep generation stores reusable stories."""

    @pytest.fixture
    def prep_db(self):
        """Create a migrated DB, insert a job, and return (path, conn)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen,
               score, score_breakdown, user_interest, jd_full, sonnet_score, fit_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("story|test|job", "Data Scientist", "StoryCo", "Remote",
             now, now, 0, "{}", "unreviewed",
             "Full job description for data scientist role with ML requirements.",
             75, '{"strengths": ["Python"]}'),
        )
        conn.commit()
        yield path, conn
        conn.close()
        os.remove(path)

    @patch("job_finder.web.interview_prep._fetch_company_info", return_value="")
    @patch("job_finder.web.interview_prep.call_model")
    def test_stores_reusable_stories_after_completion(self, mock_call, mock_fetch, prep_db):
        from job_finder.web.interview_prep import _run_prep_generation
        path, conn = prep_db

        mock_result = MagicMock()
        mock_result.data = {
            "company_brief": "StoryCo is a data company.",
            "predicted_questions": [
                {"question": "Tell me about ML work", "star_story": "At Acme I built models...", "key_points": ["ml"]},
                {"question": "Why this role?", "star_story": "", "key_points": []},
                {"question": "Team leadership", "star_story": "I led a team of 5...", "key_points": ["leadership"]},
            ],
            "gap_mitigation": ["Frame X as Y"],
            "questions_to_ask": ["What's the team structure?"],
        }
        mock_result.cost_usd = 0.05
        mock_call.return_value = mock_result

        _run_prep_generation(conn, "story|test|job", {"scoring": {}})

        row = conn.execute(
            "SELECT reusable_stories_json FROM interview_preps WHERE job_id = 'story|test|job' AND status = 'done'"
        ).fetchone()
        assert row is not None
        stories = json.loads(row["reusable_stories_json"])
        assert len(stories) == 2
        assert stories[0]["question"] == "Tell me about ML work"

    @patch("job_finder.web.interview_prep._fetch_company_info", return_value="")
    @patch("job_finder.web.interview_prep.call_model")
    def test_prior_stories_included_in_prompt(self, mock_call, mock_fetch, prep_db):
        from job_finder.web.interview_prep import _run_prep_generation
        path, conn = prep_db

        # Insert a prior prep with stories
        now = datetime.now(timezone.utc).isoformat()
        stories_json = json.dumps([
            {"question": "Prior Q", "star_story": "Prior story about teamwork", "key_points": ["team"]},
        ])
        conn.execute(
            "INSERT INTO interview_preps (job_id, status, generated_at, reusable_stories_json) VALUES (?, ?, ?, ?)",
            ("prior|job", "done", now, stories_json),
        )
        conn.commit()

        mock_result = MagicMock()
        mock_result.data = {
            "company_brief": "Test",
            "predicted_questions": [],
            "gap_mitigation": [],
            "questions_to_ask": [],
        }
        mock_result.cost_usd = 0.01
        mock_call.return_value = mock_result

        _run_prep_generation(conn, "story|test|job", {"scoring": {}})

        # Check that the system prompt included prior stories
        call_kwargs = mock_call.call_args[1]
        system_prompt = call_kwargs["system"]
        assert "Prior STAR Stories" in system_prompt
        assert "Prior story about teamwork" in system_prompt
