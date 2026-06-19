"""Migration 108 — deterministic location sync + quarantine of unresolvable rows (P2.4).

The healing half of Phase 2 of the Data Integrity Overhaul (tracking #393).
Earlier Phase-2 tasks made location lossless going *forward*: P2.1 added the
``apply_location_observation`` single-writer funnel (D-5); P2.2 taught the
crawler to capture JSON-LD ``jobLocation`` and gazetteer-validated URL-slug
hints (D-1); P2.3 added a scheduled extraction pass that drains empty-location
rows from their existing ``jd_full`` through the same funnel.

This migration heals the rows that already exist, *offline and deterministically*
— no LLM, no network (the LLM work is P2.3's scheduled job). It re-derives the
canonical location columns from retained evidence (D-12: heal retroactively from
evidence) and routes whatever genuinely cannot be resolved offline into the
existing quarantine surface (D-9: ``unresolved_reasons`` + /admin/review +
the enrichment backfill).

It fixes the S4/S5 fallout measured on the 2026-06-12 production snapshot:
338/481 careers_crawl rows (70%) carried an empty ``location`` despite a
location living in the URL slug or the JD, because the crawler hardcoded
``location=""``, enrichment side-door-wrote ``location`` without
``locations_raw``, and every re-sighting rebuilt ``location`` from an empty
``locations_raw`` (the wipe).

Four outcome classes, applied in order, counts logged per class (D-12):

  1. **Legacy side-door writes** — ``location`` non-empty but ``locations_raw``
     empty (the enrichment path wrote the display column directly): seed
     ``locations_raw = [location]`` and re-derive all five canonical columns
     (m067 logic, *without* its empty-``locations_raw`` skip).
  2. **m067 skips + post-m067 drift** — ``locations_raw`` non-empty but
     ``locations_structured`` NULL: re-parse ``locations_raw`` and backfill the
     three structured columns (m067 verbatim).
  3. **URL-slug recovery** — careers_crawl rows with an all-empty location whose
     ``source_urls`` slug yields a gazetteer-validated candidate (reuse P2.2's
     ``_location_from_url_slug`` helper): seed it and re-derive all five columns.
  4. **Quarantine the residue** — rows still empty-location after 1–3 with a
     substantive ``jd_full`` (≥ 200 chars): append ``location_missing`` to
     ``unresolved_reasons`` so they surface as amber badges in /admin/review
     (D-9). The scheduled P2.3 pass has, by the time this ships, already drained
     the bulk of these organically; this is the final sweep that declares the
     remainder provably-exhausted-offline and makes it visible.

I-07 (``locations_raw`` non-empty ⇒ ``locations_structured`` non-empty) is an
application-level invariant (``LocationShapeError`` in ``ParsedJob.from_job``),
NOT a DB trigger — so a bad write here would persist silently. Classes 1 and 3
therefore seed ``locations_raw`` only when the parse yields a non-empty
structured result; a location string that resolves to nothing leaves the row
untouched rather than minting a fresh I-07 violation.

Idempotent on both empty and populated DBs:
  - Class 1 rows gain a non-empty ``locations_raw`` and drop out of its filter.
  - Class 2 rows gain a non-NULL ``locations_structured`` and drop out.
  - Class 3 rows gain a non-empty ``location`` and drop out.
  - Class 4 rows are already tagged and the NOT-EXISTS guard skips them.
  - Re-parsing through ``parse_locations`` is a fixed point (the public anchor
    corpus in ``tests/test_location_parser.py`` proves this).
No-op when the ``jobs`` table is absent (fresh install pre-Migration 1).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Mirror of data_enricher._EXTRACTION_ONLY_MIN_JD_CHARS. Inlined so this
# migration's quarantine threshold is frozen-in-time (MI-4) even if the app
# constant drifts — the rows class 4 tags are exactly the rows P2.3's scheduled
# pass selects (empty location + jd_full ≥ this floor), so the two must agree at
# ship time.
_MIN_JD_CHARS = 200

# Class-3 quarantine/recovery is scoped to crawler-sourced rows (the S4 cohort).
_CAREERS_CRAWL_SOURCE = "careers_crawl"


def _decode_locations_raw(locations_raw: str | None) -> list[str]:
    """Decode the ``locations_raw`` column into the list the parser accepts.

    ``locations_raw`` is a JSON-serialized ``list[str]`` in current schema, but
    a handful of legacy rows stored a bare string. Both decode cleanly here;
    anything non-list/non-string yields ``[]``.
    """
    if not locations_raw:
        return []
    try:
        decoded = json.loads(locations_raw)
    except (json.JSONDecodeError, TypeError):
        return [locations_raw]
    if isinstance(decoded, list):
        return [e for e in decoded if isinstance(e, str) and e]
    if isinstance(decoded, str) and decoded:
        return [decoded]
    return []


def _apply_single_location(
    conn: sqlite3.Connection,
    dedup_key: str,
    location_str: str,
    jd_full: str | None,
    parse_locations,
    to_json,
) -> bool:
    """Seed ``locations_raw = [location_str]`` and re-derive all five columns (D-5).

    Used by class 1 (legacy side-door write) and class 3 (URL-slug recovery).
    Mirrors m067's write set but additionally seeds ``locations_raw`` and the
    derived ``location`` display string, so all five canonical columns move
    together.

    Returns True when the parse was non-empty and the row was rewritten; False
    when the parse yields nothing (row left untouched to preserve I-07 —
    ``locations_raw`` non-empty ⇒ ``locations_structured`` non-empty).
    """
    raw_list = [location_str]
    structured = parse_locations(raw_list, jd_full=jd_full)
    if not structured:
        return False
    conn.execute(
        "UPDATE jobs SET "
        "locations_raw = ?, "
        "location = ?, "
        "locations_structured = ?, "
        "workplace_type = ?, "
        "primary_country_code = ? "
        "WHERE dedup_key = ?",
        (
            json.dumps(raw_list),
            location_str,
            to_json(structured),
            structured[0].workplace_type,
            structured[0].country_code,
            dedup_key,
        ),
    )
    return True


def _reparse_structured(
    conn: sqlite3.Connection,
    dedup_key: str,
    locations_raw: str | None,
    jd_full: str | None,
    parse_locations,
    to_json,
) -> bool:
    """Re-parse an existing ``locations_raw`` and backfill the structured columns.

    Class 2 — m067 verbatim: ``locations_raw`` is already canonical (non-empty),
    so only the three structured columns are (re)written; ``location`` and
    ``locations_raw`` are preserved.

    Returns True when the parse was non-empty and the columns were written.
    """
    raw_list = _decode_locations_raw(locations_raw)
    if not raw_list:
        return False
    structured = parse_locations(raw_list, jd_full=jd_full)
    if not structured:
        return False
    conn.execute(
        "UPDATE jobs SET "
        "locations_structured = ?, "
        "workplace_type = ?, "
        "primary_country_code = ? "
        "WHERE dedup_key = ?",
        (
            to_json(structured),
            structured[0].workplace_type,
            structured[0].country_code,
            dedup_key,
        ),
    )
    return True


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'").fetchone()
        is None
    ):
        logger.info("m108: jobs table not present, no-op")
        return

    # Lazy imports — keep migration discovery (which imports this module) free
    # of any web/ import cycle, and match the deferred-import pattern the
    # location funnel and _location_from_url_slug already use.
    from job_finder.web.careers_crawler._static_tier import _location_from_url_slug
    from job_finder.web.location_canonical import to_json
    from job_finder.web.location_parser import parse_locations

    # --- Class 1: location non-empty, locations_raw empty (legacy side-door) ---
    class1 = conn.execute(
        "SELECT dedup_key, location, jd_full FROM jobs "
        "WHERE location IS NOT NULL AND location != '' "
        "AND (locations_raw IS NULL OR locations_raw = '' OR locations_raw = '[]')"
    ).fetchall()
    c1 = 0
    for dedup_key, location, jd_full in class1:
        if _apply_single_location(conn, dedup_key, location, jd_full, parse_locations, to_json):
            c1 += 1

    # --- Class 2: locations_raw non-empty, locations_structured NULL ---
    class2 = conn.execute(
        "SELECT dedup_key, locations_raw, jd_full FROM jobs "
        "WHERE locations_raw IS NOT NULL AND locations_raw != '' AND locations_raw != '[]' "
        "AND locations_structured IS NULL"
    ).fetchall()
    c2 = 0
    for dedup_key, locations_raw, jd_full in class2:
        if _reparse_structured(conn, dedup_key, locations_raw, jd_full, parse_locations, to_json):
            c2 += 1

    # --- Class 3: careers_crawl all-empty location, recover from URL slug ---
    class3 = conn.execute(
        "SELECT dedup_key, source_urls, jd_full FROM jobs "
        "WHERE (location IS NULL OR location = '') "
        "AND (locations_raw IS NULL OR locations_raw = '' OR locations_raw = '[]') "
        "AND json_valid(sources) = 1 "
        "AND EXISTS (SELECT 1 FROM json_each(sources) WHERE value = ?)",
        (_CAREERS_CRAWL_SOURCE,),
    ).fetchall()
    c3 = 0
    for dedup_key, source_urls, jd_full in class3:
        try:
            urls = json.loads(source_urls) if source_urls else []
        except (json.JSONDecodeError, TypeError):
            urls = []
        if not isinstance(urls, list):
            continue
        candidate: str | None = None
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            candidate = _location_from_url_slug(url)
            if candidate:
                break
        if not candidate:
            continue
        if _apply_single_location(conn, dedup_key, candidate, jd_full, parse_locations, to_json):
            c3 += 1

    # --- Class 4: quarantine the residue still empty after 1–3 (D-9) ---
    # Re-queried fresh so classes 1/3's in-transaction fills are excluded.
    class4 = conn.execute(
        "SELECT dedup_key, unresolved_reasons FROM jobs "
        "WHERE (location IS NULL OR location = '') "
        "AND jd_full IS NOT NULL AND length(jd_full) >= ? "
        "AND (unresolved_reasons IS NULL "
        "     OR json_extract(unresolved_reasons, '$') IS NULL "
        "     OR NOT EXISTS (SELECT 1 FROM json_each(unresolved_reasons) "
        "                    WHERE value = 'location_missing'))",
        (_MIN_JD_CHARS,),
    ).fetchall()
    c4 = 0
    for dedup_key, unresolved_reasons in class4:
        try:
            reasons = json.loads(unresolved_reasons) if unresolved_reasons else []
        except (json.JSONDecodeError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []
        if "location_missing" in reasons:
            continue
        reasons.append("location_missing")
        conn.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            (json.dumps(reasons), dedup_key),
        )
        c4 += 1

    logger.info(
        "m108: location sync — class1(side-door)=%d, class2(structured-drift)=%d, "
        "class3(url-slug)=%d, class4(quarantined location_missing)=%d",
        c1,
        c2,
        c3,
        c4,
    )


MIGRATION = Migration(
    version=108,
    description="deterministic location sync + quarantine of unresolvable rows (P2.4)",
    py=_heal,
)
