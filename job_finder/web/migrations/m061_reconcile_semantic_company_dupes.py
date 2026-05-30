"""Migration 61 — reconcile semantic company-name duplicates.

Background: m058 collapsed numeric-prefix orphans and EXACT-name
duplicates. That leaves a class of semantic duplicates that share a
canonical name once corporate-form suffixes and trailing parentheticals
are stripped. From the 2026-05-27 User Bug List:

    - AAA Mountain West Group   (MWG)  vs  AAA Mountain West Group
    - Albertsons                vs  Albertsons Companies
    - Acme Corp.                vs  Acme

m061 detects these by computing a "canonical comparison key" per
company row (lowercased, legal-entity-prefix stripped, trailing
parenthetical stripped, suffix stripped). Groups with >1 row collapse
to the lowest id; jobs / company_scan_log / company_research FK
references re-point to the canonical row.

Conservative scope (deliberate non-handling):
  - Subsidiary / branding variants ("Amazon" vs "Amazon Web Services"
    vs "Amazon.com") are NOT merged. They have different base names
    and frequently represent distinct hiring entities. The user's bug
    note about Amazon variants would require a manual aliases UI to
    handle correctly — out of scope for an auto-merge migration.
  - Mid-name punctuation variants ("Goldman Sachs & Co" vs "Goldman
    Sachs") are NOT auto-merged because the suffix regex doesn't
    handle non-comma/whitespace prefixes. Adding "&" support would
    risk false positives on company names like "Penn & Teller".

Re-running is safe: idempotent by construction (after the first heal,
no group has >1 row by canonical key).

Refs FOLLOWUPS.md ("Reconcile semantic company-name duplicates" from
the 2026-05-27 User Bug List).
"""

from __future__ import annotations

import logging
import re
import sqlite3

from job_finder.web.dedup_normalizer import normalize_company
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Trailing parenthetical: "AAA Mountain West Group (MWG)" -> "AAA Mountain
# West Group". Stripped BEFORE normalize_company runs so the bare name
# can flow through the legal-suffix loop.
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Supplemental suffixes that the shared dedup_normalizer._COMPANY_SUFFIXES
# regex does NOT cover. Kept local to m061 to avoid changing dedup_key
# generation across the codebase. Notable miss: "Companies" (plural),
# the variant in the user's bug report ("Albertsons" vs "Albertsons
# Companies"). Singular "Company" IS covered by the shared regex.
# Lowercased because normalize_company lowercases its output.
_EXTRA_SUFFIXES_RE = re.compile(r"\s*(?:companies|enterprises?)\s*$")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _canonical_key(name: str | None) -> str:
    """Comparison-only canonical key for company-table dedupe.

    NOT a replacement for ``normalize_company`` — that fn is used by
    ``normalized_dedup_key`` and must stay stable. This helper layers
    trailing-parenthetical stripping ahead of normalize_company and a
    supplemental suffix pass after it, for the narrower company-row
    dedupe purpose.

    Returns "" for empty input (skipped — no key collisions on empty).
    """
    if not name:
        return ""
    stripped = _TRAILING_PAREN_RE.sub("", name).strip()
    if not stripped:
        return ""
    key = normalize_company(stripped)
    # Supplemental suffix pass — applies repeatedly so "Foo Companies
    # Enterprises" -> "foo" after two strips.
    prev = None
    while key != prev:
        prev = key
        key = _EXTRA_SUFFIXES_RE.sub("", key).strip()
    return key


def _repoint_and_delete(conn: sqlite3.Connection, orphan_id: int, canonical_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    conn.execute(
        "UPDATE company_scan_log SET company_id = ? WHERE company_id = ?",
        (canonical_id, orphan_id),
    )
    # m029 added company_research; some upgraded DBs may not have it
    # (mirrors the table-exists guard used by m058).
    if _table_exists(conn, "company_research"):
        conn.execute(
            "UPDATE company_research SET company_id = ? WHERE company_id = ?",
            (canonical_id, orphan_id),
        )
    conn.execute("DELETE FROM companies WHERE id = ?", (orphan_id,))


def _reconcile(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if not _table_exists(conn, "companies"):
        logger.info("m061: companies table not present, no-op")
        return

    rows = conn.execute(
        "SELECT id, name FROM companies WHERE name IS NOT NULL AND name != '' ORDER BY id ASC"
    ).fetchall()

    # Group ids by canonical key. The lowest id (first encountered) is
    # the canonical; all others are orphans to be merged into it.
    by_key: dict[str, list[int]] = {}
    for company_id, name in rows:
        key = _canonical_key(name)
        if not key:
            continue
        by_key.setdefault(key, []).append(company_id)

    merged = 0
    for key, ids in by_key.items():
        if len(ids) < 2:
            continue
        canonical_id = ids[0]
        for orphan_id in ids[1:]:
            _repoint_and_delete(conn, orphan_id, canonical_id)
            merged += 1
            logger.info(
                "m061: merged company id=%d into canonical id=%d (key=%r)",
                orphan_id,
                canonical_id,
                key,
            )

    logger.info("m061: collapsed %d semantic-dup company rows", merged)


MIGRATION = Migration(
    version=61,
    description="reconcile semantic company-name dupes (paren-abbrev + corporate-suffix variants)",
    py=_reconcile,
)
