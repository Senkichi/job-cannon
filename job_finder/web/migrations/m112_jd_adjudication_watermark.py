"""Migration 112 — per-row jd-adjudication watermark column.

The LLM half of the jd-content contract (PR2). The deterministic re-sweep (m111)
handles the high-precision REJECT bodies; the genuinely AMBIGUOUS middle (a real
JD lacking standard headings vs. chrome/landing page) is resolved by a cheap
local-LLM ("is this the JD for <title> at <company>?") run by a background job,
NOT on the startup path.

``jd_adjudicated_version INTEGER DEFAULT NULL`` records the JD_CONTENT_VERSION at
which a row was last adjudicated. The background job selects rows whose watermark
is below the live version (NULL = never adjudicated) so each row is judged once
per contract version and a JD_CONTENT_VERSION bump re-arms adjudication for the
whole corpus — the row-level analogue of the schema_meta re-sweep watermark. A
row that the LLM (or the deterministic CLEAN check) vouches for gets stamped; a
row the LLM rejects is cleared + re-queued like a deterministic REJECT.

Idempotent: the runner swallows "duplicate column name" so this is a no-op on
DBs that already have the column.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=112,
    description="jd_adjudicated_version column (per-row LLM jd-content adjudication watermark)",
    sql=[
        "ALTER TABLE jobs ADD COLUMN jd_adjudicated_version INTEGER DEFAULT NULL",
    ],
)
