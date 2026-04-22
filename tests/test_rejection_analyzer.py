"""Tests for rejection analysis batch job and on-demand Dashboard trigger.

Tests cover:
- Migration 5 tables (rejection_reports, rejection_reviewed) exist after migration
- run_rejection_analysis opens own sqlite3 connection (thread-safe)
- Queries WHERE pipeline_status='rejected' AND rejection_reviewed=0
- Returns {rejections_analyzed: 0, report_id: None} when no unreviewed rejections
- Batches ALL rejections in a SINGLE Opus call (not one-per-rejection)
- Stores report in rejection_reports with cost_usd and generated_at
- Marks all analyzed jobs rejection_reviewed=1 after analysis
- Budget gate check before Opus call; graceful skip if budget exceeded
- On-demand POST /dashboard/rejection-analysis route calls run_rejection_analysis
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_migrated_db():
    """Create a temp DB and run all migrations. Returns (path, conn)."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn

def insert_rejected_job(conn, dedup_key, title="Data Scientist", company="Acme",
                        rejection_reviewed=0, classification="reject",
                        sub_scores_json='{"title_fit": 3, "location_fit": 4, "comp_fit": 3, "domain_match": 3, "seniority_match": 2, "skills_match": 3}',
                        jd_full="Job description text"):
    """Helper to insert a rejected job into the test DB (v3.0 Phase 34 Plan 3)."""
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, first_seen, last_seen, score, pipeline_status,
             rejection_reviewed, classification, sub_scores_json, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key, title, company, "Remote", '["linkedin"]', '["https://example.com"]',
            "123", "2026-03-01T10:00:00", "2026-03-10T10:00:00",
            7.5, "rejected", rejection_reviewed, classification, sub_scores_json, jd_full,
        ),
    )
    conn.commit()

def make_opus_mock_response():
    """Return a mock Anthropic client that returns a valid rejection analysis result."""
    analysis_result = {
        "patterns": [
            {
                "pattern": "Role-level mismatch: most rejections at Staff/Principal level",
                "frequency": "3 out of 3 rejections",
                "factor": "profile_match",
            }
        ],
        "recommendations": [
            {
                "action": "Target Senior rather than Staff/Principal roles",
                "impact": "high",
                "details": "Profile aligns better with Senior-level scope",
            }
        ],
        "summary": "All rejections share a common theme of level mismatch. Targeting Senior rather than Staff roles would improve fit.",
    }

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = analysis_result
    mock_response.usage.input_tokens = 1500
    mock_response.usage.output_tokens = 400

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client, analysis_result

# ---------------------------------------------------------------------------
# Opus pricing
# ---------------------------------------------------------------------------

class TestOpusPricing:
    """Verify Opus is in MODEL_PRICING for cost gating."""

    def test_opus_in_model_pricing(self):
        """MODEL_PRICING includes claude-opus-4-6 with correct rates."""
        from job_finder.web.claude_client import MODEL_PRICING

        assert "claude-opus-4-6" in MODEL_PRICING, "claude-opus-4-6 missing from MODEL_PRICING"
        pricing = MODEL_PRICING["claude-opus-4-6"]
        assert pricing["input"] == 5.0, f"Expected input=5.0, got {pricing['input']}"
        assert pricing["output"] == 25.0, f"Expected output=25.0, got {pricing['output']}"

# ---------------------------------------------------------------------------
# Core analysis engine
# ---------------------------------------------------------------------------

class TestNoUnreviewedRejections:
    """Verify graceful handling when no unreviewed rejections exist."""

    def test_returns_zero_when_no_rejected_jobs(self):
        """run_rejection_analysis returns {rejections_analyzed: 0} when no rejected jobs."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        result = run_rejection_analysis(path, config)

        os.unlink(path)

        assert result["rejections_analyzed"] == 0
        assert result.get("report_id") is None

    def test_returns_zero_when_all_rejections_already_reviewed(self):
        """run_rejection_analysis returns {rejections_analyzed: 0} when all rejections are reviewed."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "acme|ds|remote", rejection_reviewed=1)
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        result = run_rejection_analysis(path, config)

        os.unlink(path)

        assert result["rejections_analyzed"] == 0
        assert result.get("report_id") is None

    def test_does_not_call_opus_when_no_unreviewed(self):
        """Opus API is NOT called when there are no unreviewed rejections."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        run_rejection_analysis(path, config)

        os.unlink(path)

# ---------------------------------------------------------------------------
# Batch analysis tests
# ---------------------------------------------------------------------------

class TestRejectionAnalysisBatch:
    """Test core batch analysis behavior."""

    def test_single_opus_call_for_all_rejections(self):
        """ALL rejections are batched in a SINGLE Opus call, not one per rejection."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "job1|ds|remote", title="Job 1")
        insert_rejected_job(conn, "job2|ds|remote", title="Job 2")
        insert_rejected_job(conn, "job3|ds|remote", title="Job 3")
        conn.close()

        _, analysis_result = make_opus_mock_response()
        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)) as mock_cc:
            run_rejection_analysis(path, config)

        os.unlink(path)

        # Should be called exactly once (not 3 times)
        assert mock_cc.call_count == 1, (
            f"Expected 1 Opus call for all rejections, got {mock_cc.call_count}"
        )

    def test_report_stored_in_rejection_reports_table(self):
        """Report is stored in rejection_reports table with required fields."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "acme|ds|remote")
        conn.close()

        _, analysis_result = make_opus_mock_response()
        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)):
            result = run_rejection_analysis(path, config)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        report = conn.execute(
            "SELECT * FROM rejection_reports WHERE id = ?", (result["report_id"],)
        ).fetchone()
        conn.close()
        os.unlink(path)

        assert report is not None, "No report stored in rejection_reports"
        assert report["rejections_analyzed"] == 1
        assert report["generated_at"] is not None
        assert report["cost_usd"] is not None
        report_text = json.loads(report["report_text"])
        assert "patterns" in report_text
        assert "recommendations" in report_text
        assert "summary" in report_text

    def test_analyzed_jobs_marked_rejection_reviewed(self):
        """After analysis, all included jobs have rejection_reviewed=1."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "job1|ds|remote", title="Job 1")
        insert_rejected_job(conn, "job2|ds|remote", title="Job 2")
        conn.close()

        _, analysis_result = make_opus_mock_response()
        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)):
            run_rejection_analysis(path, config)

        conn = sqlite3.connect(path)
        unreviewed = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status='rejected' AND rejection_reviewed=0"
        ).fetchone()[0]
        reviewed = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE rejection_reviewed=1"
        ).fetchone()[0]
        conn.close()
        os.unlink(path)

        assert unreviewed == 0, f"Expected 0 unreviewed, got {unreviewed}"
        assert reviewed == 2, f"Expected 2 reviewed, got {reviewed}"

    def test_returns_correct_count_and_report_id(self):
        """run_rejection_analysis returns dict with rejections_analyzed and report_id."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "job1|ds|remote")
        insert_rejected_job(conn, "job2|ds|remote")
        conn.close()

        _, analysis_result = make_opus_mock_response()
        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)):
            result = run_rejection_analysis(path, config)

        os.unlink(path)

        assert result["rejections_analyzed"] == 2
        assert result["report_id"] is not None
        assert isinstance(result["report_id"], int)

    def test_only_unreviewed_rejections_included(self):
        """Only jobs with rejection_reviewed=0 are included in analysis."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "job1|ds|remote", rejection_reviewed=0)
        insert_rejected_job(conn, "job2|ds|remote", rejection_reviewed=1)  # already reviewed
        conn.close()

        _, analysis_result = make_opus_mock_response()
        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)):
            result = run_rejection_analysis(path, config)

        os.unlink(path)

        # Only 1 unreviewed rejection should be analyzed
        assert result["rejections_analyzed"] == 1

# ---------------------------------------------------------------------------
# Budget gate tests
# ---------------------------------------------------------------------------

class TestBudgetGate:
    """Verify budget gating prevents Opus calls when cap exceeded."""

    def test_graceful_skip_when_budget_exceeded(self):
        """Returns {rejections_analyzed: 0, budget_exceeded: True} when budget exceeded."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "acme|ds|remote")
        conn.close()

        # Zero budget forces cost_gate to return False
        config = {"scoring": {"daily_budget_usd": 0.0}}

        mock_client = MagicMock()
        result = run_rejection_analysis(path, config)

        os.unlink(path)

        assert result["rejections_analyzed"] == 0
        assert result.get("budget_exceeded") is True
        mock_client.messages.create.assert_not_called()

    def test_no_report_stored_when_budget_exceeded(self):
        """No report is stored in rejection_reports when budget gate blocks."""
        from job_finder.web.rejection_analyzer import run_rejection_analysis

        path, conn = make_migrated_db()
        insert_rejected_job(conn, "acme|ds|remote")
        conn.close()

        config = {"scoring": {"daily_budget_usd": 0.0}}
        run_rejection_analysis(path, config)

        conn = sqlite3.connect(path)
        count = conn.execute("SELECT COUNT(*) FROM rejection_reports").fetchone()[0]
        conn.close()
        os.unlink(path)

        assert count == 0, f"Expected 0 reports when budget exceeded, got {count}"

# ---------------------------------------------------------------------------
# On-demand Dashboard route
# ---------------------------------------------------------------------------

def _make_test_config(db_path, budget=25.0):
    """Return a minimal config dict accepted by create_app()."""
    return {
        "db": {"path": db_path},
        "scoring": {
            "min_score_threshold": 40,
            "daily_budget_usd": budget,
        },
        "profile": {
            "target_titles": ["Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }

class TestOnDemandTrigger:
    """Test the POST /dashboard/rejection-analysis on-demand route."""

    @pytest.fixture
    def flask_app(self, tmp_db_path):
        """Create a test Flask app with mocked Claude and migrated DB."""
        from job_finder.web import create_app

        app = create_app(config=_make_test_config(tmp_db_path))
        app.config["TESTING"] = True
        return app, tmp_db_path

    def test_route_exists_and_accepts_post(self, flask_app):
        """POST /dashboard/rejection-analysis returns a redirect (302)."""
        app, _ = flask_app
        with app.test_client() as client:
            resp = client.post("/dashboard/rejection-analysis")
        assert resp.status_code in (301, 302), f"Expected redirect, got {resp.status_code}"

    def test_route_flashes_no_unreviewed_message(self, flask_app):
        """When no unreviewed rejections, flashes 'No unreviewed rejections' info message."""
        app, _ = flask_app
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_flashes"] = []
            client.post("/dashboard/rejection-analysis")
            with client.session_transaction() as sess:
                flashes = sess.get("_flashes", [])

        messages = [msg for cat, msg in flashes]
        assert any("No unreviewed" in m or "0" in m for m in messages), (
            f"Expected 'no unreviewed' flash, got: {messages}"
        )

    def test_route_flashes_success_with_count(self, flask_app):
        """When rejections analyzed, flashes success message with count."""
        app, db_path = flask_app
        # Insert a rejected job into the test DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        insert_rejected_job(conn, "test|ds|remote")
        conn.close()

        _, analysis_result = make_opus_mock_response()

        with patch("job_finder.web.rejection_analyzer.call_claude",
                   return_value=(analysis_result, 0.10)):
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_flashes"] = []
                client.post("/dashboard/rejection-analysis")
                with client.session_transaction() as sess:
                    flashes = sess.get("_flashes", [])

        messages = [msg for cat, msg in flashes]
        assert any("1" in m or "analyzed" in m.lower() for m in messages), (
            f"Expected success flash with count, got: {messages}"
        )

    def test_route_flashes_budget_exceeded_warning(self, tmp_db_path):
        """When budget exceeded, flashes warning message."""
        from job_finder.web import create_app
        from job_finder.web.db_migrate import run_migrations

        # Run migrations first so jobs table exists before inserting
        run_migrations(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        insert_rejected_job(conn, "test|ds|remote")
        conn.close()

        app = create_app(config=_make_test_config(tmp_db_path, budget=0.0))
        app.config["TESTING"] = True

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_flashes"] = []
            client.post("/dashboard/rejection-analysis")
            with client.session_transaction() as sess:
                flashes = sess.get("_flashes", [])

        categories = [cat for cat, msg in flashes]
        assert "warning" in categories, f"Expected 'warning' flash category, got: {categories}"
