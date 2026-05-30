"""Migration 60 — heal location pollution by re-normalizing existing rows.

Background: prior to commit X (this session) the ingestion path stored
``jobs.locations_raw`` and ``jobs.location`` as raw parser-extracted
text without canonicalization. Three classes of pollution accumulated:

  1. Placeholder values like "Unknown" / "N/A" / "TBD" treated as real
     locations and showing up as filter-dropdown entries.
  2. Case / whitespace variants ("Remote" / "remote" / "REMOTE",
     "New York, NY" / "New York, NY " / " New York, NY") that all became
     distinct dropdown entries.
  3. Multi-location parser output joined with `|` / `;` / ` / ` / ` & `
     stored as one entry rather than split into per-location entries.

The new ``location_normalizer`` module is wired into ``upsert_job`` at
the ingestion boundary so future writes are clean. m060 backfills the
same normalization for existing rows:

  - Parse each row's ``locations_raw`` JSON array.
  - For each entry, split on unambiguous multi-location separators,
    normalize each part, drop placeholders, lower-case-dedupe.
  - Write the normalized list back to ``locations_raw``.
  - Rebuild ``location`` column as ", ".join(normalized_list) so the
    LIKE-against-substring filter still hits.

Re-running is safe: idempotent by construction (the normalizer is a
fixed point — applying it twice produces the same output).

Refs FOLLOWUPS.md ("Location pollution / multi-location normalization"
from the 2026-05-27 User Bug List).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.location_normalizer import (
    normalize_location,
    split_multi_locations,
)
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _normalize_locs_list(locations_raw: str | None, location: str | None) -> list[str]:
    """Re-normalize an existing row's locations.

    Sources from ``locations_raw`` (JSON array) when populated; falls back
    to the ``location`` column for old rows that pre-date the
    locations_raw column or were inserted by ingestion paths that didn't
    fill it. Splits each entry on unambiguous multi-location separators,
    normalizes, and lower-case-dedupes. Returns the cleaned list in
    original order (with Remote/Hybrid promoted earlier when spelled
    inline; preserved here from prior write-side logic).
    """
    entries: list[str] = []

    if locations_raw:
        try:
            decoded = json.loads(locations_raw)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, list):
            entries = [e for e in decoded if isinstance(e, str)]

    # Fallback: old rows (or rows inserted by paths that don't fill
    # locations_raw) keep their content only in the merged ``location``
    # column. Treat the whole string as one parser-extracted entry — the
    # split_multi_locations call below will break it on `|` / `;` / etc.
    # if it was a multi-location string.
    if not entries and location:
        entries = [location]

    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        # First try: split as a multi-location string (handles entries
        # that were stored as "Remote | NYC | SF" before split-on-write).
        parts = split_multi_locations(entry)
        if not parts:
            # Single-location fallback path for entries that didn't match
            # any unambiguous separator — still want normalize/placeholder
            # filter applied.
            normalized = normalize_location(entry)
            parts = [normalized] if normalized else []
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(part)
    return out


def _heal_locations(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m060: jobs table not present, no-op")
        return

    rows = conn.execute("SELECT dedup_key, locations_raw, location FROM jobs").fetchall()

    updated = 0
    cleared = 0
    for dedup_key, locs_raw, location in rows:
        normalized = _normalize_locs_list(locs_raw, location)
        new_raw = json.dumps(normalized)
        new_location = ", ".join(normalized)
        if new_raw == (locs_raw or "[]") and new_location == (location or ""):
            continue
        conn.execute(
            "UPDATE jobs SET locations_raw = ?, location = ? WHERE dedup_key = ?",
            (new_raw, new_location, dedup_key),
        )
        updated += 1
        if not normalized:
            cleared += 1

    logger.info(
        "m060: normalized %d rows (%d had all-placeholder locations and were cleared)",
        updated,
        cleared,
    )


MIGRATION = Migration(
    version=60,
    description="normalize jobs.location / locations_raw (dedupe case+whitespace, drop placeholders)",
    py=_heal_locations,
)
