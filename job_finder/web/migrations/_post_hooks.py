"""Post-migration hooks — one-time data fixes that ride on the migration loop.

These are NOT migrations. They are application-startup fixups that depend on
the migration chain having reached at least a certain version. The runner
(`run_migrations` in `db_migrate.py`) calls them after the loop terminates.

Putting them here keeps `db_migrate.py` focused on the migration runner;
the dedup hook is a one-time data-quality pass that happens to run at the
same boundary.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _run_retroactive_dedup_once(conn: sqlite3.Connection) -> None:
    """Run retroactive dedup merge exactly once (guarded by sentinel in merge_log).

    Checks for a sentinel row with `merge_source='migration_complete'`. If
    not found, runs `run_retroactive_dedup`, inserts the sentinel, and logs
    the result. Inserts a `runs` table entry for activity feed visibility.

    Args:
        conn: Open SQLite connection (must have migration 6 applied).
    """
    try:
        sentinel = conn.execute(
            "SELECT id FROM merge_log WHERE merge_source = 'migration_complete' LIMIT 1"
        ).fetchone()
        if sentinel is not None:
            return  # Already ran -- skip

        # Import here to avoid circular import at module load time
        from datetime import datetime as _dt

        from job_finder.web.dedup_normalizer import run_retroactive_dedup

        merged_count = run_retroactive_dedup(conn)
        now_iso = _dt.now().isoformat()

        # Insert sentinel row to mark completion
        conn.execute(
            """
            INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at)
            VALUES ('__sentinel__', '__sentinel__', 'migration_complete', ?)
        """,
            (now_iso,),
        )
        conn.commit()

        if merged_count > 0:
            # Add activity feed entry so the user sees the merge count
            try:
                conn.execute(
                    """
                    INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                    VALUES (?, 'dedup_migration', ?, 0, 0)
                """,
                    (now_iso, merged_count),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log dedup migration run: %s", e)

            logger.info("Retroactive dedup: merged %d duplicate jobs.", merged_count)

            # Queue merged canonical rows for re-scoring by nulling the v3
            # scoring surface (classification/sub_scores_json) and the
            # rationale (fit_analysis). Plan 5 dropped haiku_score/sonnet_score;
            # the v3 scorer re-derives classification from sub_scores.
            try:
                canonical_keys = conn.execute(
                    "SELECT canonical_key FROM merge_log WHERE merge_source = 'migration'"
                ).fetchall()
                for row in canonical_keys:
                    conn.execute(
                        """
                        UPDATE jobs
                           SET classification = NULL,
                               sub_scores_json = NULL,
                               fit_analysis = NULL
                         WHERE dedup_key = ?
                    """,
                        (row[0],),
                    )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to queue merged rows for re-scoring: %s", e)
        else:
            logger.info("Retroactive dedup: no duplicates found.")

    except Exception as e:
        logger.warning("Retroactive dedup failed (non-fatal): %s", e)
