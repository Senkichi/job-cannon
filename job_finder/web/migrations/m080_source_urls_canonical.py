"""Migration 80 — source_urls_raw forensic column + canonicalize source_urls.

Phase 49.01 (spec §13 commit 49.01; D-06; F-05; NG-03).

Adds a ``source_urls_raw`` JSON column that preserves the original (pre-
canonicalization) URLs, then rewrites the existing ``source_urls`` column to
canonical form (tracking params stripped, query sorted, scheme/host lowered).

After this migration:
  - new ingestion writes already store canonical ``source_urls`` +
    forensic ``source_urls_raw`` (the ``ParsedJob.from_job`` boundary
    canonicalizes at construction);
  - existing rows are healed here.

Idempotency: re-running is a no-op. ``source_urls_raw`` is populated from the
original URLs on the first run and preserved thereafter; canonicalizing an
already-canonical URL yields the same string. The ``ALTER TABLE ADD COLUMN``
sits in ``sql`` so the runner swallows ``duplicate column name`` on re-run.

Revert: re-add via a follow-up migration that drops ``source_urls_raw`` — the
``Migration`` value type has no down-helper (matches every other migration in
this package); rollback is a forward migration per spec §17.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext
from job_finder.web.url_canonical import canonicalize_url

logger = logging.getLogger(__name__)


def _canonicalize_list(raw_json: str | None) -> tuple[list[str], bool]:
    """Parse a source_urls JSON list and return (canonical_list, changed)."""
    if not raw_json:
        return [], False
    try:
        urls = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return [], False
    if not isinstance(urls, list):
        return [], False
    canonical = [canonicalize_url(u)[0] if isinstance(u, str) else u for u in urls]
    return canonical, canonical != urls


def _migrate(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        is None
    ):
        logger.info("m080: jobs table not present, no-op")
        return

    rows = conn.execute("SELECT dedup_key, source_urls, source_urls_raw FROM jobs").fetchall()

    rewritten = 0
    for dedup_key, source_urls, source_urls_raw in rows:
        # The forensic original: on first run source_urls holds the raw URLs;
        # on re-run source_urls_raw already preserves them.
        original_json = source_urls_raw if source_urls_raw is not None else source_urls
        if original_json is None:
            continue

        canonical, _ = _canonicalize_list(original_json)
        canonical_json = json.dumps(canonical)

        # Only write when something actually changes (canonical differs from the
        # stored source_urls, or source_urls_raw not yet backfilled).
        if canonical_json != source_urls or source_urls_raw is None:
            conn.execute(
                "UPDATE jobs SET source_urls = ?, source_urls_raw = ? WHERE dedup_key = ?",
                (canonical_json, original_json, dedup_key),
            )
            rewritten += 1

    logger.info("m080: canonicalized source_urls for %d of %d row(s)", rewritten, len(rows))


MIGRATION = Migration(
    version=80,
    description="add source_urls_raw forensic column + canonicalize source_urls",
    sql=["ALTER TABLE jobs ADD COLUMN source_urls_raw TEXT"],
    py=_migrate,
)
