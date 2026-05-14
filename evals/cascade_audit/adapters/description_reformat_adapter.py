"""Adapter for description_reformat callsite (Phase 36)."""

from __future__ import annotations

import sqlite3
from typing import Any

from evals.cascade_audit.adapters import rows_to_dicts


class DescriptionReformatAdapter:
    """Adapter for description_reformat callsite."""

    def __init__(self, judge_provider=None) -> None:
        self._judge_provider = judge_provider

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample jobs with description for description_reformat."""
        cursor = conn.execute(
            """
            SELECT dedup_key, description
            FROM jobs
            WHERE description IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        )
        return rows_to_dicts(cursor)

    def exercise(self, row: dict, provider: str, config: dict, conn: sqlite3.Connection) -> dict:
        """Exercise description_reformat production code."""
        from job_finder.web.description_reformatter import reformat_description

        result = reformat_description(
            description=row["description"],
            config=config,
            conn=conn,
        )
        return result

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold using judge protocol."""
        from evals.cascade_audit.judge import judge_with_position_swap
        from job_finder.web.model_provider import _make_adapter

        if self._judge_provider is not None:
            verdict, agreement = judge_with_position_swap(
                gold, candidate, "description_reformat", self._judge_provider
            )
            return {
                "judge_winner": verdict.winner,
                "judge_rationale": verdict.rationale,
                "judge_confidence": verdict.confidence,
                "judge_position_swap_agreement": agreement,
            }

        metrics = {
            "judge_winner": "tie",
            "judge_rationale": "Not yet implemented",
            "judge_confidence": 0.5,
        }

        # TODO: Implement actual judge call with OpenRouter provider
        # provider = _make_adapter("openrouter", None, None, config)
        # verdict, _ = judge_with_position_swap(gold, candidate, "description_reformat", provider)
        # metrics["judge_winner"] = verdict.winner
        # metrics["judge_rationale"] = verdict.rationale
        # metrics["judge_confidence"] = verdict.confidence

        return metrics
