"""Shared company-deduplication helpers.

These helpers were originally inlined in
``migrations/m068_reheal_ats_slug_collisions.py``.  They are extracted here so
that both the migration *and* the scheduled ``run_registry_hygiene`` job share a
single implementation — single point of enforcement, no copy-paste drift.

Public API consumed by callers:

    merge_exact_name_duplicates(conn)  → dict
        Collapse ``find_duplicate_companies`` pairs (same normalised name)
        into a single canonical row; return merge stats.

    heal_ats_slug_clusters(conn)  → dict
        Resolve ``(ats_platform, ats_slug)`` clusters with >1 row using the
        name-quality heuristic from m068; return heal stats.

    find_mispromoted_ats_slugs(conn)  → list[dict]
        Return rows where the company owning an ATS slug is
        aggregator-shaped *and* a better-named sibling exists — the
        "NielsenIQ/2629" class of mis-promotion.

The loser→canonical machinery (_rewrite_loser_jobs, _repoint_side_tables,
_pick_canonical, _looks_aggregator, _slug_name_match_strength) mirrors the
m068 private helpers exactly so the migration can be thinned to a shim.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict

from job_finder.web.dedup_normalizer import derive_dedup_key, normalize_company

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (mirrored from m068 to keep the shared module self-contained)
# ---------------------------------------------------------------------------

_AGGREGATOR_HINTS: tuple[str, ...] = ("jobs", "careers", "hiring", "talent", "talents")

_NAME_SUFFIX_TOKENS: tuple[str, ...] = (
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "llc",
    "ltd",
    "limited",
    "plc",
    "ag",
    "sa",
    "gmbh",
    "group",
    "holdings",
    "pharmaceuticals",
    "pharma",
    "industries",
    "international",
    "global",
    "labs",
    "labs.",
    "technologies",
    "technology",
    "solutions",
    "systems",
    "services",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _normalize_for_match(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[\s\-_./,]+", "", s.lower())


def _name_tokens(name: str | None) -> list[str]:
    """Significant name tokens (>=3 letters, suffix-words dropped, no pure numbers)."""
    if not name:
        return []
    raw = re.findall(r"[a-z]+", name.lower())
    return [t for t in raw if len(t) >= 3 and t not in _NAME_SUFFIX_TOKENS]


def _slug_tokens(slug: str | None) -> list[str]:
    """Significant slug tokens — splits on Workday-style path separators."""
    if not slug:
        return []
    raw = re.findall(r"[a-z]+", slug.lower())
    return [t for t in raw if len(t) >= 3 and t not in _NAME_SUFFIX_TOKENS]


def _looks_aggregator(name: str | None) -> bool:
    """Return True when the name contains aggregator marker words (whole-word match)."""
    if not name:
        return False
    tokens = re.findall(r"[a-z]+", name.lower())
    return any(t in _AGGREGATOR_HINTS for t in tokens)


def _slug_name_match_strength(row: sqlite3.Row, slug_norm: str, slug_tok: list[str]) -> int:
    """How strongly does this row's name match the slug?

    3 — normalised name == normalised slug, or each contains the other.
    2 — any name token appears inside the slug, or any slug token inside the name.
    0 — no overlap.
    """
    name = row["name_raw"] or ""
    name_norm = _normalize_for_match(name)
    if not name_norm:
        return 0
    if slug_norm and (slug_norm == name_norm or slug_norm in name_norm or name_norm in slug_norm):
        return 3
    nm_tokens = _name_tokens(name)
    if nm_tokens and slug_tok:
        if any(t in slug_norm for t in nm_tokens):
            return 2
        if any(t in name_norm for t in slug_tok):
            return 2
    return 0


def _score_member(
    row: sqlite3.Row, slug_norm: str, slug_tok: list[str]
) -> tuple[int, int, int, int, int]:
    """Score-tuple for max() canonical selection (higher is better)."""
    return (
        _slug_name_match_strength(row, slug_norm, slug_tok),
        1 if (row["homepage_url"] or "").strip() else 0,
        0 if _looks_aggregator(row["name_raw"]) else 1,
        int(row["jobs_found_total"] or 0),
        -int(row["id"]),
    )


def _pick_canonical(cluster: list[sqlite3.Row], slug: str) -> sqlite3.Row:
    slug_norm = _normalize_for_match(slug)
    slug_tok = _slug_tokens(slug)
    return max(cluster, key=lambda r: _score_member(r, slug_norm, slug_tok))


# ---------------------------------------------------------------------------
# Loser→canonical mechanics (shared by m068 and run_registry_hygiene)
# ---------------------------------------------------------------------------


def rewrite_loser_jobs(
    conn: sqlite3.Connection,
    loser_id: int,
    canonical_id: int,
    canonical_name: str,
) -> tuple[int, int]:
    """Move loser's jobs to canonical row, renaming company + dedup_key.

    Where the rewritten dedup_key would collide with an existing job on the
    canonical row, the loser's job is deleted (canonical already has it —
    no information loss).

    Returns: (jobs_moved, jobs_deleted_as_dup).
    """
    loser_jobs = conn.execute(
        "SELECT dedup_key, title FROM jobs WHERE company_id = ?",
        (loser_id,),
    ).fetchall()
    if not loser_jobs:
        return (0, 0)

    moved = deleted = 0
    for j in loser_jobs:
        old_key = j["dedup_key"] if isinstance(j, sqlite3.Row) else j[0]
        title = (j["title"] if isinstance(j, sqlite3.Row) else j[1]) or ""
        # Canonical dedup_key derivation (D-8) — must match Job.dedup_key and the
        # upsert lookup, else the collision check below misses and a duplicate
        # row is created. Raw .lower().strip() skipped legal-suffix / abbreviation
        # / level-suffix normalization and diverged from the canonical key.
        new_key = derive_dedup_key(canonical_name, title)
        if new_key == old_key:
            conn.execute(
                "UPDATE jobs SET company_id = ?, company = ? WHERE dedup_key = ?",
                (canonical_id, canonical_name, old_key),
            )
            moved += 1
            continue

        existing = conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (new_key,)).fetchone()
        if existing:
            conn.execute("DELETE FROM jobs WHERE dedup_key = ?", (old_key,))
            deleted += 1
        else:
            conn.execute(
                """UPDATE jobs
                      SET company_id = ?, company = ?, dedup_key = ?
                    WHERE dedup_key = ?""",
                (canonical_id, canonical_name, new_key, old_key),
            )
            moved += 1
    return (moved, deleted)


def repoint_side_tables(conn: sqlite3.Connection, loser_id: int, canonical_id: int) -> None:
    """Re-point company_scan_log and company_research from loser to canonical."""
    conn.execute(
        "UPDATE company_scan_log SET company_id = ? WHERE company_id = ?",
        (canonical_id, loser_id),
    )
    if _table_exists(conn, "company_research"):
        conn.execute(
            "UPDATE company_research SET company_id = ? WHERE company_id = ?",
            (canonical_id, loser_id),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def heal_ats_slug_clusters(conn: sqlite3.Connection) -> dict:
    """Resolve (ats_platform, ats_slug) clusters with >1 row.

    Identical algorithm to m068._heal but operates on an already-open
    connection (no MigrationContext wrapper).  Safe to call multiple times
    (idempotent — after the first run no clusters remain).

    Returns dict: {clusters_resolved, companies_merged, jobs_moved, jobs_deduped}.
    """
    if not _table_exists(conn, "companies"):
        return {"clusters_resolved": 0, "companies_merged": 0, "jobs_moved": 0, "jobs_deduped": 0}

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, name_raw, ats_platform, ats_slug,
                       homepage_url, jobs_found_total
                  FROM companies
                 WHERE ats_platform IS NOT NULL
                   AND ats_platform != ''
                   AND ats_slug IS NOT NULL
                   AND ats_slug != ''"""
        ).fetchall()

        by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            by_key[(r["ats_platform"].strip().lower(), r["ats_slug"].strip())].append(r)

        clusters = [(k, v) for k, v in by_key.items() if len(v) > 1]
        merged_rows = jobs_moved_total = jobs_deleted_total = 0

        for (platform, slug), cluster in clusters:
            canonical = _pick_canonical(cluster, slug)
            canonical_id = int(canonical["id"])
            canonical_name = canonical["name_raw"] or ""
            for r in cluster:
                if int(r["id"]) == canonical_id:
                    continue
                loser_id = int(r["id"])
                jm, jd = rewrite_loser_jobs(conn, loser_id, canonical_id, canonical_name)
                repoint_side_tables(conn, loser_id, canonical_id)
                conn.execute("DELETE FROM companies WHERE id = ?", (loser_id,))
                merged_rows += 1
                jobs_moved_total += jm
                jobs_deleted_total += jd
                logger.info(
                    "company_dedup: merged loser id=%d (%r) into canonical id=%d (%r) "
                    "on %s/%s — jobs_moved=%d jobs_deduped=%d",
                    loser_id,
                    r["name_raw"],
                    canonical_id,
                    canonical_name,
                    platform,
                    slug,
                    jm,
                    jd,
                )
    finally:
        conn.row_factory = prev_factory

    logger.info(
        "company_dedup.heal_ats_slug_clusters: %d clusters resolved, "
        "%d companies merged, %d jobs moved, %d jobs deduped",
        len(clusters),
        merged_rows,
        jobs_moved_total,
        jobs_deleted_total,
    )
    return {
        "clusters_resolved": len(clusters),
        "companies_merged": merged_rows,
        "jobs_moved": jobs_moved_total,
        "jobs_deduped": jobs_deleted_total,
    }


def merge_exact_name_duplicates(conn: sqlite3.Connection) -> dict:
    """Collapse companies sharing the same normalised name into one canonical row.

    This is the *only* auto-merge path wired into run_registry_hygiene.
    find_fuzzy_false_positives output is NEVER auto-merged here.

    Algorithm:
      - Group companies by normalize_company(name).
      - For each group with >1 member pick a canonical row (highest
        jobs_found_total, then lowest id as tie-break).
      - Re-point jobs + side tables from every loser to canonical.
      - Delete loser rows.

    Returns dict: {pairs_merged, companies_deleted, jobs_moved, jobs_deduped}.
    """
    if not _table_exists(conn, "companies"):
        return {"pairs_merged": 0, "companies_deleted": 0, "jobs_moved": 0, "jobs_deduped": 0}

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, name, name_raw, homepage_url, jobs_found_total FROM companies"
        ).fetchall()
    finally:
        conn.row_factory = prev_factory

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        norm = normalize_company(r["name"] or "")
        if norm:
            groups.setdefault(norm, []).append(r)

    dup_groups = [(norm, members) for norm, members in groups.items() if len(members) > 1]
    pairs_merged = companies_deleted = jobs_moved_total = jobs_deduped_total = 0

    for _norm, members in dup_groups:
        # Canonical = highest jobs_found_total, then lowest id
        canonical = max(
            members,
            key=lambda r: (int(r["jobs_found_total"] or 0), -int(r["id"])),
        )
        canonical_id = int(canonical["id"])
        canonical_name = canonical["name_raw"] or canonical["name"] or ""

        for r in members:
            if int(r["id"]) == canonical_id:
                continue
            loser_id = int(r["id"])
            jm, jd = rewrite_loser_jobs(conn, loser_id, canonical_id, canonical_name)
            repoint_side_tables(conn, loser_id, canonical_id)
            conn.execute("DELETE FROM companies WHERE id = ?", (loser_id,))
            pairs_merged += 1
            companies_deleted += 1
            jobs_moved_total += jm
            jobs_deduped_total += jd
            logger.info(
                "company_dedup: exact-name merge: loser id=%d (%r) → canonical id=%d (%r) "
                "jobs_moved=%d jobs_deduped=%d",
                loser_id,
                r["name_raw"],
                canonical_id,
                canonical_name,
                jm,
                jd,
            )

    if pairs_merged:
        logger.warning(
            "company_dedup.merge_exact_name_duplicates: collapsed %d duplicate pairs "
            "(%d companies deleted, %d jobs re-pointed)",
            pairs_merged,
            companies_deleted,
            jobs_moved_total,
        )
    return {
        "pairs_merged": pairs_merged,
        "companies_deleted": companies_deleted,
        "jobs_moved": jobs_moved_total,
        "jobs_deduped": jobs_deduped_total,
    }


def find_mispromoted_ats_slugs(conn: sqlite3.Connection) -> list[dict]:
    """Find rows where an aggregator-named company owns a real company's ATS slug.

    Detection logic (the "NielsenIQ/id-2629" class):
      1. The owning company's name looks aggregator-shaped (_looks_aggregator).
      2. There exists another company whose name matches the slug better
         (_slug_name_match_strength > 0).

    Returns list of dicts:
        {
          "owner_id": int,       # aggregator-named row that owns the slug
          "owner_name": str,
          "platform": str,
          "slug": str,
          "candidate_id": int,   # better-named sibling
          "candidate_name": str,
          "match_strength": int, # 2 or 3
        }

    Only high-confidence candidates (match_strength >= 2) are returned.
    The caller decides whether to auto-re-point or just surface the count.
    """
    if not _table_exists(conn, "companies"):
        return []

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        # All rows with an ATS slug
        slug_owners = conn.execute(
            """SELECT id, name_raw, ats_platform, ats_slug, homepage_url, jobs_found_total
                 FROM companies
                WHERE ats_platform IS NOT NULL AND ats_platform != ''
                  AND ats_slug IS NOT NULL AND ats_slug != ''"""
        ).fetchall()

        # All rows without an ATS slug (potential better-named siblings)
        slug_free = conn.execute(
            """SELECT id, name_raw, homepage_url, jobs_found_total
                 FROM companies
                WHERE (ats_platform IS NULL OR ats_platform = '')
                  AND name_raw IS NOT NULL"""
        ).fetchall()
    finally:
        conn.row_factory = prev_factory

    results: list[dict] = []
    for owner in slug_owners:
        if not _looks_aggregator(owner["name_raw"]):
            continue
        slug = owner["ats_slug"] or ""
        slug_norm = _normalize_for_match(slug)
        slug_tok = _slug_tokens(slug)
        best_candidate = None
        best_strength = 0
        for candidate in slug_free:
            if candidate["id"] == owner["id"]:
                continue
            strength = _slug_name_match_strength(candidate, slug_norm, slug_tok)
            if strength > best_strength:
                best_strength = strength
                best_candidate = candidate
        if best_strength >= 2 and best_candidate is not None:
            results.append(
                {
                    "owner_id": int(owner["id"]),
                    "owner_name": owner["name_raw"] or "",
                    "platform": owner["ats_platform"] or "",
                    "slug": slug,
                    "candidate_id": int(best_candidate["id"]),
                    "candidate_name": best_candidate["name_raw"] or "",
                    "match_strength": best_strength,
                }
            )

    return results


def heal_mispromoted_ats_slugs(conn: sqlite3.Connection) -> dict:
    """Re-point high-confidence aggregator mis-promotions to the real company.

    Only acts on cases returned by find_mispromoted_ats_slugs (match_strength
    >= 2 + aggregator owner name).  For each case:
      - Transfer ats_platform/ats_slug from the aggregator row to the real row.
      - Re-point jobs from the aggregator to the real company via rewrite_loser_jobs.
      - Re-point side tables.
      - Null out the aggregator row's ATS fields (it may still have other jobs).

    Returns dict: {healed, jobs_moved, jobs_deduped}.
    """
    candidates = find_mispromoted_ats_slugs(conn)
    healed = jobs_moved_total = jobs_deduped_total = 0

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for c in candidates:
            owner_id = c["owner_id"]
            candidate_id = c["candidate_id"]
            candidate_name_row = conn.execute(
                "SELECT name_raw FROM companies WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate_name_row is None:
                continue
            canonical_name = candidate_name_row["name_raw"] or ""

            # Transfer ATS slug to the real company (only if the real company
            # doesn't already have one — safety guard).
            existing_slug = conn.execute(
                "SELECT ats_platform, ats_slug FROM companies WHERE id = ?", (candidate_id,)
            ).fetchone()
            if existing_slug and (existing_slug["ats_platform"] or existing_slug["ats_slug"]):
                # Real company already has an ATS entry — skip auto-re-point,
                # leave for manual review.
                logger.warning(
                    "company_dedup.heal_mispromoted: candidate id=%d already has ATS "
                    "entry (%s/%s) — skipping auto-re-point of %s/%s from owner id=%d",
                    candidate_id,
                    existing_slug["ats_platform"],
                    existing_slug["ats_slug"],
                    c["platform"],
                    c["slug"],
                    owner_id,
                )
                continue

            # Clear the aggregator's ATS fields FIRST so the unique constraint
            # (ats_platform, ats_slug) is released before we assign the pair
            # to the real company.
            conn.execute(
                "UPDATE companies SET ats_platform = NULL, ats_slug = NULL WHERE id = ?",
                (owner_id,),
            )
            conn.execute(
                "UPDATE companies SET ats_platform = ?, ats_slug = ? WHERE id = ?",
                (c["platform"], c["slug"], candidate_id),
            )

            jm, jd = rewrite_loser_jobs(conn, owner_id, candidate_id, canonical_name)
            repoint_side_tables(conn, owner_id, candidate_id)
            healed += 1
            jobs_moved_total += jm
            jobs_deduped_total += jd
            logger.info(
                "company_dedup.heal_mispromoted: re-pointed %s/%s from aggregator "
                "id=%d (%r) to real company id=%d (%r) — jobs_moved=%d jobs_deduped=%d",
                c["platform"],
                c["slug"],
                owner_id,
                c["owner_name"],
                candidate_id,
                canonical_name,
                jm,
                jd,
            )
    finally:
        conn.row_factory = prev_factory

    if healed:
        logger.warning(
            "company_dedup.heal_mispromoted: re-pointed %d aggregator slug(s), %d jobs moved",
            healed,
            jobs_moved_total,
        )
    return {"healed": healed, "jobs_moved": jobs_moved_total, "jobs_deduped": jobs_deduped_total}
