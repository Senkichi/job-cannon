"""Adapter for parse_structured_fields callsite (Phase 36)."""

from __future__ import annotations

import sqlite3

from evals.cascade_audit.adapters import rows_to_dicts


class ParseStructuredFieldsAdapter:
    """Adapter for parse_structured_fields callsite."""

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample jobs with jd_full for parse_structured_fields."""
        cursor = conn.execute(
            """
            SELECT dedup_key, jd_full
            FROM jobs
            WHERE jd_full IS NOT NULL AND LENGTH(jd_full) > 400
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        )
        return rows_to_dicts(cursor)

    def exercise(self, row: dict, provider: str, config: dict, conn: sqlite3.Connection) -> dict:
        """Exercise parse_structured_fields production code."""
        from job_finder.web.enrichment_tiers import parse_structured_fields

        result = parse_structured_fields(
            jd_full=row["jd_full"],
            job_row=row,
            config=config,
            conn=conn,
        )
        return result

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold reference."""
        metrics = {}

        # Schema validation
        schema_valid = isinstance(candidate, dict) and all(k in candidate for k in gold)
        metrics["schema_valid"] = schema_valid

        # Salary MAE if both have salary_min
        if gold.get("salary_min") and candidate.get("salary_min"):
            try:
                mae = abs(float(gold["salary_min"]) - float(candidate["salary_min"]))
                metrics["salary_mae"] = mae
            except (ValueError, TypeError):
                metrics["salary_mae"] = None

        # Location match
        metrics["location_match"] = gold.get("location") == candidate.get("location")

        # Hallucination count
        gold_keys = set(gold.keys())
        candidate_keys = set(candidate.keys()) if isinstance(candidate, dict) else set()
        metrics["hallucination_count"] = len(candidate_keys - gold_keys)

        return metrics
