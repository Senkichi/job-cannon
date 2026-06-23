"""Adapter for company_research callsite (Phase 36)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from evals.cascade_audit.adapters import rows_to_dicts


class CompanyResearchAdapter:
    """Adapter for company_research callsite."""

    def __init__(self, judge_provider=None) -> None:
        self._judge_provider = judge_provider

    def sample(self, n: int, conn: sqlite3.Connection) -> list[dict]:
        """Sample companies for company_research."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        select_id = "id, " if "id" in columns else ""
        cursor = conn.execute(
            f"""
            SELECT {select_id}dedup_key, name, domain
            FROM companies
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,),
        )
        return rows_to_dicts(cursor)

    def exercise(self, row: dict, provider: str, config: dict, conn: sqlite3.Connection) -> dict:
        """Exercise company_research production code."""
        from job_finder.web.company_research import run_company_research_background

        if "id" not in row:
            return {"company_name": row.get("name"), "company_domain": row.get("domain")}
        now = datetime.now(UTC).isoformat()
        cursor = conn.execute(
            "INSERT INTO company_research (company_id, status, requested_at) VALUES (?, ?, ?)",
            (row["id"], "generating", now),
        )
        conn.commit()
        research_id = cursor.lastrowid
        run_company_research_background(
            research_id=research_id,
            company_id=row["id"],
            db_path=config.get("db_path", "jobs.db"),
            config=config,
        )
        return {"research_id": research_id, "company_id": row["id"]}

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate against gold using judge protocol."""
        from evals.cascade_audit.judge import judge_with_position_swap

        if self._judge_provider is not None:
            verdict, agreement = judge_with_position_swap(
                gold, candidate, "company_research", self._judge_provider
            )
            return {
                "judge_winner": verdict.winner,
                "judge_rationale": verdict.rationale,
                "judge_confidence": verdict.confidence,
                "judge_position_swap_agreement": agreement,
            }

        # No judge provider configured (set OPENROUTER_API_KEY so
        # run_audit._load_judge_provider wires one in) — fall back to a neutral tie.
        return {
            "judge_winner": "tie",
            "judge_rationale": "no judge provider configured",
            "judge_confidence": 0.5,
        }
