"""Unit tests for judge protocol (Phase 36)."""

from unittest.mock import Mock, patch

import pytest

from evals.cascade_audit.judge import JUDGE_SYSTEM_PROMPT, judge_pair, judge_with_position_swap
from evals.cascade_audit.verdict import Verdict


def test_judge_pair_mock():
    """Test judge_pair with mocked provider."""
    mock_provider = Mock()
    mock_result = Mock()
    mock_result.data = Verdict(
        winner="A", rationale="Output A is more complete", confidence=0.9
    ).model_dump_json()
    mock_provider.call.return_value = mock_result

    output_a = {"field": "value_a"}
    output_b = {"field": "value_b"}

    verdict = judge_pair(output_a, output_b, "test_callsite", mock_provider)

    assert verdict.winner == "A"
    assert verdict.rationale == "Output A is more complete"
    assert verdict.confidence == 0.9

    # Verify provider.call was called correctly
    mock_provider.call.assert_called_once()
    call_args = mock_provider.call.call_args
    assert call_args[1]["model"] == "deepseek/deepseek-chat:free"
    assert call_args[1]["system"] == JUDGE_SYSTEM_PROMPT
    assert call_args[1]["max_tokens"] == 1024


def test_judge_pair_retry_on_validation_error():
    """Test judge_pair retries on ValidationError."""
    mock_provider = Mock()

    # First call returns invalid JSON, second call returns valid
    mock_result_invalid = Mock()
    mock_result_invalid.data = "not valid json"

    mock_result_valid = Mock()
    mock_result_valid.data = Verdict(
        winner="B", rationale="Output B is better", confidence=0.8
    ).model_dump_json()

    mock_provider.call.side_effect = [mock_result_invalid, mock_result_valid]

    output_a = {"field": "value_a"}
    output_b = {"field": "value_b"}

    verdict = judge_pair(output_a, output_b, "test_callsite", mock_provider)

    assert verdict.winner == "B"
    assert mock_provider.call.call_count == 2  # Initial call + retry


def test_judge_with_position_swap_agreement():
    """Test judge_with_position_swap when both verdicts agree."""
    mock_provider = Mock()
    mock_result = Mock()
    mock_result.data = Verdict(
        winner="A", rationale="Output A wins", confidence=0.9
    ).model_dump_json()
    mock_provider.call.return_value = mock_result

    output_a = {"field": "value_a"}
    output_b = {"field": "value_b"}

    verdict, agreement = judge_with_position_swap(
        output_a, output_b, "test_callsite", mock_provider
    )

    assert agreement is True
    assert verdict.winner == "A"
    assert mock_provider.call.call_count == 2  # A/B and B/A


def test_judge_with_position_swap_disagreement():
    """Test judge_with_position_swap when verdicts disagree."""
    mock_provider = Mock()

    # First call returns A wins, second returns B wins
    mock_result_a = Mock()
    mock_result_a.data = Verdict(
        winner="A", rationale="A wins first", confidence=0.9
    ).model_dump_json()

    mock_result_b = Mock()
    mock_result_b.data = Verdict(
        winner="B", rationale="B wins second", confidence=0.8
    ).model_dump_json()

    mock_provider.call.side_effect = [mock_result_a, mock_result_b, mock_result_a, mock_result_b]

    output_a = {"field": "value_a"}
    output_b = {"field": "value_b"}

    verdict, agreement = judge_with_position_swap(
        output_a, output_b, "test_callsite", mock_provider
    )

    assert agreement is False
    assert verdict.winner == "tie"
    assert verdict.rationale == "Position swap disagreement"
    assert verdict.confidence == 0.5
