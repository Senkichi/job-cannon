"""Unit tests for eval_provider.py pure functions and CLI orchestrator.

Covers:
- sample_jobs: DB query filtering, row structure
- reconstruct_prompt: system prompt, user message structure
- compute_metrics: Pearson r, schema adherence, latency stats, edge cases
- compute_verdict: SUITABLE/MARGINAL/NOT_RECOMMENDED mapping
- save_report: directory creation, JSON output structure
- run_eval: no-jobs exit path
- parse_args: defaults and custom argument parsing
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import eval_provider
from eval_provider import (
    compute_metrics,
    compute_verdict,
    parse_args,
    reconstruct_prompt,
    run_eval,
    sample_jobs,
    save_report,
)
from job_finder.web.sonnet_evaluator import _BASE_SYSTEM_PROMPT, _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_conn():
    """In-memory SQLite DB with jobs table and test rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            salary_min INTEGER,
            salary_max INTEGER,
            description TEXT,
            jd_full TEXT,
            sonnet_score REAL,
            fit_analysis TEXT,
            haiku_score INTEGER
        )"""
    )
    # Qualifying rows (sonnet_score IS NOT NULL AND jd_full IS NOT NULL)
    conn.executemany(
        "INSERT INTO jobs (dedup_key, title, company, location, salary_min, salary_max, "
        "jd_full, sonnet_score, fit_analysis, haiku_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("key1", "Senior Data Scientist", "Acme Corp", "Remote",
             90000, 130000, "Full JD text for job 1", 78.0, '{"strengths":[]}', 65),
            ("key2", "ML Engineer", "Beta LLC", "New York, NY",
             100000, 150000, "Full JD text for job 2", 65.0, '{"strengths":[]}', 55),
            ("key3", "Data Analyst", "Gamma Inc", "San Francisco, CA",
             70000, 100000, "Full JD text for job 3", 45.0, '{"strengths":[]}', 40),
        ],
    )
    # Non-qualifying rows (sonnet_score NULL or jd_full NULL)
    conn.executemany(
        "INSERT INTO jobs (dedup_key, title, company, location, jd_full, sonnet_score) VALUES (?,?,?,?,?,?)",
        [
            ("key4", "No Score Job", "Delta Co", "Chicago", "Has JD", None),
            ("key5", "No JD Job", "Epsilon Co", "Boston", None, 50.0),
        ],
    )
    yield conn
    conn.close()


@pytest.fixture
def empty_conn():
    """In-memory SQLite DB with jobs table but no qualifying rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            salary_min INTEGER,
            salary_max INTEGER,
            jd_full TEXT,
            sonnet_score REAL,
            fit_analysis TEXT,
            haiku_score INTEGER
        )"""
    )
    # Only non-qualifying rows
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, jd_full, sonnet_score) VALUES (?,?,?,?,?,?)",
        ("key_none", "No Score", "Co", "Anywhere", None, None),
    )
    yield conn
    conn.close()


@pytest.fixture
def sample_job_row():
    """A minimal job row dict for prompt reconstruction tests."""
    return {
        "dedup_key": "acme|senior-ds|remote",
        "title": "Senior Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "salary_min": 90000,
        "salary_max": 130000,
        "jd_full": "We are looking for a Senior Data Scientist to join our team.\n"
                   "Requirements: Python, SQL, ML frameworks.\n"
                   "Responsibilities: Build models, analyze data.",
        "sonnet_score": 78.0,
        "fit_analysis": None,
        "haiku_score": 65,
    }


@pytest.fixture
def sample_experience_profile():
    """A minimal experience profile dict."""
    return {
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "Previous Corp",
                "achievements": ["Built recommendation system", "Reduced churn by 15%"],
                "skills": ["Python", "SQL", "PyTorch"],
            }
        ],
        "skills": ["Python", "SQL", "Machine Learning", "PyTorch", "scikit-learn"],
        "education": [
            {
                "degree": "M.S. Computer Science",
                "institution": "State University",
                "graduation": "2019",
                "thesis": "Deep Learning for NLP",
            }
        ],
    }


@pytest.fixture
def sample_config():
    """A minimal config dict with profile section."""
    return {
        "profile": {
            "target_titles": ["Senior Data Scientist", "ML Engineer"],
            "target_locations": ["Remote", "New York"],
            "min_salary": 90000,
            "industries": ["Tech", "Finance"],
        },
        "scoring": {
            "models": {"sonnet": "claude-sonnet-4-6"},
        },
    }


# ---------------------------------------------------------------------------
# Tests: sample_jobs
# ---------------------------------------------------------------------------

class TestSampleJobs:
    def test_returns_at_most_n_rows(self, in_memory_conn):
        """Test 1: Returns at most n rows."""
        rows = sample_jobs(in_memory_conn, 2)
        assert len(rows) <= 2

    def test_returns_empty_list_when_no_qualifying_rows(self, empty_conn):
        """Test 2: Returns empty list when no qualifying rows exist."""
        rows = sample_jobs(empty_conn, 10)
        assert rows == []

    def test_returned_rows_have_required_keys(self, in_memory_conn):
        """Test 3: Each returned row contains all required keys."""
        rows = sample_jobs(in_memory_conn, 10)
        assert len(rows) > 0
        required_keys = {
            "dedup_key", "title", "company", "location",
            "salary_min", "salary_max", "jd_full",
            "sonnet_score", "fit_analysis", "haiku_score",
        }
        for row in rows:
            for key in required_keys:
                assert key in row, f"Missing key '{key}' in row: {dict(row)}"

    def test_only_returns_qualifying_rows(self, in_memory_conn):
        """Qualifying rows have sonnet_score IS NOT NULL AND jd_full IS NOT NULL."""
        rows = sample_jobs(in_memory_conn, 10)
        for row in rows:
            assert row["sonnet_score"] is not None
            assert row["jd_full"] is not None

    def test_returns_dicts_not_sqlite_rows(self, in_memory_conn):
        """sample_jobs returns list of plain dicts, not sqlite3.Row objects."""
        rows = sample_jobs(in_memory_conn, 1)
        assert len(rows) > 0
        assert isinstance(rows[0], dict)


# ---------------------------------------------------------------------------
# Tests: reconstruct_prompt
# ---------------------------------------------------------------------------

class TestReconstructPrompt:
    def test_returns_tuple_of_two_strings(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 4: Returns (system_prompt, user_message) tuple."""
        result = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert isinstance(result, tuple)
        assert len(result) == 2
        system_prompt, user_message = result
        assert isinstance(system_prompt, str)
        assert isinstance(user_message, str)

    def test_system_prompt_matches_sonnet_evaluator(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 5: default variant system_prompt matches _BASE_SYSTEM_PROMPT from sonnet_evaluator.

        The 'default' eval variant maps to _BASE_SYSTEM_PROMPT (plain prompt without fewshot),
        preserving the legacy eval baseline behavior. Production scoring uses _SYSTEM_PROMPT
        (which includes fewshot examples) via the 'fewshot' variant.
        """
        system_prompt, _ = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert system_prompt == _BASE_SYSTEM_PROMPT

    def test_user_message_contains_job_title(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 6a: user_message contains job title."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Senior Data Scientist" in user_message

    def test_user_message_contains_company(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 6b: user_message contains company name."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Acme Corp" in user_message

    def test_user_message_contains_location(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 6c: user_message contains location."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Remote" in user_message

    def test_user_message_contains_salary(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 6d: user_message contains salary."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "90,000" in user_message
        assert "130,000" in user_message

    def test_user_message_contains_jd_full(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 6e: user_message contains jd_full text."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Senior Data Scientist to join our team" in user_message

    def test_user_message_contains_candidate_skills(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 7a: user_message contains candidate skills."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Python" in user_message
        assert "SQL" in user_message

    def test_user_message_contains_positions(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 7b: user_message contains candidate positions."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Previous Corp" in user_message

    def test_user_message_contains_education(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 7c: user_message contains candidate education."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "State University" in user_message

    def test_user_message_contains_target_titles(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 8a: user_message contains target_titles from config profile."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "ML Engineer" in user_message

    def test_user_message_contains_target_locations(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 8b: user_message contains target_locations from config profile."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "New York" in user_message

    def test_user_message_contains_min_salary(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 8c: user_message contains min_salary preference."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "90,000" in user_message

    def test_user_message_contains_industries(self, sample_job_row, sample_experience_profile, sample_config):
        """Test 8d: user_message contains industries preference."""
        _, user_message = reconstruct_prompt(sample_job_row, sample_experience_profile, sample_config)
        assert "Finance" in user_message


# ---------------------------------------------------------------------------
# Tests: compute_metrics
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def _make_results(self, pairs, schema_valids=None, latencies=None):
        """Helper: build results list from (baseline, eval) pairs."""
        if schema_valids is None:
            schema_valids = [True] * len(pairs)
        if latencies is None:
            latencies = [1.0] * len(pairs)
        results = []
        for i, (baseline, eval_score) in enumerate(pairs):
            results.append({
                "baseline_score": baseline,
                "eval_score": eval_score,
                "schema_valid": schema_valids[i],
                "latency_seconds": latencies[i],
            })
        return results

    def test_returns_required_keys(self):
        """Test 9: Returns dict with required keys."""
        results = self._make_results([(78, 74), (65, 60)])
        metrics = compute_metrics(results)
        assert "score_correlation" in metrics
        assert "schema_adherence_rate" in metrics
        assert "median_latency_seconds" in metrics
        assert "mean_latency_seconds" in metrics

    def test_pearson_r_computed_correctly(self):
        """Test 10: score_correlation is Pearson r between baseline and eval scores."""
        # Perfect correlation: both series identical
        results = self._make_results([(70, 70), (80, 80), (60, 60)])
        metrics = compute_metrics(results)
        assert metrics["score_correlation"] == pytest.approx(1.0, abs=1e-9)

    def test_correlation_none_when_fewer_than_2_pairs(self):
        """Test 11: score_correlation is None when fewer than 2 valid score pairs."""
        # Only 1 valid pair (the other has None eval_score)
        results = self._make_results([(78, 74), (65, None)])
        metrics = compute_metrics(results)
        assert metrics["score_correlation"] is None

    def test_correlation_none_when_zero_variance(self):
        """Test 12: score_correlation is None when all eval scores are identical."""
        # All eval scores identical => zero variance => StatisticsError
        results = self._make_results([(70, 50), (80, 50), (60, 50)])
        metrics = compute_metrics(results)
        assert metrics["score_correlation"] is None

    def test_schema_adherence_rate_all_valid(self):
        """Test 13a: schema_adherence_rate when all valid."""
        results = self._make_results([(70, 65), (80, 75)], schema_valids=[True, True])
        metrics = compute_metrics(results)
        assert metrics["schema_adherence_rate"] == pytest.approx(1.0)

    def test_schema_adherence_rate_partial(self):
        """Test 13b: schema_adherence_rate is count(schema_valid=True) / total."""
        results = self._make_results(
            [(70, 65), (80, 75), (60, 55)],
            schema_valids=[True, False, True],
        )
        metrics = compute_metrics(results)
        assert metrics["schema_adherence_rate"] == pytest.approx(2 / 3)

    def test_latency_stats_computed(self):
        """Test 14: median and mean latency computed correctly."""
        results = self._make_results(
            [(70, 65), (80, 75), (90, 85)],
            latencies=[2.0, 4.0, 6.0],
        )
        metrics = compute_metrics(results)
        assert metrics["median_latency_seconds"] == pytest.approx(4.0)
        assert metrics["mean_latency_seconds"] == pytest.approx(4.0)

    def test_correlation_none_when_no_valid_pairs(self):
        """Edge: all eval_scores are None => 0 valid pairs => None."""
        results = self._make_results([(70, None), (80, None)])
        metrics = compute_metrics(results)
        assert metrics["score_correlation"] is None


# ---------------------------------------------------------------------------
# Tests: compute_verdict
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {
    "correlation_suitable": 0.85,
    "correlation_marginal": 0.70,
    "adherence_suitable": 0.95,
    "adherence_marginal": 0.80,
}


class TestComputeVerdict:
    def test_suitable_when_all_thresholds_met(self):
        """Test 15: Returns SUITABLE when pearson_r >= 0.85 and adherence >= 0.95."""
        verdict = compute_verdict(0.90, 0.96, DEFAULT_THRESHOLDS)
        assert verdict == "SUITABLE"

    def test_marginal_when_correlation_marginal_adherence_marginal(self):
        """Test 16: Returns MARGINAL when pearson_r >= 0.70 but < 0.85, adherence >= 0.80."""
        verdict = compute_verdict(0.75, 0.85, DEFAULT_THRESHOLDS)
        assert verdict == "MARGINAL"

    def test_not_recommended_when_adherence_below_marginal(self):
        """Test 17: Returns NOT_RECOMMENDED when adherence < 0.80."""
        verdict = compute_verdict(0.90, 0.75, DEFAULT_THRESHOLDS)
        assert verdict == "NOT_RECOMMENDED"

    def test_not_recommended_when_pearson_r_is_none(self):
        """Test 18: Returns NOT_RECOMMENDED when pearson_r is None."""
        verdict = compute_verdict(None, 0.96, DEFAULT_THRESHOLDS)
        assert verdict == "NOT_RECOMMENDED"

    def test_custom_thresholds_respected(self):
        """Test 19: Respects custom thresholds passed in thresholds dict."""
        # Lower thresholds: correlation_suitable=0.70, adherence_suitable=0.80
        custom_thresholds = {
            "correlation_suitable": 0.70,
            "correlation_marginal": 0.50,
            "adherence_suitable": 0.80,
            "adherence_marginal": 0.60,
        }
        # pearson_r=0.72 => meets custom suitable threshold (0.70)
        # adherence=0.82 => meets custom suitable threshold (0.80)
        verdict = compute_verdict(0.72, 0.82, custom_thresholds)
        assert verdict == "SUITABLE"

    def test_not_recommended_when_correlation_below_marginal(self):
        """Edge: pearson_r below marginal threshold => NOT_RECOMMENDED."""
        verdict = compute_verdict(0.65, 0.90, DEFAULT_THRESHOLDS)
        assert verdict == "NOT_RECOMMENDED"

    def test_marginal_when_adherence_between_thresholds(self):
        """Edge: adherence >= 0.80 but < 0.95, correlation suitable => MARGINAL."""
        verdict = compute_verdict(0.90, 0.88, DEFAULT_THRESHOLDS)
        assert verdict == "MARGINAL"


# ---------------------------------------------------------------------------
# Tests: save_report
# ---------------------------------------------------------------------------

class TestSaveReport:
    def test_creates_output_dir_if_not_exists(self, tmp_path):
        """Test 20: Creates output_dir if it does not exist."""
        output_dir = tmp_path / "new_eval_dir"
        assert not output_dir.exists()
        report = {"meta": {}, "aggregate": {}, "per_job": []}
        save_report(report, "gemini", output_dir=str(output_dir))
        assert output_dir.exists()

    def test_writes_json_file(self, tmp_path):
        """Test 21: Writes valid JSON file to output_dir/{provider}_{timestamp}.json."""
        report = {"meta": {"provider": "gemini"}, "aggregate": {}, "per_job": []}
        output_path = save_report(report, "gemini", output_dir=str(tmp_path))
        assert output_path.exists()
        assert output_path.suffix == ".json"
        assert "gemini_" in output_path.name
        # File contains valid JSON
        loaded = json.loads(output_path.read_text())
        assert loaded["meta"]["provider"] == "gemini"

    def test_report_json_contains_required_keys(self, tmp_path):
        """Test 22: Report JSON contains meta, aggregate, and per_job top-level keys."""
        report = {
            "meta": {"provider": "ollama", "model": "mistral", "sample_size": 5},
            "aggregate": {"score_correlation": 0.82, "verdict": "MARGINAL"},
            "per_job": [{"dedup_key": "k1", "baseline_score": 70, "eval_score": 68}],
        }
        output_path = save_report(report, "ollama", output_dir=str(tmp_path))
        loaded = json.loads(output_path.read_text())
        assert "meta" in loaded
        assert "aggregate" in loaded
        assert "per_job" in loaded

    def test_returns_path_object(self, tmp_path):
        """save_report returns a Path to the written file."""
        report = {"meta": {}, "aggregate": {}, "per_job": []}
        result = save_report(report, "gemini", output_dir=str(tmp_path))
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# Tests: run_eval (integration — no-jobs exit path)
# ---------------------------------------------------------------------------


class TestRunEval:
    def test_run_eval_no_jobs_exits(self, tmp_path):
        """run_eval calls sys.exit(1) when no Sonnet-scored jobs exist in DB."""
        # Build minimal config and profile stubs
        mock_config = {
            "db": {"path": str(tmp_path / "jobs.db")},
            "profile": {},
            "scoring": {"models": {"sonnet": "claude-sonnet-4-6"}},
        }
        mock_profile = {"positions": [], "skills": [], "education": []}
        mock_thresholds = {
            "correlation_suitable": 0.85,
            "correlation_marginal": 0.70,
            "adherence_suitable": 0.95,
            "adherence_marginal": 0.80,
        }

        # In-memory connection with no qualifying rows
        empty_conn = sqlite3.connect(":memory:")
        empty_conn.row_factory = sqlite3.Row
        empty_conn.execute(
            """CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT NOT NULL,
                salary_min INTEGER,
                salary_max INTEGER,
                jd_full TEXT,
                sonnet_score REAL,
                fit_analysis TEXT,
                haiku_score INTEGER
            )"""
        )

        @contextmanager
        def mock_standalone_conn(_db_path):
            yield empty_conn

        with (
            patch("eval_provider.load_config", return_value=mock_config),
            patch("eval_provider.load_profile", return_value=mock_profile),
            patch("eval_provider.standalone_connection", mock_standalone_conn),
            pytest.raises(SystemExit) as exc_info,
        ):
            run_eval(
                provider="gemini",
                model=None,
                sample_size=10,
                thresholds=mock_thresholds,
                skip_confirm=True,
            )

        assert exc_info.value.code == 1

        empty_conn.close()


# ---------------------------------------------------------------------------
# Tests: parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_parse_args_defaults(self):
        """parse_args with only --provider uses correct defaults."""
        args = parse_args(["--provider", "gemini"])
        assert args.provider == "gemini"
        assert args.model is None
        assert args.sample_size == 20
        assert args.correlation_suitable == pytest.approx(0.85)
        assert args.correlation_marginal == pytest.approx(0.70)
        assert args.adherence_suitable == pytest.approx(0.95)
        assert args.adherence_marginal == pytest.approx(0.80)
        assert args.yes is False

    def test_parse_args_custom(self):
        """parse_args correctly parses all custom arguments."""
        args = parse_args([
            "--provider", "ollama",
            "--model", "llama3",
            "--sample-size", "5",
            "--correlation-suitable", "0.90",
            "--correlation-marginal", "0.75",
            "--adherence-suitable", "0.98",
            "--adherence-marginal", "0.85",
            "--yes",
        ])
        assert args.provider == "ollama"
        assert args.model == "llama3"
        assert args.sample_size == 5
        assert args.correlation_suitable == pytest.approx(0.90)
        assert args.correlation_marginal == pytest.approx(0.75)
        assert args.adherence_suitable == pytest.approx(0.98)
        assert args.adherence_marginal == pytest.approx(0.85)
        assert args.yes is True

    def test_parse_args_requires_provider(self):
        """parse_args raises SystemExit when --provider is missing."""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parse_args_prompt_variant_default(self):
        args = parse_args(["--provider", "gemini"])
        assert args.prompt_variant == "default"

    def test_parse_args_prompt_variant_rubric(self):
        args = parse_args(["--provider", "gemini", "--prompt-variant", "rubric"])
        assert args.prompt_variant == "rubric"

    def test_parse_args_prompt_variant_fewshot(self):
        args = parse_args(["--provider", "gemini", "--prompt-variant", "fewshot"])
        assert args.prompt_variant == "fewshot"

    def test_parse_args_prompt_variant_fewshot_rubric(self):
        args = parse_args(["--provider", "gemini", "--prompt-variant", "fewshot-rubric"])
        assert args.prompt_variant == "fewshot-rubric"

    def test_parse_args_baseline_default(self):
        args = parse_args(["--provider", "gemini"])
        assert args.baseline == "sonnet"

    def test_parse_args_baseline_opus(self):
        args = parse_args(["--provider", "gemini", "--baseline", "opus"])
        assert args.baseline == "opus"


# ---------------------------------------------------------------------------
# Prompt Variant Tests
# ---------------------------------------------------------------------------


class TestPromptVariants:

    @pytest.fixture
    def prompt_inputs(self):
        job_row = {
            "title": "Data Scientist", "company": "TestCo", "location": "Remote",
            "salary_min": 100000, "salary_max": 150000, "jd_full": "Full JD text",
        }
        profile = {"positions": [], "skills": ["Python"], "education": []}
        config = {"profile": {"target_titles": ["Data Scientist"], "target_locations": ["Remote"],
                              "min_salary": 100000, "industries": ["Tech"]}}
        return job_row, profile, config

    def test_default_returns_original_system_prompt(self, prompt_inputs):
        """The 'default' eval variant returns _BASE_SYSTEM_PROMPT (plain prompt without fewshot)."""
        job, profile, config = prompt_inputs
        system, _ = reconstruct_prompt(job, profile, config, prompt_variant="default")
        assert system == _BASE_SYSTEM_PROMPT

    def test_rubric_includes_scoring_rubric(self, prompt_inputs):
        job, profile, config = prompt_inputs
        system, _ = reconstruct_prompt(job, profile, config, prompt_variant="rubric")
        assert "Scoring Rubric" in system
        assert "90-100" in system
        assert system != _SYSTEM_PROMPT

    def test_fewshot_includes_calibration_examples(self, prompt_inputs):
        job, profile, config = prompt_inputs
        system, _ = reconstruct_prompt(job, profile, config, prompt_variant="fewshot")
        assert "Calibration Examples" in system
        assert "Score 15" in system
        assert "Score 91" in system

    def test_fewshot_rubric_includes_both(self, prompt_inputs):
        job, profile, config = prompt_inputs
        system, _ = reconstruct_prompt(job, profile, config, prompt_variant="fewshot-rubric")
        assert "Scoring Rubric" in system
        assert "Calibration Examples" in system

    def test_all_variants_produce_same_user_message(self, prompt_inputs):
        job, profile, config = prompt_inputs
        variants = ["default", "rubric", "fewshot", "fewshot-rubric"]
        messages = []
        for v in variants:
            _, msg = reconstruct_prompt(job, profile, config, prompt_variant=v)
            messages.append(msg)
        assert all(m == messages[0] for m in messages)

    def test_unknown_variant_falls_back_to_default(self, prompt_inputs):
        job, profile, config = prompt_inputs
        system, _ = reconstruct_prompt(job, profile, config, prompt_variant="nonexistent")
        assert system == _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Baseline Selection Tests
# ---------------------------------------------------------------------------


class TestBaselineSelection:

    def test_sample_jobs_opus_filters_by_opus_score(self):
        """baseline='opus' only returns jobs with opus_score IS NOT NULL."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs ("
            "  dedup_key TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,"
            "  location TEXT NOT NULL, salary_min INTEGER, salary_max INTEGER,"
            "  jd_full TEXT, sonnet_score REAL, fit_analysis TEXT, haiku_score INTEGER,"
            "  opus_score REAL)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("k1", "T1", "C1", "L1", 80000, 120000, "JD1", 70.0, "{}", 60, 72.0),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("k2", "T2", "C2", "L2", 80000, 120000, "JD2", 65.0, "{}", 55, None),
        )
        rows = sample_jobs(conn, 10, baseline="opus")
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == "k1"
        assert "opus_score" in rows[0]
        conn.close()

    def test_sample_jobs_sonnet_default(self, in_memory_conn):
        """Default baseline='sonnet' works as before."""
        rows = sample_jobs(in_memory_conn, 10, baseline="sonnet")
        assert len(rows) == 3  # 3 qualifying rows in fixture
