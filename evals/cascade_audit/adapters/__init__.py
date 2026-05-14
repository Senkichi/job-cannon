"""TaskAdapter protocol for cascade audit (Phase 36)."""

from __future__ import annotations

from typing import Protocol


def rows_to_dicts(cursor) -> list[dict]:
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    return [
        dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
        for row in rows
    ]


class TaskAdapter(Protocol):
    """Protocol for cascade audit task adapters.

    Each adapter implements sample(), exercise(), and score() methods
    to evaluate a specific callsite against the production code.
    """

    def sample(self, n: int, conn) -> list[dict]:
        """Sample n rows from corpus for this callsite.

        Args:
            n: Number of rows to sample.
            conn: Open SQLite connection to production DB.

        Returns:
            List of sampled row dicts.
        """
        ...

    def exercise(self, row: dict, provider: str, config: dict, conn) -> dict:
        """Exercise the production code path for this callsite.

        Args:
            row: Single row from corpus.
            provider: Provider name to use for the call.
            config: Application config dict.
            conn: Open SQLite connection.

        Returns:
            Result dict from the production code path.
        """
        ...

    def score(self, gold: dict, candidate: dict) -> dict:
        """Score candidate output against gold reference.

        Args:
            gold: Gold reference output.
            candidate: Candidate output to score.

        Returns:
            Dict of metrics for this callsite.
        """
        ...


__all__ = ["TaskAdapter"]
