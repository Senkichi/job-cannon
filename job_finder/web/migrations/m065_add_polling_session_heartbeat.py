"""Migration 65 — add `last_tick_at` heartbeat column to `batch_score_sessions`.

Replaces the wrong-shape "session started >30 min ago" liveness check in
``render_polling_status`` with a tick-based heartbeat. The polling endpoint
currently flips any session whose ``(now - started_at) > timeout_minutes`` to
``status='error'``, even when the background thread is still legitimately
ticking — a fatal cap for the ATS scan, whose probe+scan loop iterates ~1,200
companies at up to 8 s per HTTP probe (live DB: 908 hit + 303 pending = a
worst-case 161+ minutes, well past the 30-min cap).

After this migration:

  - The bg thread (``_run_ats_scan_bg._tick`` and the batch-scoring ticks at
    ``blueprints/batch_scoring.py:298,305``) writes ``last_tick_at = now`` on
    each progress flush.
  - ``render_polling_status`` compares ``(now - COALESCE(last_tick_at,
    started_at))`` against ``timeout_minutes``. A healthy scan that ticks every
    8 s never goes stale; a truly hung scan still trips the timeout.

The column is nullable so existing in-flight sessions and historical rows are
unaffected. Both new tickers and the polling endpoint treat NULL via COALESCE
fallback to ``started_at``, preserving the old behavior for any row that
predates the wiring change.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=65,
    description="add last_tick_at heartbeat column to batch_score_sessions",
    sql=[
        "ALTER TABLE batch_score_sessions ADD COLUMN last_tick_at TEXT",
    ],
)
