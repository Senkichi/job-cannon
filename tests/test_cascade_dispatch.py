"""Cascade dispatch coverage for haiku_scorer and sonnet_evaluator.

score_job_haiku and evaluate_job_sonnet are the two scorer entry points that
have had the use_dispatcher pattern longest; the live pipeline exercises them
at every ingestion cycle, but the existing test_scoring.py coverage patches
call_claude at the module level and never verifies the cascade branch.

These tests patch call_model and call_claude together so each of the three
dispatch paths — dispatcher, no-providers, cascade-exhausted-with-CLI-retry —
is independently asserted. The fourth test confirms a total-failure path
surfaces as ScoringResult.status == "error" rather than crashing the caller.
"""

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_job_row():
    return {
        "dedup_key": "acme|senior data scientist|remote",
        "title": "Senior Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "salary_min": 160000,
        "salary_max": 220000,
        "description": "Build ML models at Acme Corp. " * 40,
        "jd_full": "Full JD with enough text to pass the scoring guards. " * 40,
    }


@pytest.fixture
def sample_profile():
    return {
        "target_titles": ["Senior Data Scientist"],
        "target_locations": ["Remote"],
        "min_salary": 150000,
        "skills": ["Python", "SQL"],
        "industries": ["SaaS"],
    }


@pytest.fixture
def scoring_config():
    return {
        "scoring": {
            "haiku_threshold": 55,
            "daily_budget_usd": 25.0,
            "models": {
                "haiku": "claude-haiku-4-5",
                "sonnet": "claude-sonnet-4-6",
            },
        }
    }


_HAIKU_RESPONSE = {
    "score": 72,
    "summary": "Good match",
    "title_fit": "strong",
    "location_fit": "remote",
    "salary_meets_floor": True,
}

_SONNET_RESPONSE = {
    "score": 78,
    "summary": "Strong fit after full analysis",
    "fit_analysis": {"strengths": ["A"], "weaknesses": ["B"]},
}


# ---------------------------------------------------------------------------
# Haiku cascade dispatch
# ---------------------------------------------------------------------------


class TestHaikuCascadeDispatch:

    def test_uses_call_model_when_providers_configured(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_haiku, make_model_result,
    ):
        from job_finder.web.haiku_scorer import score_job_haiku
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_haiku}

        with patch("job_finder.web.haiku_scorer.call_model") as mock_cm, \
             patch("job_finder.web.haiku_scorer.call_claude") as mock_cc:
            mock_cm.return_value = make_model_result(_HAIKU_RESPONSE)
            result = score_job_haiku(sample_job_row, sample_profile, conn, config)

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "haiku"
        assert mock_cm.call_args.kwargs["purpose"] == "haiku_score"
        mock_cc.assert_not_called()
        assert result.status == "success"
        assert result.data["score"] == 72

    def test_uses_call_claude_when_no_providers(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
    ):
        from job_finder.web.haiku_scorer import score_job_haiku
        _path, conn = migrated_db

        with patch("job_finder.web.haiku_scorer.call_model") as mock_cm, \
             patch("job_finder.web.haiku_scorer.call_claude") as mock_cc:
            mock_cc.return_value = (_HAIKU_RESPONSE, 0.001)
            result = score_job_haiku(sample_job_row, sample_profile, conn, scoring_config)

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result.status == "success"

    def test_cascade_exhausted_falls_back_to_cli(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_haiku,
    ):
        from job_finder.web.haiku_scorer import score_job_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_haiku}

        with patch("job_finder.web.haiku_scorer.call_model") as mock_cm, \
             patch("job_finder.web.haiku_scorer.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = (_HAIKU_RESPONSE, 0.001)
            result = score_job_haiku(sample_job_row, sample_profile, conn, config)

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result.status == "success"

    def test_cascade_and_cli_both_fail_returns_error_status(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_haiku,
    ):
        from job_finder.web.haiku_scorer import score_job_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_haiku}

        with patch("job_finder.web.haiku_scorer.call_model") as mock_cm, \
             patch("job_finder.web.haiku_scorer.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            result = score_job_haiku(sample_job_row, sample_profile, conn, config)

        assert result.status == "error"
        assert result.data is None


# ---------------------------------------------------------------------------
# Sonnet cascade dispatch
# ---------------------------------------------------------------------------


class TestSonnetCascadeDispatch:

    def test_uses_call_model_when_providers_configured(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_sonnet, make_model_result,
    ):
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_sonnet}

        with patch("job_finder.web.sonnet_evaluator.call_model") as mock_cm, \
             patch("job_finder.web.sonnet_evaluator.call_claude") as mock_cc:
            mock_cm.return_value = make_model_result(_SONNET_RESPONSE)
            result = evaluate_job_sonnet(sample_job_row, sample_profile, conn, config)

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "sonnet"
        assert mock_cm.call_args.kwargs["purpose"] == "sonnet_eval"
        mock_cc.assert_not_called()
        assert result.status == "success"
        assert result.data["score"] == 78

    def test_uses_call_claude_when_no_providers(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
    ):
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        _path, conn = migrated_db

        with patch("job_finder.web.sonnet_evaluator.call_model") as mock_cm, \
             patch("job_finder.web.sonnet_evaluator.call_claude") as mock_cc:
            mock_cc.return_value = (_SONNET_RESPONSE, 0.004)
            result = evaluate_job_sonnet(sample_job_row, sample_profile, conn, scoring_config)

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result.status == "success"

    def test_cascade_exhausted_falls_back_to_cli(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_sonnet,
    ):
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_sonnet}

        with patch("job_finder.web.sonnet_evaluator.call_model") as mock_cm, \
             patch("job_finder.web.sonnet_evaluator.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = (_SONNET_RESPONSE, 0.004)
            result = evaluate_job_sonnet(sample_job_row, sample_profile, conn, config)

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result.status == "success"

    def test_cascade_and_cli_both_fail_returns_error_status(
        self, migrated_db, sample_job_row, sample_profile, scoring_config,
        cascade_config_sonnet,
    ):
        from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db
        config = {**scoring_config, **cascade_config_sonnet}

        with patch("job_finder.web.sonnet_evaluator.call_model") as mock_cm, \
             patch("job_finder.web.sonnet_evaluator.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            result = evaluate_job_sonnet(sample_job_row, sample_profile, conn, config)

        assert result.status == "error"
        assert result.data is None
