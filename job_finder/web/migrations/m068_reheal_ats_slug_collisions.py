"""Migration 68 — re-heal (ats_platform, ats_slug) collisions with a
name-quality heuristic.

m063 merged same-board duplicates using a job-volume tie-break
(``jobs_found_total DESC, id ASC``). That picked the wrong winner when
an aggregator-named row had accumulated more rows than the real
company, because aggregator names like "Experimentation Jobs" or
"People In AI" had been mis-extracted from URL slugs by the dataforseo
/ portal_jooble path, then ``ats_promote`` had silently linked them to
a legit ATS slug owned by a real company (e.g. ``greenhouse/headway``).
Once promoted, the nightly ATS scan walked the slug and created jobs
under the aggregator name instead of the real one — surfacing as rows
with ``jobs.company = "Experimentation Jobs"`` pointing at Headway's
Greenhouse board.

m068 supplements m063 with three additional signals before falling
back to the old volume tie-break:

  1. **Slug-in-name match.** The slug normalised (lowercased, hyphens
     and dots stripped) is searched inside the normalised company
     name. If only one row matches, it wins. This catches "Headway"
     vs "Experimentation Jobs" on slug=``headway``.
  2. **Has homepage_url.** A row with a confirmed homepage is more
     likely to be a real company record (set by the homepage
     discoverer) than a slug-string lifted off an aggregator URL.
  3. **Lacks aggregator markers in name.** Names containing ``jobs``,
     ``careers``, or ``hiring`` as separate words / suffixes get
     deprioritised — those are template strings, not entity names.

For each cluster, the canonical row keeps its (platform, slug). Each
loser:
  - has its jobs re-pointed to the canonical row;
  - has those jobs' ``company`` field rewritten to the canonical name
    (so the dashboard reflects reality);
  - has its ``dedup_key`` rewritten to the canonical name's form
    (so future sync passes deduplicate against the correct prefix);
    if the rewrite collides with an existing job's key, the loser
    row is dropped instead of inserted (the winner already has it);
  - has its ``company_scan_log`` and ``company_research`` references
    re-pointed;
  - is deleted from ``companies``.

Idempotent: subsequent runs find no clusters with len > 1. The matching
runtime guard (``ats_identity_reconcile._reconcile_company_ats`` slug-
collision check) prevents new clusters from forming. Companion
followup: when the runtime guard catches a fresh collision it emits a
``slug_collision`` log line — m068 won't trigger a re-run.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Names that look like template strings rather than real entities.
_AGGREGATOR_HINTS = ("jobs", "careers", "hiring", "talent", "talents")

# Common corporate suffixes that should not block a match. ``Vertex
# Pharmaceuticals`` should still match a slug containing ``vertex``.
_NAME_SUFFIX_TOKENS = (
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
    """Significant slug tokens. Splits on Workday-style path separators."""
    if not slug:
        return []
    raw = re.findall(r"[a-z]+", slug.lower())
    # Workday slugs frequently carry tenant codes like ``wd5`` / ``wd501``;
    # drop pure-numeric or two-character noise and known suffix words.
    return [t for t in raw if len(t) >= 3 and t not in _NAME_SUFFIX_TOKENS]


def _looks_aggregator(name: str | None) -> bool:
    if not name:
        return False
    # Match whole-word occurrences only (case-insensitive). "Headway" should
    # not trip on "way" inside a longer word, and "Greenhouse" should not be
    # flagged just because the chars "career" don't appear standalone.
    tokens = re.findall(r"[a-z]+", name.lower())
    return any(t in _AGGREGATOR_HINTS for t in tokens)


def _slug_name_match_strength(
    row: sqlite3.Row, slug_norm: str, slug_tokens: list[str]
) -> int:
    """How strongly does this name match the slug? Higher is better.

    Levels:
      3 — normalised name == normalised slug, or each contains the other
          (most reliable: "Headway" vs slug "headway")
      2 — any name token appears inside the slug, or any slug token
          appears inside the name (handles "Vertex Pharmaceuticals" vs
          "vrtx.wd501/Vertex_Careers" — "vertex" token is shared)
      0 — neither side recognises the other
    """
    name = row["name_raw"] or ""
    name_norm = _normalize_for_match(name)
    if not name_norm:
        return 0
    if slug_norm and (
        slug_norm == name_norm
        or slug_norm in name_norm
        or name_norm in slug_norm
    ):
        return 3
    nm_tokens = _name_tokens(name)
    if nm_tokens and slug_tokens:
        if any(t in slug_norm for t in nm_tokens):
            return 2
        if any(t in name_norm for t in slug_tokens):
            return 2
    return 0


def _score_member(
    row: sqlite3.Row, slug_norm: str, slug_tokens: list[str]
) -> tuple[int, int, int, int, int]:
    """Score-tuple for `max()` selection. Higher is better.

    Components (in priority order, all int):
      * Slug↔name match strength (3 = exact / substring; 2 = token; 0 = none)
      * 1 if homepage_url is non-empty, else 0
      * 1 if name does NOT contain an aggregator marker, else 0
      * jobs_found_total (m063's tie-break, preserved)
      * -id  (lowest id wins on equal totals — keeps stable FK targets)
    """
    return (
        _slug_name_match_strength(row, slug_norm, slug_tokens),
        1 if (row["homepage_url"] or "").strip() else 0,
        0 if _looks_aggregator(row["name_raw"]) else 1,
        int(row["jobs_found_total"] or 0),
        -int(row["id"]),
    )


def _pick_canonical(cluster: list[sqlite3.Row], slug: str) -> sqlite3.Row:
    slug_norm = _normalize_for_match(slug)
    slug_tokens = _slug_tokens(slug)
    return max(cluster, key=lambda r: _score_member(r, slug_norm, slug_tokens))


def _rewrite_loser_jobs(
    conn: sqlite3.Connection,
    loser_id: int,
    canonical_id: int,
    canonical_name: str,
) -> tuple[int, int]:
    """Move loser's jobs to canonical row, renaming company + dedup_key.

    Where the rewritten dedup_key would collide with an existing job
    on the canonical row, the loser's job is deleted (the canonical row
    already has the title — no information loss).

    Returns: (jobs_moved, jobs_deleted_as_dup).
    """
    loser_jobs = conn.execute(
        "SELECT dedup_key, title FROM jobs WHERE company_id = ?",
        (loser_id,),
    ).fetchall()
    if not loser_jobs:
        return (0, 0)

    moved = deleted = 0
    canonical_prefix = canonical_name.lower().strip()
    for j in loser_jobs:
        old_key = j["dedup_key"]
        title = j["title"] or ""
        # New dedup_key follows the project-wide "company_lower|title_lower"
        # shape. Use the canonical name lowercased + the loser job's exact
        # title to preserve the user-visible job title.
        new_key = f"{canonical_prefix}|{title.lower().strip()}"
        if new_key == old_key:
            # Already keyed under canonical (unlikely but possible after a
            # prior partial run). Just re-point company_id.
            conn.execute(
                "UPDATE jobs SET company_id = ?, company = ? WHERE dedup_key = ?",
                (canonical_id, canonical_name, old_key),
            )
            moved += 1
            continue

        # Collision check before the UPDATE — sqlite3's UNIQUE PK on jobs
        # would otherwise raise IntegrityError. Prefer canonical's existing
        # row (it has the real company linkage already) and drop the loser.
        existing = conn.execute(
            "SELECT 1 FROM jobs WHERE dedup_key = ?",
            (new_key,),
        ).fetchone()
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


def _repoint_side_tables(
    conn: sqlite3.Connection, loser_id: int, canonical_id: int
) -> None:
    conn.execute(
        "UPDATE company_scan_log SET company_id = ? WHERE company_id = ?",
        (canonical_id, loser_id),
    )
    if _table_exists(conn, "company_research"):
        conn.execute(
            "UPDATE company_research SET company_id = ? WHERE company_id = ?",
            (canonical_id, loser_id),
        )


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn
    if not _table_exists(conn, "companies"):
        logger.info("m068: companies table not present, no-op")
        return

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
            by_key[
                (r["ats_platform"].strip().lower(), r["ats_slug"].strip())
            ].append(r)

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
                jm, jd = _rewrite_loser_jobs(
                    conn, loser_id, canonical_id, canonical_name
                )
                _repoint_side_tables(conn, loser_id, canonical_id)
                conn.execute("DELETE FROM companies WHERE id = ?", (loser_id,))
                merged_rows += 1
                jobs_moved_total += jm
                jobs_deleted_total += jd
                logger.info(
                    "m068: merged loser id=%d (%r) into canonical id=%d (%r) "
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
        "m068: total %d collision-clusters resolved, %d loser companies merged, "
        "%d jobs re-pointed, %d duplicate jobs deleted",
        len(clusters),
        merged_rows,
        jobs_moved_total,
        jobs_deleted_total,
    )


MIGRATION = Migration(
    version=68,
    description="re-heal (ats_platform, ats_slug) collisions with name-quality heuristic",
    py=_heal,
)
