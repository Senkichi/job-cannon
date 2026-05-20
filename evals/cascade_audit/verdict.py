"""Verdict ADT for cascade audit (Phase 36)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Verdict(BaseModel):
    """Judge verdict for pairwise comparison.

    Represents the result of a DeepSeek-V4-Flash judge (`deepseek/deepseek-v4-flash:free`
    via OpenRouter; see `judge.py`) comparing two LLM outputs (labeled A and B)
    for the same input.

    Attributes:
        winner: Which output won ("A", "B", or "tie").
        rationale: Brief explanation of the decision.
        confidence: Confidence score 0-1.
    """

    winner: Literal["A", "B", "tie"] = Field(
        description="Which output won: 'A', 'B', or 'tie'"
    )
    rationale: str = Field(description="Brief explanation of the decision")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score 0-1"
    )

    class Config:
        strict = True
