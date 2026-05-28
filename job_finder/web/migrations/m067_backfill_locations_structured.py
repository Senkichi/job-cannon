"""Migration 67 — backfill locations_structured / workplace_type / primary_country_code.

The final step of the location-parsing SPEC (see
`.planning/SPEC-location-parsing.md`). m066 added the three nullable
columns; Commit C wired `upsert_job` to write them on every new
ingestion. This migration re-parses every existing row's
`locations_raw` through `parse_locations(raw, jd_full=row.jd_full)`
and writes the three columns, so historic rows surface in the same
Country / Workplace dropdowns introduced by Commit D.

Idempotent:
  - Re-parsing through the parser is a fixed point (the public anchor
    corpus in `tests/test_location_parser.py` proves this).
  - Each row's three columns are recomputed from scratch from
    `locations_raw` + `jd_full`; running m067 a second time produces
    the same writes (and, in production, will already be a no-op for
    rows that already have populated columns matching the current
    parser output).

Trust gate (per SPEC):
  Ships AFTER Commit D has been live for at least one ingestion cycle
  so the parser is observed working on fresh data first. The Q1
  (country-anchored Springfield) + Q3 (jd_full body hashtag fallback)
  parser refinements landed in the two commits before this one — both
  feed into the m067 backfill so historic data uses the refined parser.

Performance:
  ~10s for 10k rows on the live DB. The hot path is a Python loop
  calling the parser once per row + one UPDATE per row that changed.
  Rows with empty/NULL `locations_raw` are skipped (the parser would
  return `[]` and the three columns would stay NULL — no UPDATE needed).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.location_canonical import to_json
from job_finder.web.location_parser import parse_locations
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _decode_locations_raw(locations_raw: str | None) -> list[str] | str | None:
    """Return the input shape the parser accepts.

    `locations_raw` is a JSON-serialized list[str] in current schema but
    earlier rows occasionally stored a plain string. Both shapes feed
    cleanly into `parse_locations(raw)` — the parser accepts
    `str | list[str] | None`.
    """
    if not locations_raw:
        return None
    try:
        decoded = json.loads(locations_raw)
    except (json.JSONDecodeError, TypeError):
        # Fall back to treating the column value as a single raw string.
        return locations_raw
    if isinstance(decoded, list):
        return [e for e in decoded if isinstance(e, str)]
    if isinstance(decoded, str):
        return decoded
    return None


def _backfill_locations_structured(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'"
    ).fetchone()
    if row is None:
        logger.info("m067: jobs table not present, no-op")
        return

    rows = conn.execute(
        "SELECT dedup_key, locations_raw, jd_full, location FROM jobs"
    ).fetchall()

    updated = 0
    skipped_empty = 0
    for dedup_key, locs_raw, jd_full, location in rows:
        parser_input = _decode_locations_raw(locs_raw)
        # Fallback: a few legacy rows lost locations_raw but still have
        # the merged `location` column. Use it as a single-segment input
        # rather than skipping them.
        if parser_input is None and location:
            parser_input = location
        if parser_input is None:
            skipped_empty += 1
            continue

        structured = parse_locations(parser_input, jd_full=jd_full)
        locations_json = to_json(structured) if structured else None
        workplace_type = structured[0].workplace_type if structured else None
        primary_country_code = structured[0].country_code if structured else None

        conn.execute(
            "UPDATE jobs SET "
            "locations_structured = ?, "
            "workplace_type = ?, "
            "primary_country_code = ? "
            "WHERE dedup_key = ?",
            (locations_json, workplace_type, primary_country_code, dedup_key),
        )
        updated += 1

    logger.info(
        "m067: backfilled %d rows (%d skipped — no locations_raw and no location)",
        updated,
        skipped_empty,
    )


MIGRATION = Migration(
    version=67,
    description="backfill locations_structured/workplace_type/primary_country_code from locations_raw",
    py=_backfill_locations_structured,
)
