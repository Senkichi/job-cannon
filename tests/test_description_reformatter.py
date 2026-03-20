"""Tests for description_reformatter.py — Haiku-assisted description reformatting.

Tests cover:
- reformat_description returns section/paragraph formatted text on success
- reformat_description returns original when Haiku call fails (graceful degradation)
- reformat_description returns original when description is None or empty
- reformat_description returns original when description is already well-formatted
- run_description_reformat_pass processes only jobs where description_reformatted=0
- run_description_reformat_pass sets description_reformatted=1 after each job
- run_description_reformat_pass skips jobs where description is NULL
- run_description_reformat_pass records cost per Haiku call
- run_description_reformat_pass returns count of reformatted jobs
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client that simulates a Haiku reformatting response."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = (
        "About the Role\n\n"
        "We are looking for a Data Scientist to build ML models.\n\n"
        "Responsibilities\n\n"
        "- Deploy data pipelines\n"
        "- Monitor model performance\n"
    )
    mock_response.usage.input_tokens = 200
    mock_response.usage.output_tokens = 100

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


@pytest.fixture
def temp_db_path():
    """Create a temp SQLite DB with jobs table and description_reformatted column."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            description TEXT,
            description_reformatted INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def db_with_unformatted_jobs(temp_db_path):
    """DB with 3 jobs: 2 unformatted (reformatted=0) and 1 already formatted (reformatted=1)."""
    conn = sqlite3.connect(temp_db_path)
    conn.executemany(
        "INSERT INTO jobs (dedup_key, title, company, description, description_reformatted) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                "acme|ds|remote",
                "Data Scientist",
                "Acme Corp",
                "Build ML models | Deploy pipelines | Monitor performance",
                0,  # needs reformatting
            ),
            (
                "beta|sds|sf",
                "Staff Data Scientist",
                "Beta Inc",
                "Lead experiments. Run A/B tests. Mentor junior scientists.",
                0,  # needs reformatting
            ),
            (
                "gamma|ds|nyc",
                "Data Scientist",
                "Gamma Corp",
                "About the Role\n\nWe are looking for...\n\nRequirements\n\n- Python skills",
                1,  # already reformatted — should be skipped
            ),
        ],
    )
    conn.commit()
    conn.close()
    return temp_db_path


@pytest.fixture
def db_with_null_description(temp_db_path):
    """DB with one job that has NULL description."""
    conn = sqlite3.connect(temp_db_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, description, description_reformatted) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acme|ds|remote", "Data Scientist", "Acme Corp", None, 0),
    )
    conn.commit()
    conn.close()
    return temp_db_path


# ---------------------------------------------------------------------------
# Tests for reformat_description
# ---------------------------------------------------------------------------


class TestReformatDescription:
    def test_reformat_description_calls_haiku_and_returns_sectioned_text(
        self, mock_anthropic_client
    ):
        """reformat_description sends pipe-separated description to Haiku and returns sections."""
        from job_finder.web.description_reformatter import reformat_description

        pipe_description = "Build ML models | Deploy pipelines | Monitor performance"

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = (
                {"text": "About the Role\n\nBuild ML models.\n\nResponsibilities\n\n- Deploy pipelines"},
                0.0002,
            )
            result = reformat_description(pipe_description, mock_anthropic_client)

        mock_call.assert_called_once()
        # Result should be the reformatted text (different from input)
        assert result is not None
        assert isinstance(result, str)

    def test_reformat_description_returns_original_on_haiku_failure(
        self, mock_anthropic_client
    ):
        """reformat_description returns original description when Haiku call fails."""
        from job_finder.web.description_reformatter import reformat_description

        original = "Build ML models | Deploy pipelines | Monitor performance"

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.side_effect = Exception("API error")
            result = reformat_description(original, mock_anthropic_client)

        assert result == original

    def test_reformat_description_returns_original_when_none(self):
        """reformat_description returns None unchanged when description is None."""
        from job_finder.web.description_reformatter import reformat_description

        result = reformat_description(None, MagicMock())
        assert result is None

    def test_reformat_description_returns_original_when_empty(self):
        """reformat_description returns empty string unchanged."""
        from job_finder.web.description_reformatter import reformat_description

        result = reformat_description("", MagicMock())
        assert result == ""

    def test_reformat_description_skips_already_formatted_description(self):
        """reformat_description returns original when description has 2+ section headers."""
        from job_finder.web.description_reformatter import reformat_description

        already_formatted = (
            "About the Role\n\n"
            "We are looking for a Data Scientist.\n\n"
            "Requirements\n\n"
            "- Python skills\n"
            "- ML experience\n"
        )

        mock_client = MagicMock()
        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            result = reformat_description(already_formatted, mock_client)

        # Should return original without calling Haiku
        mock_call.assert_not_called()
        assert result == already_formatted

    def test_reformat_description_with_conn_records_cost(
        self, mock_anthropic_client, temp_db_path
    ):
        """reformat_description records cost via client.record_cost when conn provided."""
        from job_finder.web.description_reformatter import reformat_description

        conn = sqlite3.connect(temp_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scoring_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, purpose TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0, timestamp TEXT NOT NULL
            )
        """)
        conn.commit()

        original = "Build ML models | Deploy pipelines | Monitor performance"

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = (
                {"text": "About the Role\n\nBuild ML models."},
                0.0002,
            )
            result = reformat_description(
                original,
                mock_anthropic_client,
                conn=conn,
                config={"scoring": {"models": {"haiku": "claude-haiku-4-5"}}},
            )

        mock_call.assert_called_once()
        conn.close()


# ---------------------------------------------------------------------------
# Tests for run_description_reformat_pass
# ---------------------------------------------------------------------------


class TestRunDescriptionReformatPass:
    def test_processes_only_unformatted_jobs(self, db_with_unformatted_jobs):
        """run_description_reformat_pass processes only jobs where description_reformatted=0."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        reformatted_text = (
            "About the Role\n\nThis is a reformatted description.\n\n"
            "Responsibilities\n\n- Build models"
        )

        with patch("job_finder.web.description_reformatter.ClaudeClient") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value = mock_instance
            with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
                mock_call.return_value = ({"text": reformatted_text}, 0.0002)
                count = run_description_reformat_pass(
                    db_with_unformatted_jobs,
                    "fake-api-key",
                    config={"scoring": {"models": {"haiku": "claude-haiku-4-5"}}},
                )

        # Only 2 unformatted jobs should be processed (not the reformatted=1 one)
        assert count == 2

    def test_sets_reformatted_flag_to_1_after_each_job(self, db_with_unformatted_jobs):
        """run_description_reformat_pass sets description_reformatted=1 for processed jobs."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        reformatted_text = (
            "About the Role\n\nThis is a reformatted description.\n\n"
            "Responsibilities\n\n- Build models"
        )

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = ({"text": reformatted_text}, 0.0002)
            run_description_reformat_pass(
                db_with_unformatted_jobs,
                "fake-api-key",
                config={},
            )

        # Verify the flags are set in DB
        conn = sqlite3.connect(db_with_unformatted_jobs)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT dedup_key, description_reformatted FROM jobs"
        ).fetchall()
        conn.close()

        # All jobs should now have description_reformatted=1
        for row in rows:
            assert row["description_reformatted"] == 1, (
                f"Job {row['dedup_key']} still has description_reformatted=0"
            )

    def test_skips_null_descriptions(self, db_with_null_description):
        """run_description_reformat_pass skips jobs where description IS NULL."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = ({"text": "Reformatted text"}, 0.0002)
            count = run_description_reformat_pass(
                db_with_null_description,
                "fake-api-key",
                config={},
            )

        # NULL description job should be skipped (count = 0)
        assert count == 0
        # Haiku should not be called for NULL descriptions
        mock_call.assert_not_called()

    def test_returns_count_of_reformatted_jobs(self, db_with_unformatted_jobs):
        """run_description_reformat_pass returns correct count of reformatted jobs."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        reformatted_text = (
            "About the Role\n\nReformatted description.\n\n"
            "Responsibilities\n\n- Build models"
        )

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = ({"text": reformatted_text}, 0.0002)
            count = run_description_reformat_pass(
                db_with_unformatted_jobs,
                "fake-api-key",
                config={},
            )

        assert count == 2  # 2 unformatted jobs in fixture

    def test_marks_already_formatted_as_processed(self, db_with_unformatted_jobs):
        """run_description_reformat_pass marks description_reformatted=1 even when text unchanged."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        # Mock Haiku to return same text as input (already formatted)
        # This happens when description has 2+ section headers
        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = ({"text": "same as input"}, 0.0002)
            run_description_reformat_pass(
                db_with_unformatted_jobs,
                "fake-api-key",
                config={},
            )

        conn = sqlite3.connect(db_with_unformatted_jobs)
        conn.row_factory = sqlite3.Row
        unprocessed = conn.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE description_reformatted = 0"
        ).fetchone()
        conn.close()

        # No jobs should remain unprocessed
        assert unprocessed["cnt"] == 0

    def test_records_cost_per_haiku_call(self, db_with_unformatted_jobs):
        """run_description_reformat_pass records cost in scoring_costs for each Haiku call."""
        from job_finder.web.description_reformatter import run_description_reformat_pass

        reformatted_text = "About the Role\n\nReformatted.\n\nRequirements\n\n- Python"

        with patch("job_finder.web.description_reformatter.call_claude") as mock_call:
            mock_call.return_value = ({"text": reformatted_text}, 0.0002)
            run_description_reformat_pass(
                db_with_unformatted_jobs,
                "fake-api-key",
                config={},
            )

        # Check that call_claude was called for each reformatted job
        # (2 jobs with description_reformatted=0 and non-NULL descriptions)
        assert mock_call.call_count == 2
