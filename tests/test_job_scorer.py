"""Tests for job_finder.web.job_scorer (Phase 34 Plan 1).

D-28 carry-forward: Byte-identical determinism is not achievable on the
local Ollama + CUDA stack below Ollama's abstraction. Phase 33's probe
showed 2 of 3 fixtures drift on repeated temperature=0 seed=42 runs.
The success criterion for v3 scoring is ordinal stability — axis rankings
preserved across invocations — NOT byte-equality. No byte-identical test
in this file; rescore gates (Plan 4 G1-G4) capture the same intent via
G3 correlation across the full baseline.

Plan 1 scope: score_job is a pure-function addition — no production
callers yet. Plan 2's scoring_orchestrator is the first caller. Tests
mock call_model() directly so they do not require Ollama/network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_finder.db import JobAssessment
from job_finder.web.job_scorer import (
    JOB_ASSESSMENT_SCHEMA,
    ScoringResult,
    _coerce_assessment,
    score_job,
)
from job_finder.web.model_provider import ModelResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _good_response_data() -> dict:
    """A schema-valid v3 response dict matching JOB_ASSESSMENT_SCHEMA."""
    return {
        "title_fit": 5,
        "location_fit": 4,
        "comp_fit": 4,
        "domain_match": 3,
        "seniority_match": 5,
        "skills_match": 4,
        "rationale": {
            "strengths": ["Python expert", "ML background"],
            "gaps": ["No Kubernetes"],
            "talking_points": ["Led a 6-person platform team"],
            "resume_priority_skills": ["Python", "PyTorch"],
        },
        "legitimacy_note": None,
    }


def _good_model_result(provider: str = "ollama") -> ModelResult:
    """Build a ModelResult the dispatcher would return on success."""
    return ModelResult(
        data=_good_response_data(),
        cost_usd=0.0,
        input_tokens=500,
        output_tokens=200,
        model="qwen2.5:14b",
        provider=provider,
        schema_valid=True,
    )


def _good_job() -> dict:
    """Minimal job dict with a non-empty jd_full."""
    return {
        "dedup_key": "acme|senior-ml-engineer",
        "title": "Senior ML Engineer",
        "company": "Acme Corp",
        "company_canonical": "acme corp",
        "location": "Remote US",
        "salary_min": 180000,
        "salary_max": 260000,
        "jd_full": "Build scalable ML platforms. Python, PyTorch, AWS.",
    }


@pytest.fixture
def mock_conn():
    """Stand-in conn — score_job does not write to it directly."""
    return MagicMock()


@pytest.fixture
def config():
    """Minimal config dict — resolver inherits from peer tier if scoring absent."""
    return {"providers": {}}


# ---------------------------------------------------------------------------
# Tests: SCORER-05 skip precondition
# ---------------------------------------------------------------------------


class TestSkipPrecondition:
    """score_job returns status='skipped' when jd_full is empty/None (SCORER-05)."""

    def test_skips_on_empty_jd_full(self, mock_conn, config):
        """Empty string jd_full -> skipped, no call_model invocation."""
        job = _good_job()
        job["jd_full"] = ""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            result = score_job(job, mock_conn, config)
        assert result.status == "skipped"
        assert result.data is None
        assert result.provider is None
        mock_call.assert_not_called()

    def test_skips_on_none_jd_full(self, mock_conn, config):
        """None jd_full -> skipped, no call_model invocation."""
        job = _good_job()
        job["jd_full"] = None
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            result = score_job(job, mock_conn, config)
        assert result.status == "skipped"
        assert result.data is None
        mock_call.assert_not_called()

    def test_skips_on_missing_jd_full_key(self, mock_conn, config):
        """Missing jd_full key entirely -> skipped, no call_model invocation."""
        job = _good_job()
        del job["jd_full"]
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            result = score_job(job, mock_conn, config)
        assert result.status == "skipped"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: happy path + dispatcher routing
# ---------------------------------------------------------------------------


class TestHappyPath:
    """score_job routes through call_model(tier='scoring', ...) and returns JobAssessment."""

    def test_happy_path_returns_job_assessment(self, mock_conn, config):
        """Valid job + valid model result -> status='ok' with populated JobAssessment."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result(provider="ollama")
            result = score_job(_good_job(), mock_conn, config)

        assert result.status == "ok"
        assert isinstance(result.data, JobAssessment)
        assert result.provider == "ollama"
        assert result.error is None

    def test_assessment_has_all_six_sub_scores(self, mock_conn, config):
        """JobAssessment.sub_scores has all 6 D-05 keys as integers."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            result = score_job(_good_job(), mock_conn, config)

        assert result.data is not None
        for key in (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ):
            assert key in result.data.sub_scores, f"Missing sub-score key: {key}"
            assert isinstance(result.data.sub_scores[key], int)

    def test_assessment_rationale_has_d03_keys(self, mock_conn, config):
        """JobAssessment.rationale has all 4 keys from the v3 schema."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            result = score_job(_good_job(), mock_conn, config)

        assert result.data is not None
        rationale = result.data.rationale
        for key in ("strengths", "gaps", "talking_points", "resume_priority_skills"):
            assert key in rationale, f"Missing rationale key: {key}"

    def test_assessment_classification_is_sentinel_empty_string(self, mock_conn, config):
        """score_job leaves classification='' — persist_job_assessment derives it."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            result = score_job(_good_job(), mock_conn, config)

        assert result.data is not None
        # The classification field is a sentinel; real value is derived at persist time.
        assert result.data.classification == ""

    def test_call_model_invoked_with_tier_score(self, mock_conn, config):
        """call_model is called with tier='score' (renamed from 'scoring' in commit abeecf9)."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            score_job(_good_job(), mock_conn, config)

        assert mock_call.call_count == 1
        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("tier") == "score"

    def test_call_model_invoked_with_job_assessment_schema(self, mock_conn, config):
        """call_model receives output_schema=JOB_ASSESSMENT_SCHEMA (identity-equal)."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            score_job(_good_job(), mock_conn, config)

        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("output_schema") is JOB_ASSESSMENT_SCHEMA

    def test_system_prompt_contains_v3_content(self, mock_conn, config):
        """The system arg passed to call_model contains v3 prompt + fewshots + reinforcement."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            score_job(_good_job(), mock_conn, config)

        kwargs = mock_call.call_args.kwargs
        system = kwargs.get("system", "")
        # FIELD_REINFORCEMENT has a distinctive marker: "STRICT FIELD NAMES"
        assert "STRICT FIELD NAMES" in system, "system prompt missing FIELD_REINFORCEMENT"
        # FEWSHOT_EXAMPLES has a distinctive marker: "Fewshot calibration"
        assert "Fewshot calibration" in system, "system prompt missing FEWSHOT_EXAMPLES"

    def test_user_message_contains_job_content(self, mock_conn, config):
        """The user message includes title, company, location, and jd_full."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            score_job(_good_job(), mock_conn, config)

        kwargs = mock_call.call_args.kwargs
        messages = kwargs.get("messages") or []
        assert len(messages) == 1
        content = messages[0].get("content", "")
        assert "Senior ML Engineer" in content
        assert "Acme Corp" in content or "acme corp" in content
        assert "Remote US" in content
        assert "Build scalable ML platforms" in content  # jd_full excerpt

    def test_job_id_is_dedup_key(self, mock_conn, config):
        """job_id passed to call_model is the job's dedup_key (str)."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.return_value = _good_model_result()
            score_job(_good_job(), mock_conn, config)

        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("job_id") == "acme|senior-ml-engineer"


# ---------------------------------------------------------------------------
# Tests: error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """score_job returns status='error' when dispatcher fails or returns invalid data."""

    def test_dispatcher_exception_returns_error(self, mock_conn, config):
        """Exception in call_model -> ScoringResult(status='error') with reason."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            mock_call.side_effect = RuntimeError("ollama timeout")
            result = score_job(_good_job(), mock_conn, config)

        assert result.status == "error"
        assert result.data is None
        assert result.error is not None
        assert "ollama timeout" in result.error

    def test_schema_invalid_result_returns_error(self, mock_conn, config):
        """schema_valid=False on the ModelResult -> status='error'."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            bad = ModelResult(
                data={"title_fit": 5},  # incomplete
                cost_usd=0.0,
                input_tokens=100,
                output_tokens=10,
                model="qwen2.5:14b",
                provider="ollama",
                schema_valid=False,
            )
            mock_call.return_value = bad
            result = score_job(_good_job(), mock_conn, config)

        assert result.status == "error"
        assert result.provider == "ollama"

    def test_empty_data_returns_error(self, mock_conn, config):
        """Empty data dict -> status='error'."""
        with patch("job_finder.web.job_scorer.call_model") as mock_call:
            empty = ModelResult(
                data={},
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                model="qwen2.5:14b",
                provider="ollama",
                schema_valid=True,
            )
            mock_call.return_value = empty
            result = score_job(_good_job(), mock_conn, config)

        assert result.status == "error"


# ---------------------------------------------------------------------------
# Tests: schema contract
# ---------------------------------------------------------------------------


class TestSchemaContract:
    """The v3 schema does NOT emit classification; Python derives it later."""

    def test_schema_has_no_classification_key(self):
        """JOB_ASSESSMENT_SCHEMA.properties must not contain 'classification'.

        Per CONTEXT D-06 anti-pattern 3: classification is Python-derived
        from sub_scores + legitimacy_note at persist time — never LLM-emitted.
        """
        assert "classification" not in JOB_ASSESSMENT_SCHEMA.get("properties", {}), (
            "v3 schema must not declare classification as a property "
            "(derive_classification owns the 4-way rule)"
        )
        assert "classification" not in JOB_ASSESSMENT_SCHEMA.get("required", []), (
            "v3 schema must not require classification from the LLM"
        )

    def test_coerce_ignores_any_classification_field(self):
        """_coerce_assessment ignores a classification field if the model emits one."""
        data = _good_response_data()
        data["classification"] = "apply"  # lying/hallucinated — must be ignored
        assessment = _coerce_assessment(data, provider="ollama")
        # Sentinel empty string — persist_job_assessment overwrites with derived value.
        assert assessment.classification == ""

    def test_coerce_extracts_sub_scores_from_top_level(self):
        """Sub-score fields are at the top level of the LLM response (not nested)."""
        data = _good_response_data()
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores == {
            "title_fit": 5,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 3,
            "seniority_match": 5,
            "skills_match": 4,
        }

    def test_coerce_defensively_converts_string_sub_scores_to_int(self):
        """If a sub-score arrives as a string (dispatcher coercion gap), cast to int."""
        data = _good_response_data()
        data["title_fit"] = "5"
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores["title_fit"] == 5
        assert isinstance(assessment.sub_scores["title_fit"], int)

    def test_coerce_unwraps_d2_evidence_score_pairs(self):
        """Variant v4d2 wraps each axis as {evidence, score}; coerce extracts the int."""
        data = _good_response_data()
        # Wrap each axis as the D2 variant emits it.
        for key in (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ):
            data[key] = {"evidence": f"<jd-quote-for-{key}>", "score": data[key]}
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores == {
            "title_fit": 5,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 3,
            "seniority_match": 5,
            "skills_match": 4,
        }
        assert all(isinstance(v, int) for v in assessment.sub_scores.values())


# ---------------------------------------------------------------------------
# Tests: module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """score_job is a pure-function addition with no production callers yet (Plan 1)."""

    def test_scoring_result_is_frozen(self):
        """ScoringResult is @dataclass(frozen=True) (hashable, immutable)."""
        r = ScoringResult(status="ok", data=None, provider="ollama")
        with pytest.raises((AttributeError, Exception)):
            r.status = "error"  # type: ignore[misc]

    def test_module_exports_expected_names(self):
        """__all__ declares score_job, ScoringResult, JOB_ASSESSMENT_SCHEMA."""
        from job_finder.web import job_scorer

        assert "score_job" in job_scorer.__all__
        assert "ScoringResult" in job_scorer.__all__
        assert "JOB_ASSESSMENT_SCHEMA" in job_scorer.__all__
