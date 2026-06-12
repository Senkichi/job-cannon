"""Migration 96 — staleness-consistency: precedence flip + evidence backfills.

The 2026-06-11 staleness audit found the three status signals drifting
because the clock-based detector (Phase A) ignored direct liveness
evidence (Phases B/C): 4,443 rows sat at expiry_status='live' AND
is_stale=1, and 1,170 live-verified jobs were clock-archived in the
prior 30 days alone. The code fixes land alongside this migration
(persist_job_expiry_state live-refresh, auto-reopen expiry clear,
passive-stage stale scoping, B→C→A phase order); this migration repairs
the data those bugs left behind.

Schema change — computed_status precedence flip:
    m082 ranked is_stale (weak, inferred from the clock) above
    expiry_status='expired' (strong, HTTP/board-verified). 2,136 rows
    carried both and displayed as 'stale', masking the actionable
    verdict. The column is VIRTUAL, so the flip is a cheap DROP+ADD;
    the runner swallows 'no such column' / 'duplicate column name' so
    a re-run is idempotent.

Data backfills (py helper, each predicate self-extinguishing):
    1. Live-evidence refresh — a 'live' verdict newer than last_seen is
       positive sighting evidence; catch last_seen up and clear is_stale
       (retroactive application of the persist_job_expiry_state fix).
    2. Resurrect recent casualties — jobs whose LATEST archive event came
       from a system actor while the job was HTTP-verified live within
       the last 14 days go back to 'discovered' with an audit event.
       Manual archives and dismissals are never touched. Phase C
       re-verifies the cohort within days (expiry_checked_at ages past
       cascade_recheck_days); truly-dead reposts re-archive themselves.
    3. Clear frozen 'expired' on active rows — auto-reopened jobs kept
       their pre-reopen verdict, which excluded them from Phase B/C
       forever (249 rows). NULLing both columns puts them at the front
       of the Phase C queue.
    4. Clear is_stale outside the passive stages — staleness is only
       meaningful pre-application; the default jobs view hides stale
       rows and was silently hiding applied/phone_screen jobs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from job_finder.json_utils import utc_now_iso
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Archive-event sources that represent system inference, not user intent.
# Mirrors the actors that write to_status='archived' outside the UI.
_SYSTEM_ARCHIVE_SOURCES = (
    "stale_detector",
    "ats_reconciler",
    "expiry_check",
    "run_scoring_liveness",
    "ingestion_liveness",
)

# Only resurrect jobs live-verified this recently — older verdicts are too
# stale to trust without a fresh check, and Phase A would re-archive a
# resurrected row whose refreshed last_seen is already past the threshold.
_RESURRECT_WINDOW_DAYS = 14

_PASSIVE_STATUSES = ("discovered", "reviewing")


def _backfill(ctx: MigrationContext) -> None:
    conn = ctx.conn

    # 1. Live-evidence refresh: catch last_seen up to the newer live verdict.
    cur = conn.execute(
        """UPDATE jobs
              SET last_seen = expiry_checked_at, is_stale = 0
            WHERE expiry_status = 'live'
              AND expiry_checked_at IS NOT NULL
              AND expiry_checked_at > last_seen"""
    )
    logger.info("m096: live-evidence refresh touched %d rows", cur.rowcount)

    # 2. Resurrect system-archived jobs that were live-verified recently.
    cutoff = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(days=_RESURRECT_WINDOW_DAYS)
    ).isoformat()
    src_placeholders = ",".join("?" * len(_SYSTEM_ARCHIVE_SOURCES))
    rows = conn.execute(
        f"""SELECT j.dedup_key FROM jobs j
             WHERE j.pipeline_status = 'archived'
               AND j.expiry_status = 'live'
               AND j.expiry_checked_at > ?
               AND (SELECT pe.source FROM pipeline_events pe
                     WHERE pe.job_id = j.dedup_key AND pe.to_status = 'archived'
                     ORDER BY pe.timestamp DESC LIMIT 1)
                   IN ({src_placeholders})""",
        (cutoff, *_SYSTEM_ARCHIVE_SOURCES),
    ).fetchall()
    if rows:
        keys = [r["dedup_key"] for r in rows]
        now = utc_now_iso()
        placeholders = ",".join("?" * len(keys))
        conn.execute(
            f"UPDATE jobs SET pipeline_status = 'discovered' WHERE dedup_key IN ({placeholders})",
            keys,
        )
        conn.executemany(
            """INSERT INTO pipeline_events
                   (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'archived', 'discovered', ?, 'm096_backfill',
                       'live_verified_at_archive_time')""",
            [(k, now) for k in keys],
        )
    logger.info("m096: resurrected %d live-verified system-archived jobs", len(rows))

    # 3. Clear frozen 'expired' verdicts on active rows so Phase B/C re-verify.
    passive_placeholders = ",".join("?" * len(_PASSIVE_STATUSES))
    cur = conn.execute(
        f"""UPDATE jobs
               SET expiry_status = NULL, expiry_checked_at = NULL
             WHERE pipeline_status IN ({passive_placeholders})
               AND expiry_status = 'expired'""",
        _PASSIVE_STATUSES,
    )
    logger.info("m096: cleared frozen expired verdict on %d active rows", cur.rowcount)

    # 4. is_stale is only meaningful in passive stages.
    cur = conn.execute(
        f"""UPDATE jobs SET is_stale = 0
             WHERE is_stale = 1
               AND pipeline_status NOT IN ({passive_placeholders})""",
        _PASSIVE_STATUSES,
    )
    logger.info("m096: cleared is_stale on %d non-passive rows", cur.rowcount)


MIGRATION = Migration(
    version=96,
    description="staleness consistency: expired>stale precedence + liveness-evidence backfills",
    sql=[
        "ALTER TABLE jobs DROP COLUMN computed_status",
        # Identical to m082 except the expired/stale WHEN order: a verified
        # 'expired' outranks the clock-inferred 'stale'.
        "ALTER TABLE jobs ADD COLUMN computed_status TEXT "
        "GENERATED ALWAYS AS ("
        "  CASE"
        "    WHEN pipeline_status IN "
        "         ('applied','phone_screen','interviewing','offer','rejected','withdrawn')"
        "      THEN pipeline_status"
        "    WHEN expiry_status = 'expired' THEN 'expired'"
        "    WHEN is_stale = 1 THEN 'stale'"
        "    ELSE COALESCE(pipeline_status, 'active')"
        "  END"
        ") VIRTUAL",
    ],
    py=_backfill,
)
