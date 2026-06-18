"""Post-migration hooks — standing data fixups that ride on the migration loop.

These are NOT migrations. They are application-startup fixups that depend on
the migration chain having reached at least a certain version. The runner
(`run_migrations` in `db_migrate.py`) calls them after the loop terminates.

Putting them here keeps `db_migrate.py` focused on the migration runner.

The dedup re-key hook (`_run_rekey_if_stale`) is the standing, idempotent
re-derivation operation mandated by D-8: a derived value (dedup_key) records
the version of the function that produced it (`dedup_normalizer_version` in
`schema_meta`), and whenever the live `NORMALIZER_VERSION` differs, every row's
key is re-derived and duplicates are merged. This replaces the old once-ever
`merge_source='migration_complete'` sentinel, whose run-exactly-once gating let
#238's normalizer change strand 17 stale-key duplicate pairs.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.normalizers import NORMALIZER_VERSION

logger = logging.getLogger(__name__)

_VERSION_KEY = "dedup_normalizer_version"


def _read_stored_version(conn: sqlite3.Connection) -> int | None:
    """Return the stored dedup_normalizer_version, or None if unavailable.

    None means the watermark cannot be read yet — either ``schema_meta`` does
    not exist (DB is mid-migration, below m100) or the key was never seeded. The
    caller treats None as "defer; not safe to decide" so we never re-key against
    an unknown baseline.
    """
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (_VERSION_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _stamp_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the dedup_normalizer_version watermark (upsert)."""
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_VERSION_KEY, str(version)),
    )
    conn.commit()


def _run_rekey_if_stale(conn: sqlite3.Connection) -> None:
    """Re-derive dedup_keys + merge duplicates when the normalizer version drifts.

    Standing, idempotent re-key operation (D-8). Compares the stored
    ``dedup_normalizer_version`` against the live ``NORMALIZER_VERSION``:

    - Versions equal → nothing owed; return immediately (the common startup
      path — one cheap SELECT).
    - Versions differ → run ``run_retroactive_dedup`` (logging merges as
      ``rekey_v{N}``), stamp the watermark to ``NORMALIZER_VERSION``, and NULL
      classification/sub_scores/fit_analysis on merged canonicals so the v3
      scorer re-derives them. Re-keyed singletons need no rescore (their facts
      are unchanged; only the key string moved).
    - Watermark unreadable (``schema_meta`` absent, DB below m100 mid-migration)
      → defer. m100 seeds the watermark, so the next startup decides correctly.
      This is the "keep honoring the old sentinel for fresh DBs mid-migration"
      contract: the legacy ``migration_complete`` sentinel still suppresses a
      redundant v1 dedup until m100 has run, after which the watermark governs.

    Args:
        conn: Open SQLite connection (must have m100 applied to act).
    """
    try:
        stored = _read_stored_version(conn)
        if stored is None:
            # schema_meta not present / unseeded — m100 hasn't run yet. Defer.
            return
        if stored == NORMALIZER_VERSION:
            return  # Keys already at the current version — nothing to do.

        from job_finder.web.dedup_normalizer import run_retroactive_dedup

        merge_source = f"rekey_v{NORMALIZER_VERSION}"
        merged_count = run_retroactive_dedup(conn, merge_source=merge_source)

        # Stamp the watermark FIRST so a crash after a partial merge still
        # records progress at the target version (the operation is idempotent —
        # a re-run finds no remaining collisions).
        _stamp_version(conn, NORMALIZER_VERSION)

        logger.info(
            "Dedup re-key v%d: merged %d duplicate jobs (was version %d).",
            NORMALIZER_VERSION,
            merged_count,
            stored,
        )

        if merged_count > 0:
            # Activity-feed entry so the user sees the merge count.
            try:
                from job_finder.json_utils import utc_now_iso

                conn.execute(
                    """
                    INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                    VALUES (?, 'dedup_rekey', ?, 0, 0)
                """,
                    (utc_now_iso(), merged_count),
                )
                conn.commit()
            except Exception as e:
                logger.warning("Failed to log dedup re-key run: %s", e)

            # Queue merged canonical rows for re-scoring: NULL the v3 scoring
            # surface (classification/sub_scores_json) and the rationale
            # (fit_analysis). Only rows that were the canonical target of a
            # re-key merge are touched — re-keyed singletons keep their scores.
            try:
                canonical_keys = conn.execute(
                    "SELECT DISTINCT canonical_key FROM merge_log WHERE merge_source = ?",
                    (merge_source,),
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
                logger.warning("Failed to queue re-keyed rows for re-scoring: %s", e)

    except Exception as e:
        logger.warning("Dedup re-key failed (non-fatal): %s", e)
