"""Migration 105 — health escalation per-signal consecutive-degraded state (#440).

Backs the daily heartbeat's N-consecutive-degraded escalation
(``run_health_check`` in ``scheduler/_runners.py``). Each tracked signal
(``ingestion``, ``staleness``, ``oauth``, and one ``source:<name>`` key per
offending source) gets one row recording how many consecutive heartbeat checks
it has stayed degraded, persisted so the count survives an in-process
APScheduler restart / Flask reload — in-memory tracking would reset on every
reload and never reach the threshold.

``last_escalated_at`` is the fire-once-per-streak gate: stamped when a key
crosses the threshold, cleared back to NULL when the key recovers so a fresh
degradation streak can escalate again.

Idempotent ``CREATE TABLE IF NOT EXISTS`` (the table is independent of all
other schema), so application order relative to its sibling migrations does not
matter. See ``m087_heal_state.py`` for the ``Migration`` wrapper shape.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=105,
    description="health_escalation_state: per-signal consecutive-degraded tracking for heartbeat escalation (#440)",
    sql=[
        "CREATE TABLE IF NOT EXISTS health_escalation_state ("
        "    signal_key TEXT PRIMARY KEY,"
        "    consecutive_degraded INTEGER NOT NULL DEFAULT 0,"
        "    last_status TEXT NOT NULL,"
        "    last_escalated_at TEXT DEFAULT NULL,"
        "    updated_at TEXT NOT NULL"
        ")",
    ],
)
