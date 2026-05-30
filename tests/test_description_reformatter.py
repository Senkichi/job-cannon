"""Tests for description_reformatter.py — cascade-routed description reformatting.

Tests cover:
- reformat_description routes through call_model when conn is provided.
- reformat_description falls back to call_claude when call_model raises
  ProviderCascadeExhaustedError, and when conn is None (skip-dispatcher guard).
- Skips empty / None / already-formatted descriptions without any LLM call.
- Graceful degradation: returns original text on any unexpected exception.
- run_description_reformat_pass processes only rows where description_reformatted=0,
  marks every processed row as reformatted=1, leaves NULL descriptions alone.

The dispatcher-vs-direct split is governed by ``use_dispatcher = conn is not None``
inside reformat_description: with a conn, call_model runs first and call_claude is
the cascade-exhaustion fallback; without a conn, call_model is skipped entirely
because cost recording requires the DB handle.
"""

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_db_path():
    """Temp SQLite with the minimal jobs + scoring_costs schema reformatter touches."""
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
    """DB with 2 unformatted jobs (reformatted=0) and 1 already-formatted (reformatted=1)."""
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
                0,
            ),
            (
                "beta|sds|sf",
                "Staff Data Scientist",
                "Beta Inc",
                "Lead experiments. Run A/B tests. Mentor junior scientists.",
                0,
            ),
            (
                "gamma|ds|nyc",
                "Data Scientist",
                "Gamma Corp",
                "About the Role\n\nWe are looking for...\n\nRequirements\n\n- Python skills",
                1,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return temp_db_path


@pytest.fixture
def db_with_null_description(temp_db_path):
    """DB with a single job whose description IS NULL."""
    conn = sqlite3.connect(temp_db_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, description, description_reformatted) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acme|ds|remote", "Data Scientist", "Acme Corp", None, 0),
    )
    conn.commit()
    conn.close()
    return temp_db_path


_REFORMATTED = (
    "About the Role\n\nWe are looking for a Data Scientist.\n\n"
    "Responsibilities\n\n- Build models\n- Deploy pipelines\n"
)


# ---------------------------------------------------------------------------
# Tests: reformat_description — short-circuit paths (no LLM call)
# ---------------------------------------------------------------------------


class TestReformatDescriptionShortCircuit:
    """Inputs that should never reach the LLM."""

    def test_returns_none_unchanged(self):
        from job_finder.web.description_reformatter import reformat_description

        with patch("job_finder.web.description_reformatter.call_claude") as mock_cc:
            assert reformat_description(None) is None
        mock_cc.assert_not_called()

    def test_returns_empty_unchanged(self):
        from job_finder.web.description_reformatter import reformat_description

        with patch("job_finder.web.description_reformatter.call_claude") as mock_cc:
            assert reformat_description("") == ""
        mock_cc.assert_not_called()

    def test_skips_already_formatted_with_two_plus_section_headers(self):
        from job_finder.web.description_reformatter import reformat_description

        already_formatted = (
            "About the Role\n\n"
            "We are looking for a Data Scientist.\n\n"
            "Requirements\n\n"
            "- Python skills\n"
            "- ML experience\n"
        )

        with (
            patch("job_finder.web.description_reformatter.call_model") as mock_cm,
            patch("job_finder.web.description_reformatter.call_claude") as mock_cc,
        ):
            result = reformat_description(already_formatted)

        mock_cm.assert_not_called()
        mock_cc.assert_not_called()
        assert result == already_formatted


# ---------------------------------------------------------------------------
# Tests: reformat_description — cascade dispatch (conn provided)
# ---------------------------------------------------------------------------


_CASCADE_CONFIG_QUICK: dict = {
    "providers": {
        "quick": {
            "provider": "ollama",
            "model": "qwen2.5:14b",
            "fallback_chain": [
                {"provider": "anthropic", "model": "claude-haiku-4-5"},
            ],
        },
    },
}


_RAW_DESC = "Build ML models | Deploy pipelines | Monitor performance"


class TestReformatDescriptionCascade:
    """With conn provided, reformat_description must route through call_model first
    and only fall back to call_claude on ProviderCascadeExhaustedError."""

    def test_uses_call_model_when_conn_provided(self, temp_db_path, make_model_result):
        from job_finder.web.description_reformatter import reformat_description

        conn = sqlite3.connect(temp_db_path)
        try:
            with (
                patch("job_finder.web.description_reformatter.call_model") as mock_cm,
                patch("job_finder.web.description_reformatter.call_claude") as mock_cc,
            ):
                mock_cm.return_value = make_model_result({"text": _REFORMATTED})
                result = reformat_description(_RAW_DESC, conn=conn, config=_CASCADE_CONFIG_QUICK)

            mock_cm.assert_called_once()
            assert mock_cm.call_args.kwargs["tier"] == "quick"
            assert mock_cm.call_args.kwargs["purpose"] == "description_reformat"
            mock_cc.assert_not_called()
            assert result is not None and result.startswith("About the Role")
        finally:
            conn.close()

    def test_cascade_exhausted_falls_back_to_call_claude(self, temp_db_path):
        from job_finder.web.description_reformatter import reformat_description
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        conn = sqlite3.connect(temp_db_path)
        try:
            with (
                patch("job_finder.web.description_reformatter.call_model") as mock_cm,
                patch("job_finder.web.description_reformatter.call_claude") as mock_cc,
            ):
                mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
                # call_claude returns 3-tuple (result, cost, schema_valid)
                mock_cc.return_value = ({"text": "CLI fallback reformatted text"}, 0.0002, True)
                result = reformat_description(_RAW_DESC, conn=conn, config=_CASCADE_CONFIG_QUICK)

            mock_cm.assert_called_once()
            mock_cc.assert_called_once()
            assert result == "CLI fallback reformatted text"
        finally:
            conn.close()

    def test_cascade_and_cli_both_fail_returns_original(self, temp_db_path):
        from job_finder.web.description_reformatter import reformat_description
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        conn = sqlite3.connect(temp_db_path)
        try:
            with (
                patch("job_finder.web.description_reformatter.call_model") as mock_cm,
                patch("job_finder.web.description_reformatter.call_claude") as mock_cc,
            ):
                mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
                mock_cc.side_effect = RuntimeError("CLI unavailable")
                result = reformat_description(_RAW_DESC, conn=conn, config=_CASCADE_CONFIG_QUICK)

            assert result == _RAW_DESC
        finally:
            conn.close()

    def test_conn_none_skips_dispatcher_and_goes_direct_to_call_claude(self):
        """conn=None must NOT route through call_model — cost recording inside
        call_model needs the DB handle. Production guards via
        ``use_dispatcher = conn is not None`` and falls straight to call_claude."""
        from job_finder.web.description_reformatter import reformat_description

        with (
            patch("job_finder.web.description_reformatter.call_model") as mock_cm,
            patch("job_finder.web.description_reformatter.call_claude") as mock_cc,
        ):
            mock_cc.return_value = ({"text": "Reformatted via CLI"}, 0.0002, True)
            result = reformat_description(_RAW_DESC, conn=None, config=_CASCADE_CONFIG_QUICK)

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result == "Reformatted via CLI"

    def test_returns_original_when_call_claude_raises_without_conn(self):
        """conn=None path raises through outer except → graceful return of original."""
        from job_finder.web.description_reformatter import reformat_description

        with patch("job_finder.web.description_reformatter.call_claude") as mock_cc:
            mock_cc.side_effect = Exception("CLI error")
            result = reformat_description(_RAW_DESC)

        assert result == _RAW_DESC


# ---------------------------------------------------------------------------
# Tests: run_description_reformat_pass — DB iteration + flag updates
# ---------------------------------------------------------------------------


class TestRunDescriptionReformatPass:
    """The pass opens its own connection, so reformat_description always sees
    conn != None and routes through call_model. Patching call_model is enough."""

    def test_processes_only_unformatted_jobs(self, db_with_unformatted_jobs, make_model_result):
        from job_finder.web.description_reformatter import run_description_reformat_pass

        with patch("job_finder.web.description_reformatter.call_model") as mock_cm:
            mock_cm.return_value = make_model_result({"text": _REFORMATTED})
            count = run_description_reformat_pass(
                db_with_unformatted_jobs, config=_CASCADE_CONFIG_QUICK
            )

        assert count == 2  # only the 2 reformatted=0 rows
        assert mock_cm.call_count == 2  # one call per unformatted row

    def test_sets_reformatted_flag_for_every_processed_job(
        self, db_with_unformatted_jobs, make_model_result
    ):
        from job_finder.web.description_reformatter import run_description_reformat_pass

        with patch("job_finder.web.description_reformatter.call_model") as mock_cm:
            mock_cm.return_value = make_model_result({"text": _REFORMATTED})
            run_description_reformat_pass(db_with_unformatted_jobs, config=_CASCADE_CONFIG_QUICK)

        conn = sqlite3.connect(db_with_unformatted_jobs)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT dedup_key, description_reformatted FROM jobs").fetchall()
        conn.close()

        for row in rows:
            assert row["description_reformatted"] == 1, (
                f"Job {row['dedup_key']} still has description_reformatted=0"
            )

    def test_skips_null_descriptions(self, db_with_null_description, make_model_result):
        from job_finder.web.description_reformatter import run_description_reformat_pass

        with patch("job_finder.web.description_reformatter.call_model") as mock_cm:
            mock_cm.return_value = make_model_result({"text": "should not be used"})
            count = run_description_reformat_pass(
                db_with_null_description, config=_CASCADE_CONFIG_QUICK
            )

        assert count == 0
        mock_cm.assert_not_called()

    def test_marks_already_formatted_as_processed_even_if_text_unchanged(
        self, db_with_unformatted_jobs
    ):
        """When the LLM returns text equal to the input, the row is still marked
        reformatted=1 so the pass does not retry it next run."""
        from job_finder.web.description_reformatter import run_description_reformat_pass
        from job_finder.web.model_provider import ModelResult

        # The "About the Role / Requirements" row has 2+ section headers and
        # skips the LLM via _ALREADY_FORMATTED_THRESHOLD; the two messy rows
        # will call the LLM. Echo their inputs back to exercise the
        # "text unchanged but still mark processed" branch.
        def _echo(**kwargs):
            return ModelResult(
                data={"text": kwargs["messages"][0]["content"]},
                provider="ollama",
                model="qwen2.5:14b",
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                schema_valid=True,
            )

        with patch("job_finder.web.description_reformatter.call_model") as mock_cm:
            mock_cm.side_effect = _echo
            run_description_reformat_pass(db_with_unformatted_jobs, config=_CASCADE_CONFIG_QUICK)

        conn = sqlite3.connect(db_with_unformatted_jobs)
        unprocessed = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE description_reformatted = 0"
        ).fetchone()[0]
        conn.close()

        assert unprocessed == 0
