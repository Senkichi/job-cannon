"""Judge protocol with position-swap for cascade audit (Phase 36).

Implements pairwise-blind comparison using DeepSeek-V4-Flash via OpenRouter
(`deepseek/deepseek-v4-flash:free`), including position-swap validation to
eliminate position bias.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from evals.cascade_audit.verdict import Verdict

logger = logging.getLogger(__name__)

JUDGE_SYSTEM_PROMPT = """You are an expert evaluator for job search automation systems. Compare two LLM outputs (A and B) for the same input.

Evaluation criteria (in order of priority):
- Functional correctness: Does the output contain all required fields?
- Semantic accuracy: Is the extracted information factually correct?
- Completeness: Does the output include all relevant information?

If both outputs are equally good, return 'tie'. If one output has a critical error (missing required field, invalid URL, hallucinated data), the other wins.

Provide a brief rationale citing specific differences."""


def judge_pair(
    output_a: dict,
    output_b: dict,
    callsite: str,
    provider: Any,
) -> Verdict:
    """Judge a single A/B pair using DeepSeek-V4-Flash.

    Args:
        output_a: First LLM output (labeled A in prompt).
        output_b: Second LLM output (labeled B in prompt).
        callsite: Callsite name for context in prompt.
        provider: OpenRouterProvider instance for making judge call.

    Returns:
        Verdict object with winner, rationale, and confidence.

    Raises:
        ValidationError: If judge response cannot be parsed as Verdict (after retry).
    """
    # Build prompt with callsite context and anonymized outputs
    prompt = f"""Callsite: {callsite}

Output A:
{str(output_a)[:2000]}

Output B:
{str(output_b)[:2000]}

Which output is better? Respond with winner ('A', 'B', or 'tie'), rationale, and confidence (0-1)."""

    # Call provider with DeepSeek-V4 Flash (free tier)
    result = provider.call(
        model="deepseek/deepseek-v4-flash:free",
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_schema=Verdict.model_json_schema(),
        max_tokens=1024,
    )

    # Parse response into Verdict
    try:
        # OpenRouter provider returns parsed dict, not JSON string
        if isinstance(result.data, dict):
            verdict = Verdict.model_validate(result.data)
        else:
            verdict = Verdict.model_validate_json(result.data)
        return verdict
    except ValidationError as exc:
        logger.warning(f"Judge response validation error: {exc}, retrying once...")
        # Retry once with same inputs
        result = provider.call(
            model="deepseek/deepseek-v4-flash:free",
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_schema=Verdict.model_json_schema(),
            max_tokens=1024,
        )
        # OpenRouter provider returns parsed dict, not JSON string
        if isinstance(result.data, dict):
            verdict = Verdict.model_validate(result.data)
        else:
            verdict = Verdict.model_validate_json(result.data)
        return verdict


def judge_with_position_swap(
    output_a: dict,
    output_b: dict,
    callsite: str,
    provider: Any,
) -> tuple[Verdict, bool]:
    """Judge with position-swap to eliminate position bias.

    Calls judge_pair twice (A/B and B/A) and computes consensus.

    Args:
        output_a: First LLM output.
        output_b: Second LLM output.
        callsite: Callsite name for context.
        provider: OpenRouterProvider instance.

    Returns:
        Tuple of (consensus_verdict, agreement_flag).
        agreement_flag is True if both verdicts agree, False if they disagree.
    """
    verdict_ab = judge_pair(output_a, output_b, callsite, provider)
    verdict_ba = judge_pair(output_b, output_a, callsite, provider)

    # Compute agreement: both verdicts must agree for a non-tie
    if verdict_ab.winner == verdict_ba.winner:
        return verdict_ab, True
    if verdict_ab.winner == "tie" and verdict_ba.winner == "tie":
        return verdict_ab, True
    # Disagreement = tie
    return (
        Verdict(
            winner="tie",
            rationale="Position swap disagreement",
            confidence=0.5,
        ),
        False,
    )
