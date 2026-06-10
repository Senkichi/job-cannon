"""Shared heal-audit writer.

Moved out of ``heal_pipeline._audit`` in Phase D so the rollback primitive
(and later the shadow guard / upstream reporter) can audit without importing
the pipeline. Outcomes: ``candidate_generated`` | ``validated`` | ``adopted``
| ``rejected:<reason>`` | ``no_provider`` | ``skipped:<reason>`` |
``rolled_back:<reason>`` | ``cap_exhausted``.
"""

from __future__ import annotations

import sqlite3

from job_finder.json_utils import utc_now_iso


def record_audit(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Insert one heal_audit row and commit."""
    conn.execute(
        "INSERT INTO heal_audit (source, surface, outcome, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, surface, outcome, detail, utc_now_iso()),
    )
    conn.commit()
