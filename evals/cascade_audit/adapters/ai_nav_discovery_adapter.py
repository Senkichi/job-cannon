"""Adapter for ai_nav_discovery callsite (Phase 36)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from evals.cascade_audit.adapters import rows_to_dicts
from evals.cascade_audit.corpus_loader import _safe_cache_stem


class AiNavDiscoveryAdapter:
    """Adapter for ai_nav_discovery callsite."""

    def __init__(self, artifact_dir: Path) -> None:
        """Initialize with artifact directory for cached recipes."""
        self._artifact_dir = Path(artifact_dir)

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample companies with careers_nav_recipe for ai_nav_discovery."""
        cursor = conn.execute(
            """
            SELECT dedup_key, careers_nav_recipe
            FROM companies
            WHERE careers_nav_recipe IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        )
        return rows_to_dicts(cursor)

    def exercise(
        self,
        row: dict,
        provider: str,
        config: dict,
        conn: sqlite3.Connection,
        playwright_context=None,
    ) -> dict:
        """Exercise ai_nav_discovery production code."""
        from job_finder.web.ai_career_navigator import discover_navigation_recipe

        # Load cached recipe from artifacts/round_0/recipes/. Filename uses the
        # same _safe_cache_stem convention as corpus_loader so adapter and loader
        # agree on the on-disk name (#phase-36 audit followup).
        dedup_key = row["dedup_key"]
        recipe_path = (
            self._artifact_dir / "round_0" / "recipes" / f"{_safe_cache_stem(dedup_key)}.json"
        )
        if not recipe_path.exists():
            raise FileNotFoundError(f"Cached recipe not found: {recipe_path}")

        import json

        cached_recipe = json.loads(recipe_path.read_text(encoding="utf-8"))

        result = discover_navigation_recipe(
            recipe=cached_recipe,
            provider=provider,
            config=config,
            conn=conn,
            playwright_context=playwright_context,
        )
        return result

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold reference."""
        metrics = {}

        # Step count delta
        gold_steps = gold.get("step_count", 0) if isinstance(gold, dict) else 0
        candidate_steps = candidate.get("step_count", 0) if isinstance(candidate, dict) else 0
        metrics["step_count_delta"] = candidate_steps - gold_steps

        # Replay duration ratio
        gold_duration = gold.get("duration_ms", 0) if isinstance(gold, dict) else 0
        candidate_duration = candidate.get("duration_ms", 0) if isinstance(candidate, dict) else 0
        if gold_duration > 0:
            metrics["replay_duration_ratio"] = candidate_duration / gold_duration
        else:
            metrics["replay_duration_ratio"] = 0.0

        return metrics
