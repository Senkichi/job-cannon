"""Migration 63 — merge company rows by shared job board.

User rule (2026-05-27 bug list followup): two companies are the same
hiring entity iff they share a job board. If both 'Amazons' use the
same internal or external board, they're one company; if they use
different ones, they're distinct. m061's name-based heuristic deliberately
left subsidiary / branding variants alone — m063 resolves the rest by
operational identity instead of by name.

Detection (in priority order):

  1. ``(ats_platform, ats_slug)`` — the most reliable signal. Two rows
     with the same platform + slug point at the same ATS API endpoint,
     so any job pulled from one is identical to the other.
  2. Canonical ``careers_url`` — host + path, lowercased, ``www.`` and
     scheme stripped, trailing slash and query string removed. Two
     companies whose careers pages canonicalize to the same URL are the
     same hiring page.

Both signals are applied as separate passes. The (platform, slug) pass
runs first because it's strictly more reliable than URL matching
(slugs are emitted by probe/scan code paths that already know the
endpoint; URLs can be cosmetic mirrors).

Canonical row selection within a cluster:
  - Highest ``jobs_found_total`` (most operational history).
  - Tie-break: lowest ``id`` (oldest row).

Re-pointing follows the m058/m061 pattern: jobs.company_id,
company_scan_log.company_id, and (when present) company_research.
company_id are re-pointed to the canonical row, then orphan rows are
deleted.

Idempotent: after the first pass no cluster has >1 row keyed on the
same job board.

Deliberate non-handling:
  - Companies with NULL ats_platform/slug AND NULL careers_url have no
    job-board identity and are NOT merged here. They need either a
    successful probe (which assigns a slug) or a name-based heuristic
    (m061). The user's "ncidia"/"2100 nvidia usa" examples are likely
    in this cohort — they would only auto-merge if probing eventually
    discovers a shared ATS, or if a future name-based heuristic adds
    fuzzy matching.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from urllib.parse import urlparse

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _canonical_careers_url(raw: str | None) -> str:
    """Lower-case host + path, strip scheme, ``www.``, query, fragment, and
    trailing slash. Two URLs that canonicalize identically point at the
    same careers page.

    Returns "" for empty / None / unparseable input.
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    # Many DB-stored URLs lack a scheme; urlparse needs one to populate
    # netloc, so prepend a stub when missing.
    if "://" not in s:
        s = "https://" + s
    try:
        parsed = urlparse(s)
    except Exception:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    if not host:
        return ""
    return host + path


def _repoint_and_delete(
    conn: sqlite3.Connection, orphan_id: int, canonical_id: int
) -> None:
    """Mirror m058/m061's helper: re-point known FK references then drop the orphan."""
    conn.execute(
        "UPDATE jobs SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    conn.execute(
        "UPDATE company_scan_log SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    if _table_exists(conn, "company_research"):
        conn.execute(
            "UPDATE company_research SET company_id = ? WHERE company_id = ?",
            (canonical_id, orphan_id),
        )
    conn.execute("DELETE FROM companies WHERE id = ?", (orphan_id,))


def _pick_canonical(rows: list[sqlite3.Row]) -> int:
    """Return the id of the row to keep within a same-board cluster.

    Preference: highest jobs_found_total (most operational history). Ties
    broken by lowest id (oldest row, keeps stable FK targets).
    """
    return max(
        rows,
        key=lambda r: (r["jobs_found_total"] or 0, -int(r["id"])),
    )["id"]


def _merge_by_ats_slug(conn: sqlite3.Connection) -> int:
    """Pass 1: merge rows that share ``(ats_platform, ats_slug)``."""
    rows = conn.execute(
        "SELECT id, ats_platform, ats_slug, jobs_found_total "
        "FROM companies "
        "WHERE ats_platform IS NOT NULL "
        "  AND ats_platform != '' "
        "  AND ats_slug IS NOT NULL "
        "  AND ats_slug != ''"
    ).fetchall()

    by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        # Normalize platform to lowercase; ats_slug case sometimes varies
        # (e.g. "Flock%20Safety" vs "flock-safety"), so leave as-is. Two
        # rows with case-different slugs are likely separate sub-orgs.
        by_key[(r["ats_platform"].strip().lower(), r["ats_slug"].strip())].append(r)

    merged = 0
    for key, cluster in by_key.items():
        if len(cluster) < 2:
            continue
        canonical_id = _pick_canonical(cluster)
        for r in cluster:
            if r["id"] == canonical_id:
                continue
            _repoint_and_delete(conn, r["id"], canonical_id)
            merged += 1
            logger.info(
                "m063: merged company id=%d into canonical id=%d (job_board=%s/%s)",
                r["id"],
                canonical_id,
                key[0],
                key[1],
            )
    return merged


def _merge_by_careers_url(conn: sqlite3.Connection) -> int:
    """Pass 2: merge rows whose careers_url canonicalizes identically."""
    rows = conn.execute(
        "SELECT id, careers_url, jobs_found_total "
        "FROM companies "
        "WHERE careers_url IS NOT NULL AND TRIM(careers_url) != ''"
    ).fetchall()

    by_key: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = _canonical_careers_url(r["careers_url"])
        if not key:
            continue
        by_key[key].append(r)

    merged = 0
    for key, cluster in by_key.items():
        if len(cluster) < 2:
            continue
        canonical_id = _pick_canonical(cluster)
        for r in cluster:
            if r["id"] == canonical_id:
                continue
            _repoint_and_delete(conn, r["id"], canonical_id)
            merged += 1
            logger.info(
                "m063: merged company id=%d into canonical id=%d (careers_url=%s)",
                r["id"],
                canonical_id,
                key,
            )
    return merged


def _merge(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if not _table_exists(conn, "companies"):
        logger.info("m063: companies table not present, no-op")
        return

    # sqlite3.Row factory is required because helper SELECTs access columns
    # by name. The migration runner may or may not set this; force it for
    # the duration of the migration.
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        by_slug = _merge_by_ats_slug(conn)
        # Pass 2 runs AFTER pass 1 so the slug merges don't produce
        # false collisions among rows that already share a board.
        by_url = _merge_by_careers_url(conn)
    finally:
        conn.row_factory = prev_factory

    logger.info(
        "m063: merged %d rows by (ats_platform, ats_slug) and %d by careers_url",
        by_slug,
        by_url,
    )


MIGRATION = Migration(
    version=63,
    description="merge companies by shared job board (ats_platform+slug, then canonical careers_url)",
    py=_merge,
)
