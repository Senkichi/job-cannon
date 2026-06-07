"""Per-source parse health: record extractions, detect breaks, read degraded set.

record_extraction is the single entry point the three ingestion surfaces call
after each extraction. It appends to the corpus and updates the running break
counter. It NEVER raises — observability must not break ingestion. run_detection
promotes counters that crossed the threshold to DEGRADED and logs an activity
row; it opens its own connection (background/orchestration caller).
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import BREAK_THRESHOLD, MIN_MEANINGFUL_LEN, corpus_store
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def record_extraction(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    raw_text: str,
    job_count: int,
    *,
    scrub_identifiers=None,
    detect: bool = True,
) -> None:
    """Append a corpus sample and (when detect) update the break counter. Never raises.

    detect=False is capture-only: the corpus sample + baseline_yield are recorded
    but the break counter is frozen. ATS/careers use this in Phase A because only
    their post-filter output is reachable at the hook site — the raw API/HTML
    artifact needed for honest break detection is a Phase-B addition.
    """
    try:
        baseline = corpus_store.baseline_yield(conn, source)
        corpus_store.append_sample(
            conn,
            source,
            surface,
            raw_text,
            {"job_count": int(job_count)},
            scrub_identifiers=scrub_identifiers,
        )

        is_meaningful = len(raw_text or "") >= MIN_MEANINGFUL_LEN
        is_break = baseline >= 1 and int(job_count) == 0 and is_meaningful
        new_baseline = corpus_store.baseline_yield(conn, source)
        now = utc_now_iso()

        row = conn.execute(
            "SELECT consecutive_breaks FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        prior = row[0] if row else 0

        if not detect:
            consecutive = prior  # capture-only: baseline tracked, counter frozen
        elif int(job_count) > 0:
            consecutive = 0
        elif is_break:
            consecutive = prior + 1
        else:
            consecutive = prior

        conn.execute(
            """INSERT INTO source_health
                   (source, surface, status, consecutive_breaks, baseline_yield, updated_at)
               VALUES (?, ?, 'healthy', ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   surface = excluded.surface,
                   consecutive_breaks = excluded.consecutive_breaks,
                   baseline_yield = excluded.baseline_yield,
                   updated_at = excluded.updated_at,
                   status = CASE WHEN excluded.consecutive_breaks = 0
                                 THEN 'healthy' ELSE source_health.status END""",
            (source, surface, consecutive, new_baseline, now),
        )
        conn.commit()
    except Exception:  # observability must never break ingestion
        logger.exception("autoheal record_extraction failed for source=%s", source)


def run_detection(db_path: str) -> list[str]:
    """Flip any source whose counter reached threshold to DEGRADED. Returns names."""
    flagged: list[str] = []
    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT source, consecutive_breaks, status FROM source_health "
                "WHERE consecutive_breaks >= ?",
                (BREAK_THRESHOLD,),
            ).fetchall()
            now = utc_now_iso()
            for r in rows:
                if r["status"] != "degraded":
                    conn.execute(
                        "UPDATE source_health SET status='degraded', last_break_at=?, "
                        "last_signal=? WHERE source=?",
                        (now, f"{r['consecutive_breaks']} consecutive zero-yields", r["source"]),
                    )
                    flagged.append(r["source"])
            conn.commit()
    except Exception:
        logger.exception("autoheal run_detection failed")
        return flagged

    if flagged:
        from job_finder.web.activity_tracker import ACTION_SOURCE_DEGRADED, log_activity

        for src in flagged:
            log_activity(
                db_path,
                ACTION_SOURCE_DEGRADED,
                entity_id=src,
                metadata={"reason": "consecutive_zero_yields", "threshold": BREAK_THRESHOLD},
            )
            logger.warning("autoheal: source '%s' flagged DEGRADED", src)
    return flagged


def degraded_sources(conn: sqlite3.Connection) -> list[dict]:
    """All currently-degraded sources, most-recent break first (dashboard reader)."""
    rows = conn.execute(
        "SELECT source, surface, consecutive_breaks, baseline_yield, last_signal, last_break_at "
        "FROM source_health WHERE status='degraded' ORDER BY last_break_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
